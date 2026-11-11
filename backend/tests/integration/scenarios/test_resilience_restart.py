"""T21 resilience: kill the runtime (``Runtime.crash``: no lease released, nothing
closed) at five points and start a new one on the same stores. After each restart:

* the session ends in the right state, with every task accepted exactly once;
* no task is lost or run twice in the same attempt, and attempt numbers follow;
* the event log stays gap-free;
* sandboxes are reconnected; none is re-created needlessly or left running.

Crash points: planning (planner call in flight), booting (sandbox create in flight),
mid-step (agent model call in flight), awaiting approval (agent blocked in
``request_approval``) and during review (supervisor call in flight)."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator

import pytest
from runtime.support import Behavior, PlannerModel, payload, until

from agentplatform.contracts.events import (
    ApprovalDecided,
    ApprovalRequested,
    TaskAccepted,
    TaskStarted,
)
from agentplatform.contracts.model_io import ModelRequest, ModelResponse
from agentplatform.contracts.sandbox import SandboxSpec
from agentplatform.testing import FakeSandbox, FakeSandboxProvider, make_tasks
from scenarios.harness import ScenarioWorld, ScriptedSupervisor, tool_response


class Hang:
    """Makes the next ``times`` calls of something hang until the runtime dies."""

    def __init__(self, times: int = 1) -> None:
        self.times = times
        self.entered = asyncio.Event()

    async def maybe(self) -> None:
        if self.times > 0:
            self.times -= 1
            self.entered.set()
            await asyncio.Event().wait()  # cancelled by the crash


class HangingPlanner(PlannerModel):
    def __init__(self, hang: Hang) -> None:
        super().__init__(make_tasks({"T1": [], "T2": ["T1"]}))
        self.hang = hang

    async def complete(self, req: ModelRequest) -> ModelResponse:
        await self.hang.maybe()
        return await super().complete(req)


class HangingSupervisor(ScriptedSupervisor):
    def __init__(self, hang: Hang) -> None:
        super().__init__()
        self.hang = hang

    async def complete(self, req: ModelRequest) -> ModelResponse:
        await self.hang.maybe()
        return await super().complete(req)


class HangingProvider(FakeSandboxProvider):
    """Creates the container, then hangs before returning it: what a runtime killed
    during ``docker run`` leaves behind (a running sandbox no event mentions)."""

    def __init__(self, hang: Hang) -> None:
        super().__init__()
        self.hang = hang

    async def create(self, spec: SandboxSpec) -> FakeSandbox:
        sandbox = await super().create(spec)
        await self.hang.maybe()
        return sandbox


@pytest.fixture
async def world() -> AsyncIterator[ScenarioWorld]:
    w = ScenarioWorld()
    yield w
    await w.close()


async def restart(world: ScenarioWorld, sid: str) -> None:
    first = world.runtimes[-1]
    await first.crash()
    world.directory.expire(sid)
    second = await world.runtime(f"rt-{len(world.runtimes)}")
    assert sid in second.hosts


def assert_clean_finish(world: ScenarioWorld, sid: str, keys: set[str]) -> None:
    accepted = Counter(
        payload(e, TaskAccepted).task_key for e in world.events(sid, "task.accepted")
    )
    assert accepted == Counter(dict.fromkeys(keys, 1))
    started = [payload(e, TaskStarted) for e in world.events(sid, "task.started")]
    pairs = Counter((s.task_key, s.attempt) for s in started)
    assert all(n == 1 for n in pairs.values()), pairs  # no attempt ran twice
    for key in keys:
        attempts = sorted(s.attempt for s in started if s.task_key == key)
        assert attempts == list(range(1, len(attempts) + 1))
    seqs = [e.seq for e in world.events(sid)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert len(world.events(sid, "agent.created")) == len(set(world.agent_ids(sid)))


async def start(world: ScenarioWorld, edges: dict[str, list[str]], agent_count: int = 1) -> str:
    world.models.planner.plans = [make_tasks(dict(edges))]
    session = await world.create_session(agent_count=agent_count)
    await world.send("command.plan_session", session.id)
    return session.id


async def test_crash_while_planning(world: ScenarioWorld) -> None:
    hang = Hang()
    world.models.planner = HangingPlanner(hang)
    await world.runtime("rt-a")
    session = await world.create_session()
    await world.send("command.plan_session", session.id)
    await asyncio.wait_for(hang.entered.wait(), 5)

    await restart(world, session.id)
    proposed = await world.wait_event(session.id, "session.plan_proposed", limit_s=10)
    await asyncio.sleep(0.5)  # the redelivered command and the sweep must not plan again
    assert world.events(session.id, "session.plan_proposed") == [proposed]
    assert world.directory.known[session.id].session.status == "awaiting_approval"
    await world.send("command.approve_plan", session.id, plan_version=1)
    await world.wait_event(session.id, "session.completed", limit_s=20)
    assert_clean_finish(world, session.id, {"T1", "T2"})


async def test_crash_while_booting(world: ScenarioWorld) -> None:
    hang = Hang()
    world.provider = HangingProvider(hang)
    await world.runtime("rt-a")
    sid = await start(world, {"T1": [], "T2": ["T1"]})
    await world.wait_event(sid, "session.plan_proposed")
    await world.send("command.approve_plan", sid, plan_version=1)
    await asyncio.wait_for(hang.entered.wait(), 5)
    assert not world.events(sid, "agent.sandbox_ready")

    await restart(world, sid)
    await world.wait_event(sid, "session.completed", limit_s=20)
    assert_clean_finish(world, sid, {"T1", "T2"})
    ready = world.events(sid, "agent.sandbox_ready")
    assert len(ready) == 1
    # the half-booted sandbox of the dead runtime must not keep running
    alive = [s for s in world.provider.sandboxes.values() if not s.killed]
    assert [s.id for s in alive] == [world.sandbox_of(sid, world.agent_ids(sid)[0]).id]


async def test_crash_mid_step(world: ScenarioWorld) -> None:
    world.agent_model.behaviors = {"T1": Behavior(steps=6, latency_s=0.3)}
    await world.runtime("rt-a")
    sid = await start(world, {"T1": [], "T2": ["T1"]})
    await world.wait_event(sid, "session.plan_proposed")
    await world.send("command.approve_plan", sid, plan_version=1)
    await world.wait_event(sid, "agent.step")
    await until(lambda: len(world.agent_model.requests) >= 2)  # a model call is in flight
    steps_before = len(world.events(sid, "agent.step"))

    await restart(world, sid)
    world.agent_model.behaviors = {"T1": Behavior(steps=4)}
    await world.wait_event(sid, "session.completed", limit_s=20)
    assert_clean_finish(world, sid, {"T1", "T2"})
    assert len(world.events(sid, "agent.step")) > steps_before
    started = [payload(e, TaskStarted) for e in world.events(sid, "task.started")]
    assert [s.attempt for s in started if s.task_key == "T1"] == [1, 2]
    assert len(world.provider.specs) == 1  # reconnected, not re-created


async def test_crash_while_awaiting_approval(world: ScenarioWorld) -> None:
    m = world.agent_model
    m.behaviors = {"T1": Behavior(steps=5)}
    m.script(
        "T1", 2, tool_response("request_approval", action_summary="Pay the invoice", risk="high")
    )
    await world.runtime("rt-a")
    sid = await start(world, {"T1": []})
    await world.wait_event(sid, "session.plan_proposed")
    await world.send("command.approve_plan", sid, plan_version=1)
    requested = await world.wait_event(sid, "approval.requested")
    approval = payload(requested, ApprovalRequested).approval

    await restart(world, sid)
    decided = await world.wait_event(sid, "approval.decided", limit_s=10)
    assert payload(decided, ApprovalDecided).approval.id == approval.id
    assert payload(decided, ApprovalDecided).approval.status == "expired"  # nobody waits for it
    # a late click on the stale card changes nothing
    await world.send("command.decide_approval", sid, approval_id=approval.id, decision="approve")
    # the re-run (attempt 2) asks again rather than acting on the stale request
    again = await world.wait_event(
        sid,
        "approval.requested",
        lambda e: payload(e, ApprovalRequested).approval.id != approval.id,
        limit_s=10,
    )
    second = payload(again, ApprovalRequested).approval
    await world.send("command.decide_approval", sid, approval_id=second.id, decision="approve")
    await world.wait_event(sid, "session.completed", limit_s=20)
    decisions = [
        payload(e, ApprovalDecided).approval for e in world.events(sid, "approval.decided")
    ]
    assert [(a.id, a.status) for a in decisions] == [
        (approval.id, "expired"),
        (second.id, "approved"),
    ]
    assert_clean_finish(world, sid, {"T1"})
    assert any(c.tool_results == ("Approved: Pay the invoice",) for c in m.of("T1"))


async def test_crash_during_review(world: ScenarioWorld) -> None:
    hang = Hang()
    supervisor = HangingSupervisor(hang)
    world.supervisor_model = supervisor
    world.models.supervisor = supervisor
    await world.runtime("rt-a")
    sid = await start(world, {"T1": [], "T2": ["T1"]})
    await world.wait_event(sid, "session.plan_proposed")
    await world.send("command.approve_plan", sid, plan_version=1)
    await world.wait_event(sid, "task.result_submitted")
    await asyncio.wait_for(hang.entered.wait(), 5)
    assert not world.events(sid, "task.accepted")

    await restart(world, sid)
    await world.wait_event(sid, "session.completed", limit_s=20)
    assert_clean_finish(world, sid, {"T1", "T2"})
    started = Counter(payload(e, TaskStarted).task_key for e in world.events(sid, "task.started"))
    assert started == Counter({"T1": 1, "T2": 1})  # the submitted result was not redone
