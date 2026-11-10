"""Approval gate (SPEC §15.2): approve, deny, expire, races, cancellation, supervisor
flags, and the decision reaching the agent loop."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import pytest

from agentplatform.contracts.artifact import Approval
from agentplatform.contracts.common import Actor, utcnow
from agentplatform.contracts.model_io import ModelRequest
from agentplatform.errors import ConflictError, NotFoundError, NotSupportedError
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.loop import AgentRuntime, AgentRuntimeConfig
from agentplatform.runtime.hitl import APPROVAL_TTL_S, HitlApprovalGate, decision_text
from agentplatform.testing import (
    FakeModel,
    FakeSandbox,
    InMemoryArtifactStore,
    make_agent,
    make_session,
    make_task,
    tool_response,
)

from .fakes import until

USER = Actor.user("u1", "Tim")
SESSION = "s-1"


def _request(gate: HitlApprovalGate, **kw: Any) -> asyncio.Task[Approval]:
    args: dict[str, Any] = {
        "session_id": SESSION,
        "agent_id": "a1",
        "task_key": "T1",
        "action_summary": "Send the summary email to the customer",
        "risk": "high",
        "frame_id": "frame-7",
    }
    args.update(kw)
    return asyncio.create_task(gate.request(**args))


async def _pending(aw: Any, n: int = 1) -> list[Approval]:
    await until(lambda: len(aw.approvals.approvals) >= n)
    return list(aw.approvals.approvals.values())


async def test_default_ttl_is_15_minutes(aw: Any) -> None:
    assert APPROVAL_TTL_S == 900
    gate = aw.make_gate()
    task = _request(gate)
    (approval,) = await _pending(aw)
    assert approval.expires_at is not None
    assert approval.expires_at - approval.created_at == timedelta(minutes=15)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_approve(aw: Any) -> None:
    gate = aw.make_gate(agent_name=lambda agent_id: "Ada")
    task = _request(gate)
    (pending,) = await _pending(aw)
    assert pending.status == "pending"
    decided = await gate.decide(pending.id, "approve", by=USER, note="go ahead")
    result = await task
    assert result == decided
    assert (result.status, result.decided_by, result.note) == ("approved", USER, "go ahead")
    assert aw.approvals.approvals[pending.id].status == "approved"
    (req_event,) = await aw.events(SESSION, "approval.requested")
    assert req_event.payload.approval.frame_id == "frame-7"
    assert req_event.actor == Actor.agent("a1", "Ada")
    (dec_event,) = await aw.events(SESSION, "approval.decided")
    assert dec_event.actor == USER
    assert dec_event.correlation_id == pending.id
    assert (
        decision_text(result) == "Approved: Send the summary email to the customer. Note: go ahead"
    )


async def test_deny(aw: Any) -> None:
    gate = aw.make_gate()
    task = _request(gate)
    (pending,) = await _pending(aw)
    await gate.decide(pending.id, "deny", by=USER, note="save it as a draft instead")
    result = await task
    assert result.status == "denied"
    assert decision_text(result) == (
        "Denied: Send the summary email to the customer. "
        "Do this instead: save it as a draft instead"
    )


async def test_expire(aw: Any) -> None:
    gate = aw.make_gate(ttl_s=0.05)
    result = await _request(gate)
    assert result.status == "expired"
    assert result.decided_by == Actor.system()
    assert "expired; continue without doing it" in decision_text(result)
    (decided,) = await aw.payloads(SESSION, "approval.decided")
    assert decided.approval.status == "expired"
    with pytest.raises(ConflictError, match="already expired"):
        await gate.decide(result.id, "approve", by=USER)


async def test_second_decision_conflicts_and_writes_nothing(aw: Any) -> None:
    gate = aw.make_gate()
    task = _request(gate)
    (pending,) = await _pending(aw)
    results = await asyncio.gather(
        gate.decide(pending.id, "approve", by=USER),
        gate.decide(pending.id, "deny", by=USER),
        return_exceptions=True,
    )
    assert sum(isinstance(r, ConflictError) for r in results) == 1
    assert (await task).status == "approved"
    assert len(await aw.events(SESSION, "approval.decided")) == 1


async def test_store_conflict_from_another_process_writes_no_event(aw: Any) -> None:
    """Two runtimes sharing a store: the second decision fails inside the event's
    transaction, so no second ``approval.decided`` is written."""
    gate, other = aw.make_gate(), aw.make_gate()
    task = _request(gate)
    (pending,) = await _pending(aw)
    await other.decide(pending.id, "deny", by=USER)  # other process, no waiter there
    with pytest.raises(ConflictError):
        await gate.decide(pending.id, "approve", by=USER)
    assert len(await aw.events(SESSION, "approval.decided")) == 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_unknown_or_foreign_approval_is_not_found(aw: Any) -> None:
    gate = aw.make_gate()
    with pytest.raises(NotFoundError):
        await gate.decide("nope", "approve", by=USER)
    task = _request(gate)
    (pending,) = await _pending(aw)
    with pytest.raises(NotFoundError):
        await gate.decide(pending.id, "approve", by=USER, session_id="other-session")
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def test_decision_after_the_deadline_expires_instead(aw: Any) -> None:
    clock = [utcnow()]

    def now() -> datetime:
        return clock[0]

    gate = aw.make_gate(now=now)
    task = _request(gate)
    (pending,) = await _pending(aw)
    clock[0] += timedelta(minutes=16)  # the wait timer has not fired yet (e.g. suspended)
    with pytest.raises(ConflictError, match="expired"):
        await gate.decide(pending.id, "approve", by=USER)
    assert (await task).status == "expired"


async def test_cancelled_wait_expires_the_approval(aw: Any) -> None:
    gate = aw.make_gate()
    task = _request(gate)
    (pending,) = await _pending(aw)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await until(lambda: aw.approvals.approvals[pending.id].status == "expired")
    (decided,) = await aw.payloads(SESSION, "approval.decided")
    assert decided.approval.note == "the agent stopped waiting"


async def test_expire_pending_closes_every_open_approval(aw: Any) -> None:
    gate = aw.make_gate()
    tasks = [_request(gate, agent_id=f"a{i}") for i in range(3)]
    other = _request(gate, session_id="s-2")
    await _pending(aw, 4)
    expired = await gate.expire_pending(SESSION)
    assert len(expired) == 3
    assert {t.status for t in await asyncio.gather(*tasks)} == {"expired"}
    assert not other.done()
    other.cancel()
    await asyncio.gather(other, return_exceptions=True)


# -- supervisor flag ----------------------------------------------------------------------


async def test_flag_notes_the_agent_and_delivers_the_decision_as_a_note(aw: Any) -> None:
    gate = aw.make_gate()
    flagged = await gate.flag(
        session_id=SESSION, agent_id="a2", action_summary="Delete /data/report.xlsx", risk="medium"
    )
    assert flagged.status == "pending"
    (req,) = await aw.events(SESSION, "approval.requested")
    assert req.actor == Actor.supervisor()
    (wait_note,) = aw.board.entries
    assert wait_note.to == ["a2"]
    assert wait_note.author == Actor.supervisor()
    assert "Wait for the user's approval" in wait_note.title
    await gate.decide(flagged.id, "deny", by=USER, note="keep the file")
    decision_note = aw.board.entries[-1]
    assert decision_note.to == ["a2"]
    assert decision_note.author == USER
    text = decision_note.title + decision_note.body
    assert "Denied: Delete /data/report.xlsx" in text
    assert "keep the file" in text


async def test_flag_expiry_is_delivered_too(aw: Any) -> None:
    gate = aw.make_gate(ttl_s=0.05)
    flagged = await gate.flag(
        session_id=SESSION, agent_id="a2", action_summary="Buy the plan", risk="high"
    )
    await until(lambda: aw.approvals.approvals[flagged.id].status == "expired")
    await until(lambda: len(aw.board.entries) == 2)
    note = aw.board.entries[-1]
    assert note.author == Actor.supervisor()
    assert "expired" in note.title + note.body


async def test_flag_needs_a_board(aw: Any) -> None:
    gate = aw.make_gate(board=None)
    with pytest.raises(NotSupportedError):
        await gate.flag(session_id=SESSION, agent_id="a2", action_summary="x", risk="low")
    assert await aw.events(SESSION) == []


# -- through the agent loop ---------------------------------------------------------------


async def test_decision_reaches_the_agent_loop(aw: Any) -> None:
    session = make_session(agent_count=1)
    agent = make_agent(0, session_id=session.id)
    gate = aw.make_gate()
    model = FakeModel(
        {
            agent.name: [
                tool_response("request_approval", action_summary="Submit the form", risk="high"),
                tool_response("fail_task", reason="test over"),
            ]
        }
    )
    runtime = AgentRuntime(
        session=session,
        agent=agent,
        sandbox=FakeSandbox(),
        model=model,
        events=EventRecorder(aw.store, aw.bus),
        bus=aw.bus,
        frames=InMemoryArtifactStore(),
        config=AgentRuntimeConfig(error_backoff_s=0.0),
        approvals=gate,
    )
    run = asyncio.create_task(runtime.run_task(make_task(session_id=session.id)))
    await _pending(aw)
    (pending,) = aw.approvals.approvals.values()
    assert pending.session_id == session.id
    assert pending.agent_id == agent.id
    assert pending.frame_id is not None  # the frame the agent saw when it asked
    statuses = [p.status for p in await aw.payloads(session.id, "agent.status_changed")]
    assert statuses[-1] == "awaiting_approval"
    await gate.decide(pending.id, "approve", by=USER)
    outcome = await run
    assert outcome.status == "failed"
    second: ModelRequest = model.calls[1].request
    tool_results = [m.text_content() for m in second.messages if m.role == "tool"]
    assert tool_results == ["Approved: Submit the form"]
    assert (
        "Approved: Submit the form"
        in [m for m in second.messages if m.role == "user"][-1].text_content()
    )
