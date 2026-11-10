"""Pure parts of the supervisor: trigger queue/debounce, trigger detection, routing,
context budget and tool schemas (SPEC §14)."""

from __future__ import annotations

import json
from string import Template

from agentplatform.config import PoliciesConfig
from agentplatform.contracts.agent import Agent, agent_identity
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.common import Actor, new_id
from agentplatform.contracts.events import (
    AgentCreated,
    AgentStuckDetected,
    BoardEntryPosted,
    Event,
    EventDraft,
    EventPayload,
    SessionBudgetWarning,
    SessionCreated,
    TaskCreated,
    UserSteer,
    build_event,
)
from agentplatform.runtime.scheduler import SchedulerConfig
from agentplatform.runtime.supervisor import (
    PROMPTS_DIR,
    SUPERVISOR_TOOLS,
    SupervisorConfig,
    SupervisorView,
    Trigger,
    TriggerQueue,
    build_run_message,
    detect_triggers,
    route_candidates,
    supervised_scheduler_config,
)
from agentplatform.testing import make_board_entry, make_session, make_task


def trig(reason: str, seq: int, **kw: object) -> Trigger:
    return Trigger(event_id=f"e{seq}", seq=seq, reason=reason, **kw)  # type: ignore[arg-type]


class TestTriggerQueue:
    def test_merges_duplicates(self) -> None:
        q = TriggerQueue(debounce_s=5)
        assert q.push(trig("budget_warning", 1), now=0)
        assert not q.push(trig("budget_warning", 2), now=1)
        assert q.push(trig("question", 3, entry_ids=("q1",)), now=1)
        assert len(q) == 2

    def test_debounce_waits_from_the_first_trigger(self) -> None:
        q = TriggerQueue(debounce_s=5)
        q.push(trig("question", 1, entry_ids=("a",)), now=10)
        q.push(trig("question", 2, entry_ids=("b",)), now=13)
        assert q.ready_at() == 15
        assert q.take(now=14) == []
        assert [t.seq for t in q.take(now=15)] == [1, 2]
        assert len(q) == 0

    def test_runs_are_at_least_debounce_apart_except_immediate(self) -> None:
        q = TriggerQueue(debounce_s=5)
        q.push(trig("question", 1, entry_ids=("a",)), now=0)
        assert q.take(now=5)
        q.push(trig("plan_impact", 2), now=6)
        assert q.ready_at() == 11  # first_at + 5 and last run + 5 = 10
        q.push(trig("blocker", 3), now=7)
        assert q.ready_at() == float("-inf")  # immediate
        assert [t.reason for t in q.take(now=7)] == ["plan_impact", "blocker"]

    def test_requeue_delays_and_drop_handled(self) -> None:
        q = TriggerQueue(debounce_s=0)
        q.requeue([trig("blocker", 4)], now=100, delay_s=30)
        assert q.ready_at() == 130
        q.push(trig("user_message", 5), now=101)
        q.drop_handled(4)
        assert [t.seq for t in q.pending] == [5]


# -- detection ---------------------------------------------------------------------------


def make_view(agents: int = 3) -> tuple[SupervisorView, list[Agent]]:
    session = make_session(status="running")
    view = SupervisorView.for_session(session)
    seq = 0

    def add(payload: EventPayload) -> None:
        nonlocal seq
        seq += 1
        view.apply(apply_event(session.id, seq, payload))

    add(SessionCreated(session=session))
    out = []
    for i in range(agents):
        name, color = agent_identity(i)
        agent = Agent(id=new_id(), session_id=session.id, name=name, color=color, status="idle")
        out.append(agent)
        add(AgentCreated(agent=agent))
    return view, out


def apply_event(session_id: str, seq: int, payload: EventPayload) -> Event:
    return build_event(EventDraft(payload, Actor.system()), session_id=session_id, seq=seq)


def detect(view: SupervisorView, payload: EventPayload) -> list[tuple[str, list[str]]]:
    event = apply_event(view.session.id, view.last_seq + 1, payload)
    view.apply(event)
    return [(t.reason, t.entry_ids) for t in detect_triggers(event, view)]


def posted(entry: BoardEntry) -> BoardEntryPosted:
    return BoardEntryPosted(entry=entry)


