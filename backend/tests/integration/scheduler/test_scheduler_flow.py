"""Scheduler happy paths (SPEC §12): DAG order, affinity, addressed delivery,
follow-ups, modes, upstream inputs, lease TTL wiring and restart."""

from __future__ import annotations

from typing import Any

from agentplatform.bus.base import agent_queue
from agentplatform.contracts.events import TaskAssignedEvent
from agentplatform.runtime.scheduler import SchedulerConfig
from agentplatform.testing import FakeSandboxProvider, make_task_draft

from .harness import (
    FAST_POOL,
    Behavior,
    TaskModel,
    build_world,
    open_session,
    order_of,
    seq_of,
    until,
)

SPEC_DAG: dict[str, list[str]] = {"T1": [], "T2": [], "T3": ["T1", "T2"], "T4": ["T3"]}


async def test_three_agents_four_task_dag_completes_in_dependency_order() -> None:
    w = await open_session(edges=SPEC_DAG, agent_count=3)
    try:
        await w.wait_for("session.completed")
        events = await w.events()
    finally:
        await w.stop()

    assert len(await w.payloads("agent.created")) == 3
    assert sorted(order_of(events, "task.accepted")) == ["T1", "T2", "T3", "T4"]
    # T3 starts only after T1 and T2 are done, T4 only after T3
    assert seq_of(events, "task.started", "T3") > seq_of(events, "task.accepted", "T1")
    assert seq_of(events, "task.started", "T3") > seq_of(events, "task.accepted", "T2")
    assert seq_of(events, "task.started", "T4") > seq_of(events, "task.accepted", "T3")
    # T1 and T2 ran in parallel on different agents
    assigned = {p.task_key: p.agent_id for p in await w.payloads("task.assigned")}
    assert assigned["T1"] != assigned["T2"]
    # every assignment was acked and started exactly once
    for key in SPEC_DAG:
        assert order_of(events, "task.assignment_acked").count(key) == 1
        assert order_of(events, "task.started").count(key) == 1
    # the scheduler never emitted session.created (the session existed already)
    assert [e.actor.type for e in events if e.type == "session.created"] == ["user"]
    assert (await w.payloads("session.completed"))[0].summary == "4 of 4 tasks done"
    # status sequence of a task as seen by the UI
    t4 = [
        e.type
        for e in events
        if e.type.startswith("task.") and getattr(e.payload, "task_key", None) == "T4"
    ]
    assert t4 == [
        "task.ready",
        "task.assigned",
        "task.assignment_acked",
        "task.started",
        "task.result_submitted",
        "task.accepted",
    ]


async def test_upstream_outputs_reach_downstream_task() -> None:
    w = await open_session(edges={"T1": [], "T2": ["T1"]}, agent_count=2)
    try:
        await w.wait_for("session.completed")
    finally:
        await w.stop()
    t2_system = next(c.system for c in w.model.calls if c.task_key == "T2")
    t1_agent = next(iter(w.model.agents_for("T1")))
    assert f'T1.result = "T1 by {t1_agent}"' in t2_system


async def test_affinity_prefers_the_agent_that_did_the_upstream_task() -> None:
    # T1 (Ada) finishes first and stays idle; when T3 (Cyra) is done, T5 becomes ready
    # with Ada and Cyra idle. Affinity must pick Cyra although Ada comes first.
    edges = {"T1": [], "T2": [], "T3": [], "T4": ["T1", "T2"], "T5": ["T3"]}
    model = TaskModel({"T1": Behavior(steps=4), "T2": Behavior(steps=60), "T3": Behavior(steps=12)})
    w = await open_session(edges=edges, agent_count=3, model=model)
    try:
        await w.wait_for("session.completed", limit_s=30)
    finally:
        await w.stop()
    names = {a.id: a.name for a in (p.agent for p in await w.payloads("agent.created"))}
    assigned = {p.task_key: names[p.agent_id] for p in await w.payloads("task.assigned")}
    assert assigned["T1"] == "Ada"
    assert assigned["T3"] == "Cyra"
    assert assigned["T5"] == "Cyra"


