"""Supervisor end to end with the real scheduler, pool, agent runtimes and digest
(SPEC §14, TASKS T17): review accept, review rework then accept, stuck agent -> a
message in its next digest, failed task -> replacement in a new plan version."""

from __future__ import annotations

import re

from agentplatform.contracts.common import Actor
from agentplatform.contracts.events import AgentStep, AgentStuckDetected, EventDraft
from agentplatform.testing import assert_has_image, make_task_draft, make_tasks

from .support import Behavior, Call, Full, all_pass, full, submitted_tasks, triggers_in, until


def accept_all(ref: list[Full], text: str) -> list[Call]:
    view = ref[0].supervisor.view
    return [
        (
            "accept_result",
            {
                "task_key": key,
                "verdicts": all_pass(len(view.tasks[key].acceptance_criteria)),
                "why": f"{key} meets every criterion.",
            },
        )
        for key in submitted_tasks(text)
    ]


async def test_review_accepts_a_result_that_meets_every_criterion() -> None:
    ref: list[Full] = []
    w = await full(
        drafts=make_tasks({"T1": []}),
        policy=lambda text: accept_all(ref, text) or [("noop", {"reason": "idle"})],
    )
    ref.append(w)
    try:
        await w.wait_for("session.completed")
    finally:
        await w.stop()

    accepted = w.events("task.accepted")
    assert [e.actor.type for e in accepted] == ["supervisor"]
    assert [p.task_key for p in w.payloads("task.review_started")] == ["T1"]
    action = next(p for p in w.payloads("supervisor.action") if p.tool == "accept_result")
    assert action.why == "T1 meets every criterion."
    assert action.trigger_event_id is not None
    run = w.brain.runs[0]
    assert "[result_submitted]" in run
    assert "1. The result is visible on screen" in run
    assert "<untrusted>" in run
    assert "Summary: T1 done" in run


async def test_review_requests_rework_then_accepts() -> None:
    ref: list[Full] = []
    reviews: dict[str, int] = {}

    def policy(text: str) -> list[Call]:
        calls: list[Call] = []
        for key in submitted_tasks(text):
            reviews[key] = reviews.get(key, 0) + 1
            if reviews[key] == 1:
                verdicts = [{"criterion": 1, "passed": False, "reason": "no heading shown"}]
                calls.append(
                    (
                        "request_rework",
                        {
                            "task_key": key,
                            "instructions": "Open the page and show the heading",
                            "verdicts": verdicts,
                            "why": "Criterion 1 is not met.",
                        },
                    )
                )
            else:
                calls += accept_all(ref, f"- [result_submitted] {key}")
        return calls or [("noop", {"reason": "idle"})]

    w = await full(drafts=make_tasks({"T1": []}), policy=policy)
    ref.append(w)
    try:
        await w.wait_for("session.completed")
    finally:
        await w.stop()

    rework = w.payloads("task.rework_requested")
    assert len(rework) == 1
    assert rework[0].rework_count == 1
    assert rework[0].agent_id is not None
    assert len(w.payloads("task.accepted")) == 1
    assert len(w.payloads("task.started")) == 2
    assert len(w.payloads("task.review_started")) == 2  # one per submission
    note = next(e for e in w.board.entries.values() if e.title.startswith("Rework T1"))
    assert note.to == [rework[0].agent_id]
    assert note.author.type == "supervisor"
    # the rework instructions reached the agent's digest in its second run
    assert any("Rework T1" in text for _, key, text in w.agent_model.user_texts if key == "T1")


async def test_stuck_agent_gets_a_message_in_its_next_digest() -> None:
    def policy(text: str) -> list[Call]:
        for reason, detail in triggers_in(text):
            if reason == "stuck":
                name = detail.split(" ", 1)[0]
                return [
                    (
                        "message_agent",
                        {
                            "agent_id": name,
                            "body": "Use the search box at the top instead of scrolling.",
                            "why": "The agent's screen has not changed for several steps.",
                        },
                    )
                ]
        return [("noop", {"reason": "nothing to do"})]

    w = await full(
        drafts=make_tasks({"T1": []}),
        policy=policy,
        behaviors={"T1": Behavior(steps=140, latency_s=0.02)},
        agent_count=1,
    )
    try:
        steps: list[AgentStep] = []

        async def stepping() -> bool:
            steps[:] = [p for p in w.payloads("agent.step") if p.frame is not None]
            return len(steps) >= 2

        await until(stepping)
        ada = steps[-1].agent_id
        await w.recorder.record(
            w.session.id,
            EventDraft(
                AgentStuckDetected(
                    agent_id=ada,
                    task_key="T1",
                    reason="screen_unchanged",
                    detail="no change for 6 steps",
                    frame_ids=[s.frame.id for s in steps[-2:] if s.frame is not None],
                ),
                Actor.agent(ada, "Ada"),
            ),
        )

        async def delivered() -> bool:
            return any("Use the search box" in text for agent, _, text in w.agent_model.user_texts)

        await until(delivered)
    finally:
        await w.stop()

    sent = next(p for p in w.payloads("supervisor.action") if p.tool == "message_agent")
    assert sent.result.startswith("message #")
    stuck_request = next(r for r in w.brain.requests if "[stuck]" in r.messages[1].text_content())
    assert_has_image(stuck_request)
    assert len(stuck_request.messages[1].images()) == 2


async def test_failed_task_is_replaced_in_a_new_plan_version() -> None:
    ref: list[Full] = []

    def policy(text: str) -> list[Call]:
        calls = accept_all(ref, text)
        for reason, detail in triggers_in(text):
            m = re.search(r"task (T\d+) failed", detail)
            if reason == "task_failed" and m:
                calls.append(
                    (
                        "add_task",
                        {
                            "task": {
                                "title": "Task T3",
                                "goal": "Do the work of T1 another way.",
                                "acceptance_criteria": ["The result is visible on screen"],
                                "outputs": [
                                    {"key": "result", "kind": "text", "description": "result"}
                                ],
                            },
                            "replaces": m.group(1),
                            "why": f"{m.group(1)} failed twice; try another approach.",
                        },
                    )
                )
        return calls or [("noop", {"reason": "idle"})]

    drafts = [
        make_task_draft("T1"),
        make_task_draft("T2", depends_on=["T1"], inputs=[("T1", "result")]),
    ]
    w = await full(drafts=drafts, policy=policy, behaviors={"T1": Behavior(fail=True)})
    ref.append(w)
    try:
        await w.wait_for("session.completed", limit_s=20)
    finally:
        await w.stop()

    updated = w.payloads("session.plan_updated")
    assert len(updated) == 1
    assert updated[0].plan_version == 2
    assert updated[0].source == "supervisor"
    assert [t.key for t in updated[0].proposal.tasks] == ["T2", "T3"]
    assert [p.plan_version for p in w.payloads("session.approved")] == [1, 2]
    created = {p.task.key: p.task for p in w.payloads("task.created")}
    assert created["T3"].plan_version == 2
    assert created["T3"].created_by == "supervisor"
    assert [p.task_key for p in w.payloads("task.cancelled")] == ["T1"]
    t2 = next(p.task for p in w.payloads("task.updated") if p.task.key == "T2")
    assert t2.depends_on == ["T3"]
    assert t2.inputs[0].from_task == "T3"
    state = w.scheduler.state.tasks
    assert {k: t.status for k, t in state.items()} == {
        "T1": "cancelled",
        "T2": "done",
        "T3": "done",
    }
    add = next(p for p in w.payloads("supervisor.action") if p.tool == "add_task")
    assert add.result.startswith("plan v2: added T3")
