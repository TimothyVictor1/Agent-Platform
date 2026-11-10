"""HitlController (SPEC §10.7, §15.2): steering, takeover/release, command routing and
the kill switch with fakes."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest

from agentplatform.contracts.commands import (
    DecideApprovalCommand,
    PauseSessionCommand,
    ReleaseAgentCommand,
    SteerCommand,
    StopSessionCommand,
    TakeoverAgentCommand,
)
from agentplatform.contracts.common import Actor, utcnow
from agentplatform.contracts.session import Session
from agentplatform.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    NotSupportedError,
)
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.loop import AgentRuntime, AgentRuntimeConfig, LocalControlState
from agentplatform.runtime.hitl import STEER_NOTE_PREFIX, HitlController
from agentplatform.testing import (
    FakeModel,
    FakeSandbox,
    InMemoryArtifactStore,
    make_agent,
    make_session,
    make_task,
    tool_response,
)
from agentplatform.testing.images import make_png

from .fakes import FakeNotes, supervisor_triggers, until, until_async

USER = Actor.user("u1", "Tim")
RESUME_NOTE = "The user interacted with your screen. Re-observe carefully before acting."


class FakeScheduler:
    def __init__(self, delay_s: float = 0.0) -> None:
        self.stops: list[tuple[str, str | None]] = []
        self.delay_s = delay_s

    async def stop(self, reason: Any = "user", detail: str | None = None) -> None:
        self.stops.append((reason, detail))
        await asyncio.sleep(self.delay_s)


class FakeReplay:
    def __init__(self) -> None:
        self.flushed: list[str] = []

    async def flush(self, session_id: str) -> None:
        self.flushed.append(session_id)


def _controller(aw: Any, session: Session, **kw: Any) -> HitlController:
    kw.setdefault("board", aw.board)
    kw.setdefault("scheduler", FakeScheduler())
    return HitlController(session=session, events=aw.recorder, gate=aw.make_gate(), **kw)


def _cmd(cls: Any, session: Session, **kw: Any) -> Any:
    return cls(id="c1", session_id=session.id, issued_at=utcnow(), issued_by=USER, **kw)


# -- steering -----------------------------------------------------------------------------


async def test_steer_reaches_only_the_target_agent(aw: Any) -> None:
    session = make_session(agent_count=3)
    ctrl = _controller(aw, session, agent_ids=lambda: ["a1", "a2", "a3"])
    entry = await ctrl.steer("a2", "Use the EU price list, not the US one.", by=USER)
    assert entry is not None
    assert entry.to == ["a2"]
    assert entry.kind == "note"
    assert entry.author == USER
    assert entry.title == "Use the EU price list, not the US one."
    assert [e.id for e in aw.board.visible_to("a2")] == [entry.id]
    assert aw.board.visible_to("a1") == []
    assert aw.board.visible_to("a3") == []
    (steer,) = await aw.payloads(session.id, "user.steer")
    assert (steer.agent_id, steer.entry_id) == ("a2", entry.id)
    assert await supervisor_triggers(aw.store, session) == []


async def test_broadcast_steer_triggers_the_supervisor_exactly_once(aw: Any) -> None:
    session = make_session(agent_count=2)
    ctrl = _controller(aw, session, agent_ids=lambda: ["a1", "a2"])
    entry = await ctrl.steer(None, "Stop researching; write the report now.", by=USER)
    assert entry is not None
    assert entry.to is None
    assert aw.board.visible_to("a1") == aw.board.visible_to("a2") == [entry]
    (steer,) = await aw.payloads(session.id, "user.steer")
    assert steer.agent_id is None
    assert await aw.payloads(session.id, "supervisor.triggered") == []  # none emitted by T18
    (trigger,) = await supervisor_triggers(aw.store, session)  # T17's detection
    assert trigger.reason == "user_message"
    assert trigger.entry_ids == [entry.id]


async def test_isolated_steer_goes_through_the_note_channel(aw: Any) -> None:
    session = make_session(mode="parallel_isolated", agent_count=3)
    ctrl = _controller(aw, session, board=None)
    sinks = {f"a{i}": FakeNotes() for i in range(1, 3)}
    for agent_id, sink in sinks.items():
        ctrl.bind_control(agent_id, LocalControlState(), sink)
    assert await ctrl.steer("a2", "Use the EU\nprice list.", by=USER) is None
    assert sinks["a2"].notes == [STEER_NOTE_PREFIX + "Use the EU price list."]
    assert sinks["a1"].notes == []
    (steer,) = await aw.payloads(session.id, "user.steer")
    assert (steer.agent_id, steer.entry_id) == ("a2", None)
    assert await supervisor_triggers(aw.store, session) == []

    assert await ctrl.steer(None, "Wrap up now.", by=USER) is None
    assert all(s.notes[-1] == STEER_NOTE_PREFIX + "Wrap up now." for s in sinks.values())
    (trigger,) = await supervisor_triggers(aw.store, session)  # exactly one, from T17
    assert trigger.reason == "user_message"
    # refused only when no control is bound for the agent
    with pytest.raises(NotFoundError):
        await ctrl.steer("a3", "hi", by=USER)
    with pytest.raises(NotFoundError):
        await _controller(aw, session, board=None).steer(None, "hi", by=USER)
    assert aw.board.entries == []


async def test_isolated_steer_reaches_an_idle_agents_next_task(aw: Any) -> None:
    session = make_session(mode="parallel_isolated", agent_count=1)
    agent = make_agent(0, session_id=session.id)
    model = FakeModel(default=tool_response("fail_task", reason="done"))
    control = LocalControlState()
    runtime = AgentRuntime(
        session=session,
        agent=agent,
        sandbox=FakeSandbox(),
        model=model,
        events=EventRecorder(aw.store, aw.bus),
        bus=aw.bus,
        frames=InMemoryArtifactStore(),
        config=AgentRuntimeConfig(error_backoff_s=0.0),
        control=control,
    )
    ctrl = _controller(aw, session, board=None)
    ctrl.bind_control(agent.id, control, runtime)
    await ctrl.steer(agent.id, "Only compare EU vendors.", by=USER)  # agent is idle
    await runtime.run_task(make_task(session_id=session.id))
    users = [m for m in model.calls[0].request.messages if m.role == "user"]
    assert f"[system note] {STEER_NOTE_PREFIX}Only compare EU vendors." in users[-1].text_content()


async def test_long_steering_text_keeps_the_full_body(aw: Any) -> None:
    session = make_session()
    ctrl = _controller(aw, session, agent_ids=lambda: ["a1"])
    text = "Please " + "check every vendor carefully " * 10
    entry = await ctrl.steer("a1", text, by=USER)
    assert entry is not None
    assert len(entry.title) <= 120
    assert entry.body == text.strip()


async def test_steer_errors(aw: Any) -> None:
    session = make_session()
    ctrl = _controller(aw, session, agent_ids=lambda: ["a1"])
    with pytest.raises(NotFoundError):
        await ctrl.steer("ghost", "hi", by=USER)
    no_board = _controller(aw, session, board=None)
    with pytest.raises(NotSupportedError):
        await no_board.steer(None, "hi", by=USER)
    assert aw.board.entries == []


# -- takeover -----------------------------------------------------------------------------


def _screens(n: int = 60) -> list[bytes]:
    return [make_png(64, 40, seed=i) for i in range(n)]


async def test_takeover_pauses_the_loop_and_release_resumes_it(aw: Any) -> None:
    session = make_session(agent_count=1)
    agent = make_agent(0, session_id=session.id)
    sandbox = FakeSandbox(_screens())
    model = FakeModel(default=tool_response("run_command", cmd="echo hi"), latency_s=0.01)
    control = LocalControlState()
    runtime = AgentRuntime(
        session=session,
        agent=agent,
        sandbox=sandbox,
        model=model,
        events=EventRecorder(aw.store, aw.bus),
        bus=aw.bus,
        frames=InMemoryArtifactStore(),
        config=AgentRuntimeConfig(error_backoff_s=0.0, heartbeat_interval_s=0.05),
        control=control,
    )
    ctrl = _controller(aw, session)
    ctrl.bind_control(agent.id, control, runtime)
    run = asyncio.create_task(runtime.run_task(make_task(session_id=session.id)))
    try:
        await until(lambda: len(sandbox.actions) >= 2)
        await ctrl.takeover(agent.id, by=USER)
        assert control.mode == "takeover"

        async def paused() -> bool:
            statuses = await aw.payloads(session.id, "agent.status_changed")
            return bool(statuses) and statuses[-1].status == "paused_by_user"

        await until_async(paused)
        acted, asked = len(sandbox.actions), len(model.calls)
        await asyncio.sleep(0.3)
        assert len(sandbox.actions) == acted  # nothing reaches the user's screen
        assert len(model.calls) == asked
        with pytest.raises(ConflictError):
            await ctrl.takeover(agent.id, by=USER)

        await ctrl.release(agent.id, by=USER)
        await until(lambda: len(model.calls) > asked + 1)
        users = [m for m in model.calls[asked].request.messages if m.role == "user"]
        assert RESUME_NOTE in users[-1].text_content()
        with pytest.raises(ConflictError):
            await ctrl.release(agent.id, by=USER)
    finally:
        control.set("stop")
        await asyncio.wait_for(run, 5)
    started = await aw.events(session.id, "user.takeover_started")
    ended = await aw.events(session.id, "user.takeover_ended")
    assert [e.payload.agent_id for e in started + ended] == [agent.id, agent.id]
    assert started[0].actor == USER
    assert started[0].seq < ended[0].seq


async def test_takeover_of_unknown_or_stopped_agent(aw: Any) -> None:
    session = make_session()
    ctrl = _controller(aw, session)
    with pytest.raises(NotFoundError):
        await ctrl.takeover("ghost", by=USER)
    control = LocalControlState("stop")
    ctrl.bind_control("a1", control, FakeNotes())
    with pytest.raises(ConflictError, match="stopped"):
        await ctrl.takeover("a1", by=USER)


# -- commands -----------------------------------------------------------------------------


async def test_handle_routes_commands(aw: Any) -> None:
    session = make_session()
    scheduler = FakeScheduler()
    ctrl = _controller(aw, session, scheduler=scheduler, agent_ids=lambda: ["a1"])
    control = LocalControlState()
    ctrl.bind_control("a1", control, FakeNotes())
    assert await ctrl.handle(_cmd(SteerCommand, session, agent_id="a1", text="hi"))
    assert await ctrl.handle(_cmd(TakeoverAgentCommand, session, agent_id="a1"))
    modes = [control.mode]
    assert await ctrl.handle(_cmd(ReleaseAgentCommand, session, agent_id="a1"))
    modes.append(control.mode)
    assert modes == ["takeover", "run"]
    gate = ctrl._gate
    req = asyncio.create_task(
        gate.request(
            session_id=session.id,
            agent_id="a1",
            task_key=None,
            action_summary="Pay",
            risk="high",
            frame_id=None,
        )
    )
    await until(lambda: len(aw.approvals.approvals) == 1)
    (approval,) = aw.approvals.approvals.values()
    assert await ctrl.handle(
        _cmd(DecideApprovalCommand, session, approval_id=approval.id, decision="deny", note="no")
    )
    assert (await req).status == "denied"
    assert not await ctrl.handle(_cmd(PauseSessionCommand, session))
    assert await ctrl.handle(_cmd(StopSessionCommand, session))
    assert scheduler.stops == [("user", "stopped by the user")]
    other = make_session()
    with pytest.raises(InvalidRequestError):
        await ctrl.handle(_cmd(SteerCommand, other, text="hi"))


# -- kill switch --------------------------------------------------------------------------


async def test_kill_switch_stops_every_control_expires_approvals_and_flushes(aw: Any) -> None:
    session = make_session(agent_count=2)
    scheduler, replay = FakeScheduler(), FakeReplay()
    ctrl = _controller(aw, session, scheduler=scheduler, replay=replay)
    controls = [LocalControlState(), LocalControlState("takeover")]
    for i, c in enumerate(controls):
        ctrl.bind_control(f"a{i}", c, FakeNotes())
    req = asyncio.create_task(
        ctrl._gate.request(
            session_id=session.id,
            agent_id="a0",
            task_key="T1",
            action_summary="Send",
            risk="high",
            frame_id=None,
        )
    )
    await until(lambda: len(aw.approvals.approvals) == 1)
    first, second = await asyncio.gather(ctrl.stop(by=USER), ctrl.stop(by=USER))
    assert first == second  # idempotent
    assert first.within_deadline
    assert first.approvals_expired == 1
    assert [c.mode for c in controls] == ["stop", "stop"]
    assert (await req).status == "expired"
    assert scheduler.stops == [("user", "stopped by the user")]
    assert replay.flushed == [session.id]
    late = LocalControlState()
    ctrl.bind_control("a9", late, FakeNotes())  # an agent booting during the stop
    assert late.mode == "stop"
    with pytest.raises(ConflictError, match="stopping"):
        await ctrl.takeover("a0", by=USER)
    with pytest.raises(ConflictError, match="stopping"):
        await ctrl.steer(None, "hi", by=USER)


async def test_kill_switch_reports_a_slow_stop(aw: Any) -> None:
    ctrl = _controller(aw, make_session(), scheduler=FakeScheduler(0.2), stop_deadline_s=0.1)
    report = await ctrl.stop()
    assert not report.within_deadline
    assert report.elapsed_s >= 0.2


async def test_kill_switch_interrupts_a_slow_model_call_quickly(aw: Any) -> None:
    session = make_session(agent_count=1)
    agent = make_agent(0, session_id=session.id)
    model = FakeModel(default=tool_response("run_command", cmd="echo"), latency_s=60)
    control = LocalControlState()
    runtime = AgentRuntime(
        session=session,
        agent=agent,
        sandbox=FakeSandbox(),
        model=model,
        events=EventRecorder(aw.store, aw.bus),
        bus=aw.bus,
        frames=InMemoryArtifactStore(),
        config=AgentRuntimeConfig(error_backoff_s=0.0),
        control=control,
    )
    ctrl = _controller(aw, session)
    ctrl.bind_control(agent.id, control, runtime)
    run = asyncio.create_task(runtime.run_task(make_task(session_id=session.id)))
    await until(lambda: len(model.calls) == 1)
    t0 = time.monotonic()
    await ctrl.stop(by=USER)
    outcome = await asyncio.wait_for(run, 5)
    assert time.monotonic() - t0 < 2
    assert outcome.status == "stopped"
