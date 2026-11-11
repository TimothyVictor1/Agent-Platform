"""Scenario 1 (SPEC §21): a single agent completes a task and submits a valid result.

An invalid result (missing output) is refused with an ``ERROR`` the model sees; the
valid one is submitted, reviewed by the supervisor and accepted; the session completes
with a gap-free log and stored frames."""

from __future__ import annotations

from agentplatform.contracts.events import TaskAccepted, TaskResultSubmitted

from runtime.support import payload
from scenarios.harness import ScenarioWorld, submit, tool_response


async def test_single_agent_submits_a_valid_result(world: ScenarioWorld) -> None:
    m = world.agent_model
    m.script("T1", 4, tool_response("submit_result", summary="half", outputs={}))
    m.script("T1", 5, submit("T1", result="Example Domain"))
    for step in (1, 2, 3):
        m.script("T1", step, tool_response("run_command", cmd=f"echo {step}"))
    await world.runtime()
    sid = await world.approved_session({"T1": []})
    await world.wait_event(sid, "session.completed", limit_s=15)

    # the invalid submit came back as an error, the next step fixed it
    fifth = m.of("T1")[4]
    assert any(r.startswith("ERROR") and "missing output 'result'" in r for r in fifth.tool_results)
    # submit_result is hidden on steps 1-3 (SPEC §10.5 phase gating)
    assert all("submit_result" not in c.tools for c in m.of("T1")[:3])

    submitted = [
        payload(e, TaskResultSubmitted) for e in world.events(sid, "task.result_submitted")
    ]
    assert len(submitted) == 1
    assert submitted[0].result.outputs == {"result": "Example Domain"}
    accepted = [payload(e, TaskAccepted) for e in world.events(sid, "task.accepted")]
    assert [a.task_key for a in accepted] == ["T1"]
    assert world.events(sid, "task.accepted")[0].actor.type == "supervisor"

    assert len(world.agent_ids(sid)) == 1
    assert await world.frames.list()  # replay frames were stored
    seqs = [e.seq for e in world.events(sid)]
    assert seqs == list(range(1, len(seqs) + 1))