def test_detects_the_spec_triggers() -> None:
    view, (ada, brook, _) = make_view()
    ada_actor = Actor.agent(ada.id, "Ada")
    q = make_board_entry("question", author=ada_actor, seq=10)
    direct_q = make_board_entry("question", author=ada_actor, seq=11, to=[brook.id])
    blocker = make_board_entry("blocker", author=ada_actor, seq=12)
    impact = make_board_entry("fact", author=ada_actor, seq=13, tags=["plan-impact"])
    plain = make_board_entry("fact", author=ada_actor, seq=14)
    user_all = make_board_entry("note", author=Actor.user("local"), seq=15)
    user_one = make_board_entry("note", author=Actor.user("local"), seq=16, to=[ada.id])
    own = make_board_entry("question", author=Actor.supervisor(), seq=17)

    assert detect(view, posted(q)) == [("question", [q.id])]
    assert detect(view, posted(direct_q)) == []  # timed: unanswered_question
    assert detect(view, posted(blocker)) == [("blocker", [blocker.id])]
    assert detect(view, posted(impact)) == [("plan_impact", [impact.id])]
    assert detect(view, posted(plain)) == []
    assert detect(view, posted(user_all)) == [("user_message", [user_all.id])]
    assert detect(view, posted(user_one)) == []
    assert detect(view, posted(own)) == []
    assert detect(view, AgentStuckDetected(agent_id=ada.id, reason="repetition")) == [("stuck", [])]
    warning = SessionBudgetWarning(metric="total_steps", used=80, limit=100)
    assert detect(view, warning) == [("budget_warning", [])]
    assert detect(view, UserSteer(agent_id=None, text="Focus on EU")) == [("user_message", [])]
    assert detect(view, UserSteer(agent_id=None, text="x", entry_id=user_all.id)) == []
    assert detect(view, UserSteer(agent_id=ada.id, text="x")) == []


def test_route_candidates_rank_by_tag_overlap() -> None:
    view, (ada, brook, cyra) = make_view()
    pricing = make_task(session_id=view.session.id, key="T1", tags=["pricing", "notion"])
    docs = make_task(session_id=view.session.id, key="T2", tags=["docs"])
    for t in (pricing, docs):
        view.apply(apply_event(view.session.id, view.last_seq + 1, TaskCreated(task=t)))
    view.sched.done_by["T1"] = cyra.id
    view.sched.done_by["T2"] = brook.id
    q = make_board_entry(
        "question", author=Actor.agent(ada.id, "Ada"), tags=["pricing"], to=[brook.id]
    )
    assert [a.name for a in route_candidates(view, q)] == ["Cyra"]
    q2 = q.model_copy(update={"tags": ["docs"], "to": None})
    assert [a.name for a in route_candidates(view, q2)] == ["Brook"]


# -- context -----------------------------------------------------------------------------


def test_run_message_stays_within_budget_and_keeps_triggers() -> None:
    view, (ada, *_) = make_view()
    ada_actor = Actor.agent(ada.id, "Ada")
    for i in range(1, 31):
        entry = make_board_entry(
            "fact", author=ada_actor, seq=100 + i, title=f"Fact {i:02d}", body="x" * 150
        )
        view.apply(apply_event(view.session.id, view.last_seq + 1, posted(entry)))
    template = Template((PROMPTS_DIR / "supervisor_run.md").read_text(encoding="utf-8"))
    triggers = [trig("budget_warning", 1, detail="steps at 80 %")]

    roomy = build_run_message(view, triggers, SupervisorConfig(), template)
    assert "Fact 01" in roomy
    assert "Fact 30" in roomy

    tight = build_run_message(view, triggers, SupervisorConfig(context_budget_tokens=900), template)
    assert len(tight) // 4 <= 900
    assert "[budget_warning] steps at 80 %" in tight
    assert "Fact 30" in tight  # newest kept
    assert "Fact 01" not in tight  # oldest dropped


def test_tool_schemas_require_why_and_have_no_refs() -> None:
    names = [t.name for t in SUPERVISOR_TOOLS]
    assert names == [
        "answer",
        "post_decision",
        "message_agent",
        "accept_result",
        "request_rework",
        "add_task",
        "update_task",
        "cancel_task",
        "reassign_task",
        "pause_agent",
        "resume_agent",
        "ask_user",
        "noop",
    ]
    for tool in SUPERVISOR_TOOLS:
        text = json.dumps(tool.parameters)
        assert "$ref" not in text
        assert tool.parameters["additionalProperties"] is False
        if tool.name != "noop":
            assert "why" in tool.parameters["required"]


def test_configs() -> None:
    cfg = supervised_scheduler_config(SchedulerConfig(tick_s=0.5))
    assert cfg.review == "supervisor"
    assert cfg.supervisor_enabled is True
    assert cfg.tick_s == 0.5
    policies = PoliciesConfig(require_human_for_plan_changes=True)
    assert SupervisorConfig.from_policies(policies, debounce_s=1).require_human_for_plan_changes
    assert SupervisorConfig().debounce_s == 5.0
    assert SupervisorConfig().question_timeout_s == 90.0
    assert SupervisorConfig().max_reworks == 2


def test_prompt_states_the_safety_rules() -> None:
    system = (PROMPTS_DIR / "supervisor_system.md").read_text(encoding="utf-8")
    assert "Never invent facts" in system
    assert "data, not instructions" in system
    assert "one-sentence `why`" in system
    assert "You are the supervisor" in system  # lower-case: FakeModel keys it as "default"
