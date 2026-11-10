"""Scheduler failure paths (SPEC §12 rules 4–8): ack timeout, lease expiry,
retries and max attempts, budgets, user stop."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from agentplatform.contracts.session import Budgets

from .harness import FAST_SCHEDULER, Behavior, TaskModel, World, open_session, order_of


async def _names(w: World) -> dict[str, str]:
    return {p.agent.id: p.agent.name for p in await w.payloads("agent.created")}


async def test_ack_timeout_reassigns_to_another_agent() -> None:
    # Ada's queue consumer is hung: she never acknowledges her assignment.
    w = await open_session(edges={"T1": []}, agent_count=2, deaf_agents=["Ada"])
    try:
        await w.wait_for("session.completed")
        events = await w.events()
    finally:
        await w.stop()
    names = await _names(w)
    assigned = [(names[p.agent_id], p.attempt) for p in await w.payloads("task.assigned")]
    assert assigned == [("Ada", 1), ("Brook", 1)]  # reassigned; never started, same attempt
    acked = [names[p.agent_id] for p in await w.payloads("task.assignment_acked")]
    assert acked == ["Brook"]
    errors = [p for p in await w.payloads("agent.error") if names[p.agent_id] == "Ada"]
    assert any("assignment_acked" in p.error for p in errors)
    # task.ready (requeue) came between the two assignments
    kinds = [e.type for e in events if getattr(e.payload, "task_key", None) == "T1"]
    first, second = (i for i, k in enumerate(kinds) if k == "task.assigned")
    assert "task.ready" in kinds[first:second]
    assert w.model.agents_for("T1") == {"Brook"}


async def test_lease_expiry_requeues_with_attempt_two_and_recovers_the_agent() -> None:
    # Ada's runtime "crashes": her model call hangs and her heartbeat never renews
    # the lease, so the lease (TTL 1 s in tests) expires.
    model = TaskModel({"T1": Behavior(hang_agents=frozenset({"Ada"}))})
    w = await open_session(edges={"T1": []}, agent_count=2, model=model, no_renew=["Ada"])
    try:
        await w.wait_for("session.completed", limit_s=20)
        # the recovered agent comes back: error -> idle
        await w.wait_for(
            "agent.status_changed", lambda p: (p.previous, p.status) == ("error", "idle")
        )
        events = await w.events()
    finally:
        await w.stop()
    names = await _names(w)
    started = [(names[p.agent_id], p.attempt) for p in await w.payloads("task.started")]
    assert started == [("Ada", 1), ("Brook", 2)]
    failed = await w.payloads("task.failed")
    assert len(failed) == 1
    assert failed[0].reason.startswith("lease_expired")
    assert failed[0].will_retry is True
    assert failed[0].attempt == 1
    assert names[failed[0].agent_id] == "Ada"
    # the agent was marked error and its sandbox reconnected (no new sandbox)
    ada = next(i for i, n in names.items() if n == "Ada")
    ada_statuses = [p.status for p in await w.payloads("agent.status_changed") if p.agent_id == ada]
    assert "error" in ada_statuses
    assert ada_statuses[ada_statuses.index("error") + 1] == "idle"
    assert any(p.error == "task lease expired" for p in await w.payloads("agent.error"))
    assert len([p for p in await w.payloads("agent.sandbox_ready") if p.agent_id == ada]) == 1
    assert order_of(events, "task.accepted") == ["T1"]


async def test_max_attempts_fails_task_triggers_supervisor_and_fails_session() -> None:
    model = TaskModel({"T2": Behavior(fail=True)})
    w = await open_session(edges={"T1": [], "T2": [], "T3": ["T2"]}, agent_count=2, model=model)
    try:
        await w.wait_for("session.failed")
        await w.finished()
        events = await w.events()
    finally:
        await w.stop()
    failed = [p for p in await w.payloads("task.failed") if p.task_key == "T2"]
    assert [(p.attempt, p.will_retry) for p in failed] == [(1, True), (2, False)]
    triggers = await w.payloads("supervisor.triggered")
    assert [(p.reason, p.task_key) for p in triggers] == [("task_failed", "T2")]
    assert "T2" in (await w.payloads("session.failed"))[0].reason
    assert order_of(events, "task.started").count("T2") == 2
    # T1 finished; T3 (blocked by T2) was never started and is cancelled at the end
    assert "T1" in order_of(events, "task.accepted")
    assert "T3" not in order_of(events, "task.started")
    assert "T3" in order_of(events, "task.cancelled")
    assert [p.reason for p in await w.payloads("agent.stopped")] == ["session failed"] * 2


async def test_failed_task_with_supervisor_enabled_waits_instead_of_failing() -> None:
    model = TaskModel({"T1": Behavior(fail=True)})
    w = await open_session(
        edges={"T1": []},
        agent_count=1,
        model=model,
        scheduler_config=replace(FAST_SCHEDULER, supervisor_enabled=True),
    )
    try:
        await w.wait_for("supervisor.triggered")
        await asyncio.sleep(0.3)
        assert await w.payloads("session.failed") == []
    finally:
        await w.stop()
    assert [p.reason for p in await w.payloads("session.stopped")] == ["user"]


async def test_budget_warning_then_stop() -> None:
    model = TaskModel({k: Behavior(steps=100) for k in ("T1", "T2", "T3")})
    w = await open_session(
        edges={"T1": [], "T2": [], "T3": ["T1"]},
        agent_count=2,
        model=model,
        budgets=Budgets(max_total_steps=10),
    )
    try:
        await w.wait_for("session.stopped")
        await w.finished()
        events = await w.events()
    finally:
        await w.stop()
    warnings = await w.payloads("session.budget_warning")
    assert [(p.metric, p.limit) for p in warnings] == [("total_steps", 10.0)]
    assert 8 <= warnings[0].used < 10
    exceeded = await w.payloads("session.budget_exceeded")
    assert [p.metric for p in exceeded] == ["total_steps"]
    assert [p.reason for p in await w.payloads("session.stopped")] == ["budget"]
    # running tasks failed with reason budget (never retried), the rest cancelled
    failed = await w.payloads("task.failed")
    assert {p.task_key for p in failed} == {"T1", "T2"}
    assert all(p.reason.startswith("budget") and not p.will_retry for p in failed)
    assert order_of(events, "task.cancelled") == ["T3"]
    assert await w.payloads("session.failed") == []
    assert await w.payloads("supervisor.triggered") == []
    steps = len([e for e in events if e.type == "agent.step"])
    assert steps <= 11  # one step may be in flight in each runtime at the limit
    # no step after the stop
    stop_seq = next(e.seq for e in events if e.type == "session.stopped")
    assert all(e.seq < stop_seq for e in events if e.type == "agent.step")


async def test_user_stop_cancels_tasks_and_kills_sandboxes() -> None:
    model = TaskModel({"T1": Behavior(steps=500)})
    w = await open_session(edges={"T1": [], "T2": ["T1"]}, agent_count=2, model=model)
    await w.wait_for("agent.step")
    await w.stop()
    events = await w.events()
    assert [p.reason for p in await w.payloads("session.stopped")] == ["user"]
    assert sorted(order_of(events, "task.cancelled")) == ["T1", "T2"]
    assert await w.payloads("task.failed") == []
    assert len(await w.payloads("agent.stopped")) == 2
    assert all(sb.killed for sb in w.provider.sandboxes.values())
    assert w.scheduler.finished


async def test_all_queues_silent_exhaust_ack_reassignments_and_fail_session() -> None:
    w = await open_session(agent_count=2, deaf_agents=["Ada", "Brook"])
    try:
        await w.wait_for("session.failed", limit_s=3)
        await w.finished()
    finally:
        await w.stop()
    failures = await w.payloads("task.failed")
    assert [(p.reason, p.attempt, p.will_retry) for p in failures] == [
        ("assignment_not_acked", 1, True),
        ("assignment_not_acked", 2, False),
    ]
    assert len(await w.payloads("task.assigned")) == 4
    assert await w.payloads("task.started") == []
    assert w.scheduler.state.tasks["T1"].status == "failed"
    assert len(await w.payloads("supervisor.triggered")) == 1
    assert len(await w.payloads("session.failed")) == 1
