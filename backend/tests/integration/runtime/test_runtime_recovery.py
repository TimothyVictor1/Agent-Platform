"""Restarts, session leases and sweeps of the runtime with fakes (T13)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from agentplatform.contracts.events import (
    SessionPlanProposed,
    TaskAccepted,
    TaskFailed,
    TaskStarted,
)
from agentplatform.testing import make_tasks

from .support import Behavior, World, payload, until


@pytest.fixture
async def world() -> AsyncIterator[World]:
    w = World()
    yield w
    await w.close()


async def start_session(world: World, edges: dict[str, list[str]], agent_count: int = 1) -> str:
    world.models.planner.plans = [make_tasks(dict(edges))]
    session = await world.create_session(agent_count=agent_count)
    await world.send("command.plan_session", session.id)
    await world.wait_event(session.id, "session.plan_proposed")
    await world.send("command.approve_plan", session.id, plan_version=1)
    return session.id


async def test_restart_mid_task_completes_with_the_next_attempt(world: World) -> None:
    world.models.agent.behaviors = {"T1": Behavior(steps=30, latency_s=0.02)}
    first = await world.runtime("rt-a")
    sid = await start_session(world, {"T1": [], "T2": ["T1"]})
    await world.wait_event(sid, "agent.step")
    sandbox_ids = set(world.provider.sandboxes)

    await first.crash()  # killed: no lease released, sandboxes left running
    world.directory.expire(sid)  # ... and its session lease ran out

    second = await world.runtime("rt-b")
    assert sid in second.hosts
    await world.wait_event(sid, "session.completed", limit_s=20)

    starts = [payload(e, TaskStarted) for e in world.events(sid, "task.started")]
    t1 = [s.attempt for s in starts if s.task_key == "T1"]
    assert t1 == [1, 2]  # the interrupted run counts as an attempt
    failed = [payload(e, TaskFailed) for e in world.events(sid, "task.failed")]
    assert [(f.task_key, f.attempt, f.will_retry) for f in failed] == [("T1", 1, True)]
    accepted = [payload(e, TaskAccepted).task_key for e in world.events(sid, "task.accepted")]
    assert sorted(accepted) == ["T1", "T2"]
    # the sandbox was reconnected, not re-created
    assert set(world.provider.sandboxes) == sandbox_ids
    seqs = [e.seq for e in world.events(sid)]
    assert seqs == list(range(1, len(seqs) + 1))


async def test_takeover_survives_a_restart(world: World) -> None:
    world.models.agent.behaviors = {"T1": Behavior(steps=12, latency_s=0.02)}
    first = await world.runtime("rt-a")
    sid = await start_session(world, {"T1": []})
    await world.wait_event(sid, "agent.step")
    agent_id = world.agent_ids(sid)[0]
    await world.send("command.takeover", sid, agent_id=agent_id)
    await world.wait_event(sid, "user.takeover_started")
    await until(lambda: world.agent_status(sid, agent_id) == "paused_by_user")
    await first.hosts[sid].rt.controls.flush()

    await first.crash()
    world.directory.expire(sid)
    second = await world.runtime("rt-b")
    host = second.hosts[sid]
    assert host.rt.controls.mode(agent_id) == "takeover"
    pool = host.rt.pool
    await until(lambda: pool.has(agent_id) and pool.runtime(agent_id) is not None, limit_s=10)
    runtime = pool.runtime(agent_id)
    assert runtime is not None
    assert runtime.control.mode == "takeover"  # restored before the agent runs again

    await world.send("command.release", sid, agent_id=agent_id)
    await world.wait_event(sid, "user.takeover_ended")
    await world.wait_event(sid, "session.completed", limit_s=20)


async def test_one_session_has_one_owner(world: World) -> None:
    a = await world.runtime("rt-a")
    b = await world.runtime("rt-b")
    session = await world.create_session()
    await world.send("command.plan_session", session.id)
    await world.wait_event(session.id, "session.plan_proposed")
    owners = [rt.owner for rt in (a, b) if session.id in rt.hosts]
    assert len(owners) == 1
    assert world.directory.known[session.id].owner == owners[0]
    assert len(world.events(session.id, "session.plan_proposed")) == 1


async def test_a_runtime_that_loses_its_lease_stops_the_session(world: World) -> None:
    world.models.agent.behaviors = {"T1": Behavior(steps=200, latency_s=0.02)}
    rt = await world.runtime("rt-a")
    sid = await start_session(world, {"T1": []})
    await world.wait_event(sid, "agent.step")
    world.directory.expire(sid)
    assert await world.directory.claim(sid, "rt-other", 60)  # someone else took it
    await until(lambda: sid not in rt.hosts, limit_s=5)
    steps = len(world.events(sid, "agent.step"))
    await asyncio.sleep(0.3)
    assert world.directory.known[sid].owner == "rt-other"  # never released by the loser
    assert not any(s.killed for s in world.provider.sandboxes.values())  # left for adoption
    assert len(world.events(sid, "agent.step")) <= steps + 1


async def test_stuck_planning_is_planned_once(world: World) -> None:
    await world.runtime("rt-a")
    session = await world.create_session()  # the API crashed before enqueueing the command
    proposed = await world.wait_event(session.id, "session.plan_proposed", limit_s=5)
    assert proposed.causation_id is None  # no command caused it
    await world.send("command.plan_session", session.id)  # arrives after all
    await until(lambda: world.bus.pending_count("sessions:new") == 0)
    await asyncio.sleep(0.3)
    assert len(world.events(session.id, "session.plan_proposed")) == 1
    assert payload(proposed, SessionPlanProposed).plan_version == 1


async def test_graceful_shutdown_keeps_sandboxes_and_releases_leases(world: World) -> None:
    world.models.agent.behaviors = {"T1": Behavior(steps=200, latency_s=0.02)}
    rt = await world.runtime("rt-a")
    sid = await start_session(world, {"T1": []})
    await world.wait_event(sid, "agent.step")
    await rt.shutdown()
    assert world.directory.known[sid].owner is None
    assert world.provider.sandboxes
    assert not any(s.killed for s in world.provider.sandboxes.values())
    assert not world.events(sid, "session.stopped")
    # the next runtime adopts the session right away and finishes it
    world.models.agent.behaviors = {"T1": Behavior(steps=1)}
    nxt = await world.runtime("rt-b")
    assert sid in nxt.hosts
    await world.wait_event(sid, "session.completed", limit_s=20)
