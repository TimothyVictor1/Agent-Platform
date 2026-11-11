"""Scenario 9 (SPEC §21, §15.2): ``request_approval`` blocks the agent until the user
decides; a denial with a note is delivered as "Denied: … Do this instead: …"."""

from __future__ import annotations

import asyncio

from runtime.support import Behavior, payload

from agentplatform.contracts.events import ApprovalDecided, ApprovalRequested
from scenarios.harness import ScenarioWorld, tool_response


async def test_approval_blocks_until_denied_and_the_note_reaches_the_agent(
    world: ScenarioWorld,
) -> None:
    m = world.agent_model
    m.behaviors = {"T1": Behavior(steps=5)}
    m.script(
        "T1",
        2,
        tool_response("request_approval", action_summary="Submit the contact form", risk="medium"),
    )
    await world.runtime()
    sid = await world.approved_session({"T1": []})
    requested = await world.wait_event(sid, "approval.requested")
    approval = payload(requested, ApprovalRequested).approval
    assert approval.status == "pending"
    assert approval.frame_id  # the card carries a screenshot

    calls_while_pending = len(m.of("T1"))
    await asyncio.sleep(0.5)
    assert len(m.of("T1")) == calls_while_pending == 2  # blocked: no further step
    assert not world.events(sid, "task.result_submitted")

    await world.send(
        "command.decide_approval",
        sid,
        approval_id=approval.id,
        decision="deny",
        note="Save it as a draft instead",
    )
    decided = await world.wait_event(sid, "approval.decided")
    await world.wait_event(sid, "session.completed", limit_s=20)

    assert payload(decided, ApprovalDecided).approval.status == "denied"
    third = m.of("T1")[2]
    assert third.tool_results == (
        "Denied: Submit the contact form. Do this instead: Save it as a draft instead",
    )
    # a second decision is refused (race-safe): still exactly one decided event
    await world.send("command.decide_approval", sid, approval_id=approval.id, decision="approve")
    await asyncio.sleep(0.3)
    assert len(world.events(sid, "approval.decided")) == 1
