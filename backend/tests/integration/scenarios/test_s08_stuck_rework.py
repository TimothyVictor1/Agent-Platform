"""Scenario 8 (SPEC §21): stuck detection → supervisor trigger → the supervisor's
message is delivered in the stuck agent's next digest; the agent then finishes."""

from __future__ import annotations

from runtime.support import payload

from agentplatform.contracts.events import AgentStuckDetected, BoardEntryPosted
from scenarios.harness import AgentCall, ScenarioWorld, ScriptedSupervisor, submit, tool_response

HINT = "Use the search box at the top instead of clicking the logo"


async def test_stuck_agent_gets_the_supervisors_message_and_recovers() -> None:
    world = ScenarioWorld(supervisor_model=ScriptedSupervisor(stuck_message=HINT))
    try:
        m = world.agent_model
        seen: list[int] = []

        def step(call: AgentCall) -> object:
            if HINT in call.inbox:
                seen.append(call.step)
            if seen and call.step > max(seen[0], 3):
                return submit("T1")
            return tool_response("click", x=100, y=100)  # the same click, again and again

        for n in range(1, 60):
            m.script("T1", n, step)  # type: ignore[arg-type]
        await world.runtime()
        sid = await world.approved_session({"T1": []})
        await world.wait_event(sid, "session.completed", limit_s=20)

        stuck = [payload(e, AgentStuckDetected) for e in world.events(sid, "agent.stuck_detected")]
        assert stuck
        assert stuck[0].reason in ("repetition", "screen_unchanged")
        triggers = [
            getattr(e.payload, "reason", None) for e in world.events(sid, "supervisor.triggered")
        ]
        assert "stuck" in triggers
        notes = [
            payload(e, BoardEntryPosted).entry
            for e in world.events(sid, "board.entry_posted")
            if payload(e, BoardEntryPosted).entry.author.type == "supervisor"
        ]
        agent_id = world.agent_ids(sid)[0]
        assert len(notes) == 1
        assert (notes[0].kind, notes[0].to) == ("note", [agent_id])  # direct to the stuck agent
        assert HINT in f"{notes[0].title} {notes[0].body}"
        assert len(seen) == 1  # delivered once, in the next digest after the post
        stuck_seq = world.events(sid, "agent.stuck_detected")[0].seq
        posted_seq = next(
            e.seq
            for e in world.events(sid, "board.entry_posted")
            if payload(e, BoardEntryPosted).entry.author.type == "supervisor"
        )
        assert stuck_seq < posted_seq
        assert len(world.events(sid, "task.accepted")) == 1
    finally:
        await world.close()