async def test_regression_three_idle_agents_one_followup_task_exactly_one_runs_it() -> None:
    """SPEC §12.4 regression: a follow-up task is delivered to exactly one agent."""
    first = [make_task_draft("T1")]
    w = await open_session(drafts=first, agent_count=3)
    try:
        await w.wait_for("session.completed")
        # all three agents idle, then a follow-up plan adds T2
        await w.wait_for("agent.status_changed", lambda p: p.status == "idle")
        creates_before = len(w.provider.specs)
        await w.approve([*first, make_task_draft("T2", depends_on=["T1"])], 2)
        await w.wait_for("task.accepted", lambda p: p.task_key == "T2")
        await w.wait_for("session.completed", lambda p: True)
        events = await w.events()
    finally:
        await w.stop()

    t2_deliveries = [
        (queue, item)
        for queue, item in w.bus.enqueued
        if isinstance(item, TaskAssignedEvent) and item.payload.task_key == "T2"
    ]
    assert len(t2_deliveries) == 1
    queue, item = t2_deliveries[0]
    assert queue == agent_queue(item.payload.agent_id)
    assert order_of(events, "task.assigned").count("T2") == 1
    assert order_of(events, "task.started").count("T2") == 1
    assert len(w.model.agents_for("T2")) == 1
    # scheduled without restarting agents: no new agents, no new sandboxes
    assert len(await w.payloads("agent.created")) == 3
    assert len(w.provider.specs) == creates_before
    created = [p.task for p in await w.payloads("task.created")]
    assert [(t.key, t.plan_version) for t in created] == [("T1", 1), ("T2", 2)]
    assert len(await w.payloads("session.completed")) == 2


async def test_solo_mode_uses_one_agent() -> None:
    w = await open_session(edges=SPEC_DAG, agent_count=3, mode="solo")
    try:
        await w.wait_for("session.completed")
    finally:
        await w.stop()
    assert [p.agent.name for p in await w.payloads("agent.created")] == ["Ada"]
    assert {c.agent for c in w.model.calls} == {"Ada"}
    assert len(await w.payloads("task.accepted")) == 4


async def _tools_seen(mode: Any) -> set[str]:
    w = await open_session(edges={"T1": []}, agent_count=1, mode=mode)
    try:
        await w.wait_for("session.completed")
    finally:
        await w.stop()
    return {name for c in w.model.calls for name in c.tools}


async def test_parallel_isolated_mode_has_no_coordination_tools() -> None:
    assert "post" in await _tools_seen("coordinated")  # the fake coordination tool
    isolated = await _tools_seen("parallel_isolated")
    assert "post" not in isolated
    assert "run_command" in isolated


async def test_real_lease_ttl_reaches_the_agent_runtime() -> None:
    """The runtime tolerates brief renewal failures only if it knows the TTL (45 s);
    the pool enforces it even when a factory forgets to pass it."""
    w = await open_session(start=False, pass_lease_ttl=False, scheduler_config=SchedulerConfig())
    pool = w.pool
    await pool.start(1)

    async def _has_runtime() -> bool:
        return pool.runtime(pool.agent_ids[0]) is not None

    try:
        await until(_has_runtime)
        runtime = pool.runtime(pool.agent_ids[0])
        assert runtime is not None
        assert runtime.config.lease_ttl_s == 45.0
    finally:
        await pool.shutdown("test over")


async def test_restart_adopts_agents_and_finishes_the_session() -> None:
    provider = FakeSandboxProvider()
    edges = {"T1": [], "T2": ["T1"]}
    model = TaskModel({"T2": Behavior(steps=8)})
    w = await open_session(edges=edges, agent_count=2, provider=provider, model=model)
    await w.wait_for("task.accepted", lambda p: p.task_key == "T1")
    await w.crash()
    sandboxes_before = len(provider.specs)

    # a new runtime: new pool + scheduler on the same store, bus and leases
    w2 = build_world(
        session=w.session,
        store=w.store,
        bus=w.bus,
        leases=w.leases,
        provider=provider,
        model=model,
        pool_config=FAST_POOL,
    )
    w2.start()
    try:
        await w2.wait_for("session.completed", limit_s=20)
        events = await w2.events()
    finally:
        await w2.stop()
    assert len([e for e in events if e.type == "agent.created"]) == 2  # adopted, not re-created
    assert len(provider.specs) == sandboxes_before  # sandboxes reconnected
    assert order_of(events, "task.accepted") == ["T1", "T2"]
