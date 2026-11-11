"""Scenario 5 (SPEC §21): an agent's question is answered by the supervisor; the
answer is in the asker's next digest and the question is resolved."""

from __future__ import annotations

import asyncio

from runtime.support import Behavior, payload

from agentplatform.contracts.events import BoardEntryPosted, BoardEntryStatusChanged
from scenarios.harness import ScenarioWorld, ScriptedSupervisor, gate, multi_tool_response, shell


async def test_supervisor_answer_reaches_the_asker_and_resolves_the_question() -> None:
    world = ScenarioWorld(supervisor_model=ScriptedSupervisor(answers="Use yearly prices in EUR"))
    try:
        m = world.agent_model
        m.behaviors = {"T1": Behavior(steps=6)}
        m.script(
            "T1",
            2,
            multi_tool_response(
                ("post", {"kind": "question", "title": "Monthly or yearly prices?"}),
                ("run_command", {"cmd": "echo asked"}),
            ),
        )
        release = asyncio.Event()
        m.script("T1", 3, lambda _call: gate(release, shell("waiting")))
        await world.runtime()
        sid = await world.approved_session({"T1": []})

        def kind_is(kind: str):  # type: ignore[no-untyped-def]
            return lambda e: payload(e, BoardEntryPosted).entry.kind == kind

        q_event = await world.wait_event(sid, "board.entry_posted", kind_is("question"))
        question = payload(q_event, BoardEntryPosted).entry
        a_event = await world.wait_event(sid, "board.entry_posted", kind_is("answer"))
        answer = payload(a_event, BoardEntryPosted).entry
        release.set()
        await world.wait_event(sid, "session.completed", limit_s=20)

        assert answer.refs == [question.id]
        assert answer.author.type == "supervisor"
        resolved = [
            payload(e, BoardEntryStatusChanged)
            for e in world.events(sid, "board.entry_status_changed")
        ]
        assert [(r.entry_id, r.previous, r.status) for r in resolved] == [
            (question.id, "open", "resolved")
        ]
        triggers = [e for e in world.events(sid, "supervisor.triggered")]
        assert any(getattr(t.payload, "reason", None) == "question" for t in triggers)

        step4 = m.of("T1")[3]
        assert f"[#{answer.seq} answer · Supervisor] Use yearly prices in EUR" in step4.inbox
        # the answer to my question ranks first (score 85)
        assert step4.inbox.splitlines()[0].startswith(f"[#{answer.seq} answer")
    finally:
        await world.close()
