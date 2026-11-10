"""``Scheduler.reassign`` (public method used by the Supervisor's ``reassign_task``)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from agentplatform.errors import ConflictError, InvalidRequestError, NotFoundError

from .harness import FAST_SCHEDULER, Behavior, TaskModel, World, open_session

# A long lease: these tests interrupt runs on purpose, so an expiry on a busy
# machine must not interfere.
CONFIG = replace(FAST_SCHEDULER, lease_ttl_s=30.0)


def agent_id(w: World, name: str) -> str:
    return next(a.id for a in w.scheduler.state.agents.values() if a.name == name)


async def wait_running(w: World, key: str, name: str) -> None:
    async def check() -> bool:
        t = w.scheduler.state.tasks.get(key)
        return (
            t is not None
            and t.status == "running"
            and t.assigned_agent_id is not None
            and w.scheduler.state.agents[t.assigned_agent_id].name == name
        )

    await w.until(check)


async def test_running_task_moves_to_the_named_agent_and_old_run_is_fenced() -> None:
    model = TaskModel({"T1": Behavior(steps=2, hang_agents=frozenset({"Ada"}))})
    w = await open_session(edges={"T1": []}, agent_count=2, model=model, scheduler_config=CONFIG)
    try:
        await wait_running(w, "T1", "Ada")
        brook = agent_id(w, "Brook")
        assert await w.scheduler.reassign("T1", brook) == "requeued"
        ada = agent_id(w, "Ada")
        accepted = await w.wait_for("task.accepted", lambda p: p.task_key == "T1")
        # The old agent is fenced and recovered in the background. Its run may notice
        # the lost lease first ("task lease lost") or the pool marks it ("task T1
        # reassigned"); either way it goes to error and then back to idle.
        await w.wait_for(
            "agent.status_changed", lambda p: p.agent_id == ada and p.status == "error"
        )
        await w.wait_for(
            "agent.status_changed",
            lambda p: p.agent_id == ada and p.status == "idle" and p.reason == "recovered",
        )
        assigned = await w.payloads("task.assigned")
    finally:
        await w.stop()

    assert accepted.task_key == "T1"
    assert [p.agent_id for p in assigned] == [ada, brook]
    assert assigned[1].attempt == 2  # the interrupted run counted as an attempt


async def test_without_agent_the_current_agent_is_avoided() -> None:
    model = TaskModel({"T1": Behavior(steps=2, hang_agents=frozenset({"Ada"}))})
    w = await open_session(edges={"T1": []}, agent_count=2, model=model, scheduler_config=CONFIG)
    try:
        await wait_running(w, "T1", "Ada")
        assert await w.scheduler.reassign("T1") == "requeued"
        await w.wait_for("task.accepted", lambda p: p.task_key == "T1")
        assigned = await w.payloads("task.assigned")
    finally:
        await w.stop()
    assert [p.agent_id for p in assigned] == [agent_id(w, "Ada"), agent_id(w, "Brook")]


async def test_pending_task_preference_beats_affinity() -> None:
    model = TaskModel({"T1": Behavior(steps=30)})
    w = await open_session(
        edges={"T1": [], "T2": ["T1"]}, agent_count=2, model=model, scheduler_config=CONFIG
    )
    try:
        await wait_running(w, "T1", "Ada")
        assert await w.scheduler.reassign("T2", agent_id(w, "Brook")) == "preferred"
        await w.wait_for("session.completed")
    finally:
        await w.stop()
    assert model.agents_for("T1") == {"Ada"}
    assert model.agents_for("T2") == {"Brook"}  # affinity alone would pick Ada


async def test_same_agent_is_unchanged_and_bad_requests_raise() -> None:
    model = TaskModel({"T1": Behavior(steps=2, hang_agents=frozenset({"Ada"}))})
    w = await open_session(
        edges={"T1": [], "T2": []}, agent_count=2, model=model, scheduler_config=CONFIG
    )
    try:
        await wait_running(w, "T1", "Ada")
        assert await w.scheduler.reassign("T1", agent_id(w, "Ada")) == "unchanged"
        with pytest.raises(NotFoundError):
            await w.scheduler.reassign("T9")
        with pytest.raises(InvalidRequestError):
            await w.scheduler.reassign("T1", "no-such-agent")
        await w.wait_for("task.accepted", lambda p: p.task_key == "T2")
        with pytest.raises(ConflictError):
            await w.scheduler.reassign("T2")
        assert len(await w.payloads("task.assigned")) == 2
    finally:
        await w.stop()


async def test_reassign_needs_a_running_scheduler() -> None:
    w = await open_session(edges={"T1": []}, start=False)
    with pytest.raises(RuntimeError):
        await w.scheduler.reassign("T1")
