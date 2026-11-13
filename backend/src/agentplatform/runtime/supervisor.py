"""Supervisor (SPEC §14): an event-driven LLM agent that reviews results, answers and
routes questions, unblocks agents and changes the plan. See docs/coordination-protocol.md
("Supervisor") for the protocol; this docstring covers the implementation.

**Triggers.** The supervisor folds the session's events (``SupervisorView``, which
reuses the scheduler's public ``SchedulerState`` fold). ``detect_triggers`` turns raw
events into ``supervisor.triggered`` events (SPEC §14.1); the board (``claim_conflict``)
and the scheduler (``task_failed``) emit theirs directly. Every trigger is therefore an
event, which makes the UI feed, replay and restarts work the same way.

**Debounce.** ``TriggerQueue`` merges duplicate triggers and releases a batch at least
``debounce_s`` after its first trigger and after the previous run started; blockers and
user messages are released at once. One run is in flight per session: runs are awaited
inside the single decision loop, and triggers arriving meanwhile go into the next
batch. Runs happen only while the session is ``running`` or ``completed``; a paused
session keeps its triggers until it resumes.

**Runs.** A run builds the context (SPEC §14.2) within a token budget, asks the model
(at most ``max_turns`` turns) and executes the tool calls (SPEC §14.3). Every call is
recorded as ``supervisor.action`` with its ``why``. ``trigger_event_id`` is the newest
trigger of the batch: a trigger is handled once an action names it or a newer trigger,
which is what a restarted supervisor uses to find unhandled triggers.

**Plan changes** (``add_task``/``update_task``/``cancel_task``) create a new plan
version (``session.plan_updated`` with ``source="supervisor"``; ``session.approved``
only when tasks are added, because that is what makes the scheduler create them) and
the task events. Each event is appended with a compare-and-set on the log position
(a draft factory that refuses when the log moved), after re-checking its precondition
on the current state, so supervisor edits never race the scheduler. With
``require_human_for_plan_changes`` the change goes to the approvals queue instead and
is applied when ``approval.decided`` approves it.

The supervisor never touches scheduler internals: it emits events and calls
``Scheduler.reassign``; agents are paused through an ``AgentControl`` port.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from string import Template
from typing import Annotated, Any, Final, Literal, Protocol

import structlog
from PIL import Image
from pydantic import BeforeValidator, Field, ValidationError

from agentplatform.artifacts.base import ArtifactStore
from agentplatform.bus.base import EventBus
from agentplatform.config import PoliciesConfig
from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Approval, Frame
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.common import (
    Actor,
    Contract,
    SessionStatus,
    SuggestedTool,
    new_id,
    utcnow,
)
from agentplatform.contracts.events import (
    AgentStep,
    AgentStuckDetected,
    ApprovalDecided,
    ApprovalRequested,
    BoardEntryPosted,
    BoardEntryStatusChanged,
    Event,
    EventDraft,
    EventEnvelope,
    EventPayload,
    SessionApproved,
    SessionBudgetWarning,
    SessionPlanProposed,
    SessionPlanUpdated,
    SupervisorAction,
    SupervisorTriggered,
    SupervisorTriggerReason,
    TaskAccepted,
    TaskCancelled,
    TaskFailed,
    TaskResultSubmitted,
    TaskReviewStarted,
    TaskReworkRequested,
    TaskUpdated,
    UserSteer,
)
from agentplatform.contracts.model_io import (
    ImagePart,
    Message,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCall,
    ToolSpec,
)
from agentplatform.contracts.plan import PlanProposal, validate_plan
from agentplatform.contracts.session import Session
from agentplatform.contracts.task import InputRef, OutputSpec, Task, TaskDraft
from agentplatform.db.event_store import DraftFactory, EventStore
from agentplatform.errors import (
    AppError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from agentplatform.models.base import ModelClient
from agentplatform.models.errors import redact_secrets
from agentplatform.runtime.agent.loop import LocalControlState
from agentplatform.runtime.coordination import posting
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.runtime.coordination.digest import estimate_tokens, format_entry, sanitize
from agentplatform.runtime.planner import next_key_number
from agentplatform.runtime.pool import AgentPool
from agentplatform.runtime.scheduler import FINISHED_TASK_STATUSES, SchedulerConfig, SchedulerState

__all__ = [
    "IMMEDIATE_REASONS",
    "PLAN_IMPACT_TAG",
    "SUPERVISOR_AGENT_ID",
    "SUPERVISOR_TOOLS",
    "AgentControl",
    "EventWriter",
    "PoolAgentControl",
    "Supervisor",
    "SupervisorConfig",
    "SupervisorView",
    "TaskReassigner",
    "Trigger",
    "TriggerQueue",
    "Verdict",
    "build_run_message",
    "detect_triggers",
    "route_candidates",
    "supervised_scheduler_config",
]

log = structlog.get_logger(__name__)

PROMPTS_DIR: Final = Path(__file__).parent / "prompts"
SUPERVISOR_AGENT_ID: Final = "supervisor"
"""``Approval.agent_id`` of plan changes the supervisor routes to the approvals queue."""
PLAN_IMPACT_TAG: Final = "plan-impact"
IMMEDIATE_REASONS: Final[frozenset[str]] = frozenset({"blocker", "user_message"})
_RUN_STATUSES: Final[frozenset[SessionStatus]] = frozenset({"running", "completed"})
_END_STATUSES: Final[frozenset[SessionStatus]] = frozenset({"stopped", "failed"})
_EDITABLE: Final = frozenset({"pending", "ready"})
_CANCELLABLE: Final = frozenset({"pending", "ready", "failed", "in_review"})
_REPLACEABLE: Final = frozenset({"pending", "ready", "failed"})
_RESULT_CHARS: Final = 600
_WHY_CHARS: Final = 400
_UNTRUSTED_TAG_RE: Final = re.compile(r"<\s*/?\s*untrusted\s*>", re.IGNORECASE)


# -- configuration ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    debounce_s: float = 5.0
    """Minimum time between runs, and how long a batch collects triggers."""
    question_timeout_s: float = 90.0
    """An agent-to-agent question still open after this triggers the supervisor."""
    max_reworks: int = 2
    max_review_retries: int = 1
    """A review the model left undecided is queued again this often, then ``ask_user``."""
    context_budget_tokens: int = 8000
    """Text budget of the run message (4 chars ≈ 1 token); frames are extra."""
    board_entries: int = 30
    recent_actions: int = 10
    max_frames: int = 3
    max_turns: int = 3
    max_actions_per_run: int = 8
    max_output_tokens: int = 2048
    model_timeout_s: float = 120.0
    model_backoff_s: tuple[float, ...] = (2.0, 10.0)
    """Waits between model attempts within a run; after the last failure the batch is
    requeued ``requeue_after_s`` later."""
    requeue_after_s: float = 30.0
    cas_attempts: int = 8
    """Compare-and-set attempts per plan-change event before giving up."""
    tick_s: float = 1.0
    approval_ttl_s: float = 15 * 60.0
    require_human_for_plan_changes: bool = False

    @classmethod
    def from_policies(cls, policies: PoliciesConfig, **overrides: Any) -> SupervisorConfig:
        """Defaults plus ``require_human_for_plan_changes`` from ``policies.yaml``."""
        return cls(
            require_human_for_plan_changes=policies.require_human_for_plan_changes, **overrides
        )


def supervised_scheduler_config(base: SchedulerConfig | None = None) -> SchedulerConfig:
    """The scheduler configuration to use when a ``Supervisor`` runs the session:
    results wait for the supervisor's review, and a DAG stalled by failed tasks waits
    for the supervisor instead of failing the session."""
    return replace(base or SchedulerConfig(), review="supervisor", supervisor_enabled=True)


# -- ports --------------------------------------------------------------------------------


class AgentControl(Protocol):
    """Pauses and resumes one agent between its steps (T13 unifies this with T18)."""

    async def pause(self, agent_id: str, reason: str) -> None: ...

    async def resume(self, agent_id: str, note: str) -> None: ...


class PoolAgentControl:
    """``AgentControl`` over the pool's public ``runtime(agent_id).control``. Never
    overrides a user takeover or a stop."""

    def __init__(self, pool: AgentPool) -> None:
        self._pool = pool

    def _control(self, agent_id: str) -> LocalControlState:
        if not self._pool.has(agent_id):
            raise NotFoundError(f"agent {agent_id} is not in this session")
        runtime = self._pool.runtime(agent_id)
        if runtime is None or not isinstance(runtime.control, LocalControlState):
            raise ConflictError("the agent has no running runtime")
        return runtime.control

    async def pause(self, agent_id: str, reason: str) -> None:
        control = self._control(agent_id)
        if control.mode in ("takeover", "stop"):
            raise ConflictError(f"the agent is in {control.mode} mode")
        control.set("pause")

    async def resume(self, agent_id: str, note: str) -> None:
        control = self._control(agent_id)
        if control.mode != "pause":
            raise ConflictError(f"the agent is not paused (mode {control.mode})")
        control.set("run")


class TaskReassigner(Protocol):
    """``Scheduler.reassign``."""

    async def reassign(self, task_key: str, agent_id: str | None = None) -> str: ...


class EventWriter(Protocol):
    """``EventRecorder``: appends (accepting a draft factory) and publishes."""

    async def record(self, session_id: str, draft: EventDraft | DraftFactory) -> Event: ...


class _StaleLog(Exception):
    """The log moved since the state a plan-change event was built from."""


# -- triggers -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Trigger:
    """A ``supervisor.triggered`` event waiting for (or handled by) a run."""

    event_id: str
    seq: int
    reason: SupervisorTriggerReason
    detail: str = ""
    task_key: str | None = None
    agent_id: str | None = None
    entry_ids: tuple[str, ...] = ()
    causation_id: str | None = None

    @classmethod
    def from_event(cls, event: Event) -> Trigger:
        p = event.payload
        assert isinstance(p, SupervisorTriggered)
        return cls(
            event_id=event.id,
            seq=event.seq,
            reason=p.reason,
            detail=p.detail,
            task_key=p.task_key,
            agent_id=p.agent_id,
            entry_ids=tuple(p.entry_ids),
            causation_id=event.causation_id,
        )

    @property
    def immediate(self) -> bool:
        return self.reason in IMMEDIATE_REASONS

    @property
    def merge_key(self) -> tuple[str, str | None, str | None, tuple[str, ...]]:
        return (self.reason, self.task_key, self.agent_id, self.entry_ids)


def _trigger_key(p: SupervisorTriggered) -> tuple[str, str | None, str | None, tuple[str, ...]]:
    return (p.reason, p.task_key, p.agent_id, tuple(p.entry_ids))


class TriggerQueue:
    """Pending triggers with debounce (see the module docstring). Times are monotonic
    seconds supplied by the caller, so tests control the clock."""

    def __init__(self, debounce_s: float) -> None:
        self.debounce_s = debounce_s
        self._items: OrderedDict[tuple[str, str | None, str | None, tuple[str, ...]], Trigger] = (
            OrderedDict()
        )
        self._first_at: float | None = None
        self._not_before = float("-inf")
        self._last_run_at = float("-inf")

    def __len__(self) -> int:
        return len(self._items)

    @property
    def pending(self) -> list[Trigger]:
        return sorted(self._items.values(), key=lambda t: t.seq)

    def has(self, key: tuple[str, str | None, str | None, tuple[str, ...]]) -> bool:
        return key in self._items

    def push(self, trigger: Trigger, now: float) -> bool:
        """Add a trigger; ``False`` if an equal one is already pending (merged)."""
        if trigger.merge_key in self._items:
            return False
        self._items[trigger.merge_key] = trigger
        if self._first_at is None:
            self._first_at = now
        return True

    def drop_handled(self, handled_seq: int) -> None:
        for key in [k for k, t in self._items.items() if t.seq <= handled_seq]:
            del self._items[key]
        if not self._items:
            self._first_at = None

    def ready_at(self) -> float | None:
        """Monotonic time at which the pending batch may run, ``None`` if empty."""
        if not self._items or self._first_at is None:
            return None
        if any(t.immediate for t in self._items.values()):
            return self._not_before
        return max(
            self._first_at + self.debounce_s,
            self._last_run_at + self.debounce_s,
            self._not_before,
        )

    def take(self, now: float) -> list[Trigger]:
        """The whole pending batch if it is due (marks the start of a run)."""
        due = self.ready_at()
        if due is None or now < due:
            return []
        batch = self.pending
        self._items.clear()
        self._first_at = None
        self._last_run_at = now
        return batch

    def requeue(self, triggers: Iterable[Trigger], now: float, delay_s: float) -> None:
        """Put a batch back after a failed run; it is not released before ``delay_s``."""
        for t in triggers:
            self.push(t, now)
        self._not_before = now + delay_s


def route_candidates(view: SupervisorView, question: BoardEntry, limit: int = 3) -> list[Agent]:
    """Agents best placed to answer ``question``: most tags in common between the
    question (plus its task) and the agent's current and finished tasks. Excludes the
    asker and, for a direct question, the agents it was addressed to."""
    st = view.sched
    tags = set(question.tags)
    if question.task_key and question.task_key in st.tasks:
        tags |= set(st.tasks[question.task_key].tags)
    excluded = {question.author.id, *(question.to or [])}
    scored: list[tuple[int, int, Agent]] = []
    for index, agent in enumerate(st.agents.values()):
        if agent.id in excluded or agent.status == "stopped":
            continue
        agent_tags: set[str] = set()
        for key, a in st.assignments.items():
            if a.agent_id == agent.id:
                agent_tags |= set(st.tasks[key].tags)
        for key, agent_id in st.done_by.items():
            if agent_id == agent.id and key in st.tasks:
                agent_tags |= set(st.tasks[key].tags)
        overlap = len(tags & agent_tags)
        if overlap:
            scored.append((-overlap, index, agent))
    scored.sort(key=lambda s: (s[0], s[1]))
    return [a for _, _, a in scored[:limit]]


def detect_triggers(event: Event, view: SupervisorView) -> list[SupervisorTriggered]:
    """Triggers (SPEC §14.1) that the supervisor raises itself for ``event``. ``view``
    is the state after ``event`` was applied. The board's ``claim_conflict``, the
    scheduler's ``task_failed`` and the timed ``unanswered_question`` are not here."""
    p = event.payload
    names = view.agent_names
    if isinstance(p, TaskResultSubmitted):
        return [
            SupervisorTriggered(
                reason="result_submitted",
                detail=f"{names.get(p.agent_id, 'an agent')} submitted a result for {p.task_key}",
                task_key=p.task_key,
                agent_id=p.agent_id,
            )
        ]
    if isinstance(p, AgentStuckDetected):
        return [
            SupervisorTriggered(
                reason="stuck",
                detail=f"{names.get(p.agent_id, 'an agent')} is stuck ({p.reason}): {p.detail}"[
                    :1000
                ],
                task_key=p.task_key,
                agent_id=p.agent_id,
            )
        ]
    if isinstance(p, SessionBudgetWarning):
        return [
            SupervisorTriggered(
                reason="budget_warning",
                detail=f"{p.metric} at {p.used:g} of {p.limit:g}",
            )
        ]
    if isinstance(p, UserSteer):
        if p.agent_id is None and p.entry_id is None:  # with an entry: see the board note
            return [SupervisorTriggered(reason="user_message", detail=sanitize(p.text, 300))]
        return []
    if not isinstance(p, BoardEntryPosted):
        return []
    e = p.entry
    agent_id = e.author.id if e.author.type == "agent" else None
    ids = [e.id]
    title = sanitize(e.title, 200)
    if e.author.type == "user" and e.to is None:
        return [
            SupervisorTriggered(
                reason="user_message", detail=title, entry_ids=ids, task_key=e.task_key
            )
        ]
    if e.author.type != "agent":
        return []
    who = e.author.name
    if e.kind == "question" and e.to is None:
        return [
            SupervisorTriggered(
                reason="question",
                detail=f"{who} asks: {title}",
                task_key=e.task_key,
                agent_id=agent_id,
                entry_ids=ids,
            )
        ]
    if e.kind == "blocker":
        return [
            SupervisorTriggered(
                reason="blocker",
                detail=f"{who} is blocked: {title}",
                task_key=e.task_key,
                agent_id=agent_id,
                entry_ids=ids,
            )
        ]
    if e.kind == "fact" and PLAN_IMPACT_TAG in e.tags:
        return [
            SupervisorTriggered(
                reason="plan_impact",
                detail=f"{who} reports a fact that affects the plan: {title}",
                task_key=e.task_key,
                agent_id=agent_id,
                entry_ids=ids,
            )
        ]
    return []


# -- state --------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _PendingChange:
    tool: str
    args: dict[str, Any]
    why: str


@dataclass(slots=True)
class SupervisorView:
    """Everything the supervisor knows, rebuilt from events (``apply``)."""

    sched: SchedulerState
    entries: dict[str, BoardEntry] = field(default_factory=dict)
    seq_to_entry: dict[int, str] = field(default_factory=dict)
    frames: OrderedDict[str, Frame] = field(default_factory=OrderedDict)
    stuck_frames: dict[str, list[str]] = field(default_factory=dict)
    """``agent.stuck_detected`` event id -> its frame ids."""
    actions: list[SupervisorAction] = field(default_factory=list)
    triggers: dict[str, Trigger] = field(default_factory=dict)
    handled_seq: int = 0
    """Triggers with ``seq <= handled_seq`` have been handled by a run."""
    event_seq: dict[str, int] = field(default_factory=dict)
    detected_seq: int = 0
    """Highest seq of a raw event the supervisor already raised a trigger for."""
    unanswered_triggered: set[str] = field(default_factory=set)
    reviews_started: set[tuple[str, int]] = field(default_factory=set)
    latest_proposal: PlanProposal | None = None
    pending_changes: dict[str, _PendingChange] = field(default_factory=dict)
    resolved_changes: set[str] = field(default_factory=set)
    decisions: dict[str, Approval] = field(default_factory=dict)
    max_frames: int = 300
    max_actions: int = 50

    @classmethod
    def for_session(cls, session: Session) -> SupervisorView:
        return cls(sched=SchedulerState.for_session(session))

    # -- queries ----------------------------------------------------------------------

    @property
    def session(self) -> Session:
        return self.sched.session

    @property
    def status(self) -> SessionStatus:
        return self.sched.status

    @property
    def last_seq(self) -> int:
        return self.sched.last_seq

    @property
    def tasks(self) -> dict[str, Task]:
        return self.sched.tasks

    @property
    def agent_names(self) -> dict[str, str]:
        return {a.id: a.name for a in self.sched.agents.values()}

    @property
    def plan_version(self) -> int:
        return max([self.session.plan_version, *self.sched.proposals])

    def entry(self, ref: str) -> BoardEntry | None:
        """An entry by id or by ``#seq`` / ``seq``."""
        ref = ref.strip()
        if ref in self.entries:
            return self.entries[ref]
        digits = ref.lstrip("#")
        if digits.isdigit():
            entry_id = self.seq_to_entry.get(int(digits))
            return self.entries.get(entry_id) if entry_id else None
        return None

    def agent(self, ref: str) -> Agent | None:
        """An agent by id or (case-insensitive) name."""
        ref = ref.strip()
        if ref in self.sched.agents:
            return self.sched.agents[ref]
        return next((a for a in self.sched.agents.values() if a.name.lower() == ref.lower()), None)

    def current_task_of(self, agent_id: str) -> str | None:
        return next((k for k, a in self.sched.assignments.items() if a.agent_id == agent_id), None)

    def recent_entries(self, n: int) -> list[BoardEntry]:
        return sorted(self.entries.values(), key=lambda e: e.seq)[-n:] if n > 0 else []

    def open_directed_questions(self) -> list[BoardEntry]:
        return [
            e
            for e in self.entries.values()
            if e.kind == "question"
            and e.status == "open"
            and e.to is not None
            and e.author.type == "agent"
        ]

    def unfinished_dependents(self, key: str) -> list[str]:
        return [
            t.key
            for t in self.tasks.values()
            if key in t.depends_on and t.status not in FINISHED_TASK_STATUSES
        ]

    def agent_frames(self, agent_id: str, n: int) -> list[Frame]:
        return [f for f in self.frames.values() if f.agent_id == agent_id][-n:]

    # -- fold -------------------------------------------------------------------------

    def apply(self, event: Event) -> None:
        self.sched.apply(event)
        self.event_seq[event.id] = event.seq
        p = event.payload
        if isinstance(p, BoardEntryPosted):
            self.entries[p.entry.id] = p.entry
            self.seq_to_entry[p.entry.seq] = p.entry.id
        elif isinstance(p, BoardEntryStatusChanged):
            entry = self.entries.get(p.entry_id)
            if entry is not None:
                self.entries[p.entry_id] = entry.model_copy(update={"status": p.status})
        elif isinstance(p, AgentStep):
            if p.frame is not None:
                self.frames[p.frame.id] = p.frame
                while len(self.frames) > self.max_frames:
                    self.frames.popitem(last=False)
        elif isinstance(p, AgentStuckDetected):
            self.stuck_frames[event.id] = list(p.frame_ids)
        elif isinstance(p, SessionPlanProposed | SessionPlanUpdated):
            self.latest_proposal = p.proposal
        elif isinstance(p, TaskReviewStarted):
            task = self.tasks.get(p.task_key)
            if task is not None:
                self.reviews_started.add((p.task_key, task.rework_count))
        elif isinstance(p, SupervisorTriggered):
            trigger = Trigger.from_event(event)
            self.triggers[event.id] = trigger
            if event.actor.type == "supervisor" and event.causation_id in self.event_seq:
                self.detected_seq = max(self.detected_seq, self.event_seq[event.causation_id])
            if p.reason == "unanswered_question":
                self.unanswered_triggered.update(p.entry_ids)
        elif isinstance(p, SupervisorAction):
            self._apply_action(p)
        elif isinstance(p, ApprovalDecided):
            if p.approval.agent_id == SUPERVISOR_AGENT_ID:
                self.decisions[p.approval.id] = p.approval

    def _apply_action(self, p: SupervisorAction) -> None:
        self.actions.append(p)
        del self.actions[: -self.max_actions]
        if p.trigger_event_id is not None and p.trigger_event_id in self.triggers:
            self.handled_seq = max(self.handled_seq, self.triggers[p.trigger_event_id].seq)
        approval_id = p.args.get("approval_id")
        if isinstance(approval_id, str):
            if (p.result or "").startswith("pending approval"):
                args = {k: v for k, v in p.args.items() if k != "approval_id"}
                self.pending_changes[approval_id] = _PendingChange(p.tool, args, p.why)
            else:
                self.resolved_changes.add(approval_id)


# -- tool arguments -----------------------------------------------------------------------


def _clip_why(value: object) -> object:
    if isinstance(value, str):
        flat = " ".join(value.split())
        return flat if len(flat) <= _WHY_CHARS else flat[: _WHY_CHARS - 1] + "…"
    return value


Why = Annotated[str, BeforeValidator(_clip_why), Field(min_length=1, max_length=_WHY_CHARS)]


class _Args(Contract):
    why: Why


class Verdict(Contract):
    criterion: int = Field(ge=1, description="1-based number of the acceptance criterion")
    passed: bool
    reason: str = Field(min_length=1, max_length=300)


class AnswerArgs(_Args):
    question_id: str = Field(min_length=1, description="question id or #seq")
    body: str = Field(min_length=1, max_length=1500)


class DecisionArgs(_Args):
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=1500)
    tags: list[str] = Field(default_factory=list, max_length=10)


class MessageArgs(_Args):
    agent_id: str = Field(min_length=1, description="agent id or name")
    body: str = Field(min_length=1, max_length=1500)


class AcceptArgs(_Args):
    task_key: str
    verdicts: list[Verdict] = Field(min_length=1)
    note: str | None = Field(default=None, max_length=1000)


class ReworkArgs(_Args):
    task_key: str
    instructions: str = Field(min_length=1, max_length=1200)
    verdicts: list[Verdict] = Field(min_length=1)


class NewTask(Contract):
    """A task to add; the key is assigned by the supervisor (``T<n>``)."""

    title: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=1200)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=6)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[InputRef] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list, max_length=10)
    suggested_tools: list[SuggestedTool] = Field(default_factory=list)
    max_attempts: int = Field(default=2, ge=1, le=5)


class AddTaskArgs(_Args):
    task: NewTask
    replaces: str | None = Field(
        default=None, description="key of a failed or unstarted task this one replaces"
    )


class TaskFields(Contract):
    title: str | None = Field(default=None, min_length=1, max_length=80)
    goal: str | None = Field(default=None, min_length=1, max_length=1200)
    acceptance_criteria: list[str] | None = Field(default=None, min_length=1, max_length=6)
    depends_on: list[str] | None = None
    inputs: list[InputRef] | None = None
    outputs: list[OutputSpec] | None = None
    tags: list[str] | None = Field(default=None, max_length=10)
    suggested_tools: list[SuggestedTool] | None = None
    max_attempts: int | None = Field(default=None, ge=1, le=5)


class UpdateTaskArgs(_Args):
    task_key: str
    fields: TaskFields


class CancelTaskArgs(_Args):
    task_key: str
    reason: str = Field(min_length=1, max_length=500)


class ReassignArgs(_Args):
    task_key: str
    agent_id: str | None = Field(default=None, description="agent id or name; omit for any")


class PauseArgs(_Args):
    agent_id: str
    reason: str = Field(min_length=1, max_length=500)


class ResumeArgs(_Args):
    agent_id: str
    note: str = Field(min_length=1, max_length=1000)


class AskUserArgs(_Args):
    question: str = Field(min_length=1, max_length=1000)


class NoopArgs(Contract):
    reason: str = Field(min_length=1, max_length=_WHY_CHARS)
    why: Annotated[str | None, BeforeValidator(_clip_why)] = None


_TOOLS: Final[dict[str, tuple[type[Contract], str]]] = {
    "answer": (AnswerArgs, "Answer an open question on the board (resolves it)."),
    "post_decision": (DecisionArgs, "Post a binding decision for every agent."),
    "message_agent": (MessageArgs, "Send a direct note to one agent."),
    "accept_result": (AcceptArgs, "Accept a submitted result after checking every criterion."),
    "request_rework": (ReworkArgs, "Send a submitted result back with specific instructions."),
    "add_task": (AddTaskArgs, "Add a task to the plan (new plan version)."),
    "update_task": (UpdateTaskArgs, "Change a task that has not started (new plan version)."),
    "cancel_task": (CancelTaskArgs, "Cancel a task nothing unfinished depends on."),
    "reassign_task": (ReassignArgs, "Move a task to another agent."),
    "pause_agent": (PauseArgs, "Pause an agent between its steps."),
    "resume_agent": (ResumeArgs, "Resume a paused agent with a note."),
    "ask_user": (AskUserArgs, "Ask the human user a question."),
    "noop": (NoopArgs, "Do nothing."),
}
_PLAN_TOOLS: Final = frozenset({"add_task", "update_task", "cancel_task"})


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline ``$defs`` references (some providers reject ``$ref``) and drop titles."""
    defs: dict[str, Any] = schema.pop("$defs", {})

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                return walk(dict(defs[ref.removeprefix("#/$defs/")]))
            return {k: walk(v) for k, v in node.items() if k != "title"}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    result = walk(schema)
    assert isinstance(result, dict)
    return result


def _tool_specs() -> list[ToolSpec]:
    return [
        ToolSpec(name=name, description=desc, parameters=_inline_refs(model.model_json_schema()))
        for name, (model, desc) in _TOOLS.items()
    ]


SUPERVISOR_TOOLS: Final[list[ToolSpec]] = _tool_specs()


# -- context ------------------------------------------------------------------------------


def _load_prompts(directory: Path = PROMPTS_DIR) -> tuple[str, Template]:
    system = (directory / "supervisor_system.md").read_text(encoding="utf-8")
    run = Template((directory / "supervisor_run.md").read_text(encoding="utf-8"))
    return system, run


def _data(text: str, limit: int) -> str:
    """Untrusted text as one line that cannot close the ``<untrusted>`` block."""
    return _UNTRUSTED_TAG_RE.sub("[tag removed]", sanitize(text, limit))


def _render_plan(view: SupervisorView) -> str:
    st = view.sched
    lines: list[str] = []
    for t in st.tasks.values():
        agent = st.agents.get(t.assigned_agent_id or "") or st.agents.get(
            st.last_agent.get(t.key, "")
        )
        parts = [f"{t.key} [{t.status}] {_data(t.title, 80)}"]
        if t.depends_on:
            parts.append("after " + ", ".join(t.depends_on))
        if t.tags:
            parts.append("tags " + ", ".join(t.tags))
        if agent is not None:
            parts.append(f"agent {agent.name}")
        parts.append(f"attempt {t.attempt}/{t.max_attempts}, reworks {t.rework_count}")
        lines.append("- " + " · ".join(parts))
        lines.append(f"  goal: {_data(t.goal, 240)}")
    return "\n".join(lines) or "(no tasks yet)"


def _render_agents(view: SupervisorView) -> str:
    lines = []
    for a in view.sched.agents.values():
        task = view.current_task_of(a.id)
        doing = f", working on {task}" if task else ""
        lines.append(f"- {a.name} (id {a.id}): {a.status}{doing}")
    return "\n".join(lines) or "(no agents yet)"


def _render_review(view: SupervisorView, task: Task, max_reworks: int) -> list[str]:
    lines = [
        f'Review {task.key} "{_data(task.title, 80)}" (rework {task.rework_count} of '
        f"{max_reworks} used). Acceptance criteria:"
    ]
    lines += [f"  {i}. {_data(c, 300)}" for i, c in enumerate(task.acceptance_criteria, 1)]
    if task.outputs:
        lines.append(
            "  Expected outputs: "
            + "; ".join(f"{o.key} ({o.kind}): {_data(o.description, 120)}" for o in task.outputs)
        )
    result = task.result
    if result is None:
        lines.append("  (no result)")
        return lines
    lines.append("  <untrusted>")
    lines.append(f"  Summary: {_data(result.summary, 800)}")
    for key, value in result.outputs.items():
        lines.append(
            f"  Output {_data(key, 64)}: {_data(json.dumps(value, ensure_ascii=False), 800)}"
        )
    for artifact_id in result.artifact_ids:
        art = view.sched.artifacts.get(artifact_id)
        if art is None:
            lines.append(f"  Artifact {artifact_id}: (unknown)")
        else:
            lines.append(
                f"  Artifact {art.id}: {_data(art.name, 120)} ({art.mime}, {art.size_bytes} bytes)"
            )
    lines.append(f"  Confidence: {result.confidence}")
    if result.open_issues:
        lines.append("  Open issues: " + "; ".join(_data(i, 200) for i in result.open_issues))
    lines.append("  </untrusted>")
    return lines


def _render_trigger(view: SupervisorView, t: Trigger, config: SupervisorConfig) -> list[str]:
    head = f"- [{t.reason}] {_data(t.detail, 300)}"
    lines = [head]
    for entry_id in t.entry_ids:
        e = view.entries.get(entry_id)
        if e is not None:
            lines.append(f"  <untrusted>{_data(format_entry(e, body_chars=600), 900)}</untrusted>")
            lines.append(f"  (entry id {e.id}, status {e.status or '-'})")
            if t.reason in ("question", "unanswered_question"):
                names = [a.name for a in route_candidates(view, e)]
                lines.append(
                    "  Best placed to answer (by tags): " + (", ".join(names) or "nobody obvious")
                )
    if t.reason == "result_submitted" and t.task_key in view.tasks:
        lines += [
            "  " + line for line in _render_review(view, view.tasks[t.task_key], config.max_reworks)
        ]
    if t.reason == "task_failed" and t.task_key in view.tasks:
        dependents = view.unfinished_dependents(t.task_key)
        lines.append(f"  Unfinished tasks depending on it: {', '.join(dependents) or 'none'}.")
    return lines


def build_run_message(
    view: SupervisorView, triggers: Sequence[Trigger], config: SupervisorConfig, template: Template
) -> str:
    """The run message (SPEC §14.2) within ``config.context_budget_tokens``. The board
    and the action history are trimmed (oldest first) to fit."""
    trigger_lines = [line for t in triggers for line in _render_trigger(view, t, config)]
    board = [
        _data(format_entry(e, body_chars=200), 400)
        for e in view.recent_entries(config.board_entries)
    ]
    actions = [
        f"- {a.tool}({_data(json.dumps(a.args, ensure_ascii=False), 200)}) — why: "
        f"{_data(a.why, 200)} — result: {_data(a.result or '', 160)}"
        for a in view.actions[-config.recent_actions :]
    ]

    def render() -> str:
        return template.substitute(
            goal=_data(view.session.prompt, 2000),
            plan_version=view.plan_version,
            plan=_render_plan(view),
            agents=_render_agents(view),
            triggers="\n".join(trigger_lines) or "(none)",
            board="\n".join(board) or "(empty)",
            actions="\n".join(actions) or "(none yet)",
        )

    text = render()
    while estimate_tokens(text) > config.context_budget_tokens and (board or actions):
        if board:
            board.pop(0)
        else:
            actions.pop(0)
        text = render()
    return text


def _jpeg_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


# -- supervisor ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Outcome:
    text: str
    extra_args: dict[str, Any] = field(default_factory=dict)


_Step = Callable[[SupervisorView], EventPayload]
_Handler = Callable[[Any], Awaitable[_Outcome]]


class Supervisor:
    """The supervisor of one session. See the module docstring.

    ``run()`` returns when the session is stopped or failed, or after ``stop()``.
    """

    def __init__(
        self,
        *,
        session: Session,
        model: ModelClient,
        board: BoardService,
        events: EventWriter,
        store: EventStore,
        bus: EventBus,
        scheduler: TaskReassigner,
        control: AgentControl,
        frames: ArtifactStore | None = None,
        config: SupervisorConfig | None = None,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        prompts_dir: Path = PROMPTS_DIR,
    ) -> None:
        self.config = config or SupervisorConfig()
        self.view = SupervisorView.for_session(session)
        self.queue = TriggerQueue(self.config.debounce_s)
        self.runs = 0
        self._model = model
        self._board = board
        self._events = events
        self._store = store
        self._bus = bus
        self._scheduler = scheduler
        self._control = control
        self._frames = frames
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep
        self._system, self._template = _load_prompts(prompts_dir)
        self._inbox: asyncio.Queue[Event] = asyncio.Queue()
        self._stopped = asyncio.Event()
        self._running = False
        self._detect = False
        self._pending_detections: list[tuple[SupervisorTriggered, str]] = []
        self._review_retries: dict[str, int] = {}
        self._current_trigger: str | None = None
        self._actor = Actor.supervisor()
        self._log = log.bind(session_id=session.id, agent_id=SUPERVISOR_AGENT_ID)
        self._handlers: dict[str, _Handler] = {
            "answer": self._answer,
            "post_decision": self._post_decision,
            "message_agent": self._message_agent,
            "accept_result": self._accept_result,
            "request_rework": self._request_rework,
            "add_task": self._plan_tool("add_task"),
            "update_task": self._plan_tool("update_task"),
            "cancel_task": self._plan_tool("cancel_task"),
            "reassign_task": self._reassign_task,
            "pause_agent": self._pause_agent,
            "resume_agent": self._resume_agent,
            "ask_user": self._ask_user,
            "noop": self._noop,
        }

    @property
    def session_id(self) -> str:
        return self.view.session.id

    # -- public API -----------------------------------------------------------------

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("Supervisor.run() is already running")
        self._running = True
        feeder = asyncio.create_task(self._feed(), name=f"supervisor-feed-{self.session_id}")
        try:
            await self._start()
            while not self._stopped.is_set() and self.view.status not in _END_STATUSES:
                await self._step()
        finally:
            self._running = False
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)

    async def stop(self) -> None:
        """Make ``run()`` return after the current step (a run in flight finishes)."""
        self._stopped.set()

    def notify(self, event: Event) -> None:
        """Feed an event that did not come through the bus (optional)."""
        self._inbox.put_nowait(event)

    # -- input ----------------------------------------------------------------------

    async def _feed(self) -> None:
        async for record in self._bus.subscribe(self.session_id, include_ephemeral=False):
            if isinstance(record.item, EventEnvelope):
                await self._inbox.put(record.item)

    async def _start(self) -> None:
        """Fold the whole log. Raw events after the last one the supervisor already
        raised a trigger for are checked again, then unhandled triggers are queued."""
        history: list[Event] = []
        while True:
            batch = await self._store.read(self.session_id, history[-1].seq if history else 0, 5000)
            if not batch:
                break
            history.extend(batch)
        seq_of = {e.id: e.seq for e in history}
        detected = max(
            (
                seq_of.get(e.causation_id or "", 0)
                for e in history
                if isinstance(e.payload, SupervisorTriggered) and e.actor.type == "supervisor"
            ),
            default=0,
        )
        for event in history:
            self._detect = event.seq > detected
            self._observe(event)
        self._detect = True
        await self._flush_detections()
        self._log.info("supervisor_started", last_seq=self.view.last_seq, pending=len(self.queue))

    async def _absorb(self, event: Event) -> None:
        if event.session_id != self.session_id or event.seq <= self.view.last_seq:
            return
        if event.seq == self.view.last_seq + 1:
            self._observe(event)
        else:
            await self._catch_up()

    async def _catch_up(self) -> None:
        while True:
            batch = await self._store.read(self.session_id, self.view.last_seq, limit=500)
            if not batch:
                return
            for event in batch:
                if event.seq == self.view.last_seq + 1:
                    self._observe(event)

    def _observe(self, event: Event) -> None:
        self.view.apply(event)
        if isinstance(event.payload, SupervisorTriggered):
            trigger = self.view.triggers[event.id]
            if trigger.seq > self.view.handled_seq:
                self.queue.push(trigger, self._monotonic())
        elif isinstance(event.payload, SupervisorAction):
            self.queue.drop_handled(self.view.handled_seq)
        if self._detect:
            for payload in detect_triggers(event, self.view):
                self._pending_detections.append((payload, event.id))

    async def _flush_detections(self) -> None:
        while self._pending_detections:
            payload, cause = self._pending_detections.pop(0)
            if self.queue.has(_trigger_key(payload)):
                continue  # merged with a pending trigger: no second event
            probe = Trigger(
                event_id="",
                seq=0,
                reason=payload.reason,
                task_key=payload.task_key,
                entry_ids=tuple(payload.entry_ids),
            )
            if not self._relevant(probe):
                continue  # already resolved (e.g. found again after a restart)
            await self._emit(payload, causation_id=cause, correlation_id=self._correlation(payload))

    def _correlation(self, p: SupervisorTriggered) -> str | None:
        task = self.view.tasks.get(p.task_key or "")
        return task.id if task is not None else None

    async def _emit(
        self,
        payload: EventPayload,
        *,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        expect_last_seq: int | None = None,
    ) -> Event:
        draft = EventDraft(
            payload=payload,
            actor=self._actor,
            causation_id=causation_id,
            correlation_id=correlation_id,
        )
        if expect_last_seq is None:
            event = await self._events.record(self.session_id, draft)
        else:
            expected = expect_last_seq + 1

            def factory(seq: int) -> EventDraft:
                if seq != expected:
                    raise _StaleLog()
                return draft

            event = await self._events.record(self.session_id, factory)
        await self._absorb(event)
        return event

    # -- the decision loop ----------------------------------------------------------

    async def _step(self) -> None:
        now = self._monotonic()
        wake = now + self.config.tick_s
        due = self.queue.ready_at()
        if due is not None and self.view.status in _RUN_STATUSES:
            wake = min(wake, due)
        timeout = max(0.0, wake - now)
        stop_wait = asyncio.create_task(self._stopped.wait())
        get = asyncio.create_task(self._inbox.get())
        try:
            await asyncio.wait(
                {stop_wait, get}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stop_wait.cancel()
            if not get.done():
                get.cancel()
            await asyncio.gather(stop_wait, get, return_exceptions=True)
        # ``get`` may complete after ``wait`` returned; never drop its event
        received = get.done() and not get.cancelled() and get.exception() is None
        if received:
            await self._absorb(get.result())
        while not self._inbox.empty():
            await self._absorb(self._inbox.get_nowait())
        if self._stopped.is_set():
            return
        if not received:
            await self._catch_up()  # a quiet bus: pick up anything that was missed
        await self._flush_detections()
        await self._check_questions()
        await self._apply_decisions()
        if self.view.status in _RUN_STATUSES:
            batch = self.queue.take(self._monotonic())
            if batch:
                await self._run_batch(batch)

    async def _check_questions(self) -> None:
        """Unanswered agent-to-agent questions after ``question_timeout_s``."""
        now = self._now()
        limit = timedelta(seconds=self.config.question_timeout_s)
        for q in self.view.open_directed_questions():
            if q.id in self.view.unanswered_triggered or now - q.created_at < limit:
                continue
            asked = ", ".join(self.view.agent_names.get(a, "?") for a in q.to or [])
            await self._emit(
                SupervisorTriggered(
                    reason="unanswered_question",
                    detail=f"{q.author.name} asked {asked} {int(limit.total_seconds())} s ago: "
                    f"{sanitize(q.title, 200)}",
                    task_key=q.task_key,
                    agent_id=q.author.id,
                    entry_ids=[q.id],
                ),
                causation_id=None,
            )

    # -- runs -----------------------------------------------------------------------

    def _relevant(self, t: Trigger) -> bool:
        """``False`` for triggers whose cause has gone away (e.g. after a restart)."""
        v = self.view
        if t.reason == "result_submitted":
            task = v.tasks.get(t.task_key or "")
            return task is not None and task.status == "in_review"
        if t.reason == "task_failed":
            task = v.tasks.get(t.task_key or "")
            return task is not None and task.status == "failed"
        if t.reason in ("question", "unanswered_question", "blocker", "claim_conflict"):
            return any(
                (e := v.entries.get(i)) is not None and e.status == "open" for i in t.entry_ids
            )
        return True

    async def _run_batch(self, batch: list[Trigger]) -> None:
        triggers = [t for t in batch if self._relevant(t)]
        if not triggers:
            return
        self.runs += 1
        primary = max(triggers, key=lambda t: t.seq)
        self._current_trigger = primary.event_id
        log_ = self._log.bind(run=self.runs, triggers=[t.reason for t in triggers])
        log_.info("supervisor_run_started")
        for t in triggers:
            await self._start_review(t)
        images = await self._frames_for(triggers)
        content: list[TextPart | ImagePart] = [
            TextPart(text=build_run_message(self.view, triggers, self.config, self._template))
        ]
        for label, image in images:
            content += [TextPart(text=label), image]
        messages = [Message.text("system", self._system), Message(role="user", content=content)]
        recorded = 0
        tool_choice: Literal["required", "auto"] = "required"
        for _turn in range(self.config.max_turns):
            resp = await self._ask(messages, tool_choice)
            if resp is None:
                if recorded == 0:
                    self.queue.requeue(triggers, self._monotonic(), self.config.requeue_after_s)
                    log_.warning("supervisor_run_requeued")
                    return
                break
            if not resp.tool_calls:
                break
            messages.append(
                Message(
                    role="assistant",
                    content=[TextPart(text=resp.text)] if resp.text else [],
                    tool_calls=resp.tool_calls,
                )
            )
            for call in resp.tool_calls:
                if recorded >= self.config.max_actions_per_run:
                    result = "ERROR: action limit for this run reached; stop calling tools"
                else:
                    result, did_record = await self._execute(call)
                    recorded += did_record
                messages.append(Message.tool_result(call.id, result))
            if all(c.name == "noop" for c in resp.tool_calls):
                break
            tool_choice = "auto"
        if recorded == 0:
            await self._record(
                "noop", {"reason": "no action was needed"}, "No action was needed.", "ok"
            )
        await self._follow_up_reviews(triggers)
        log_.info("supervisor_run_finished", actions=recorded)

    async def _ask(
        self, messages: list[Message], tool_choice: Literal["required", "auto"]
    ) -> ModelResponse | None:
        request = ModelRequest(
            messages=messages,
            tools=SUPERVISOR_TOOLS,
            tool_choice=tool_choice,
            max_output_tokens=self.config.max_output_tokens,
            timeout_s=self.config.model_timeout_s,
        )
        waits = [*self.config.model_backoff_s, None]
        for wait in waits:
            try:
                return await self._model.complete(request)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # ModelError messages carry only the status and a truncated, redacted
                # provider message; redact again for anything else.
                self._log.warning(
                    "supervisor_model_failed",
                    error=type(exc).__name__,
                    detail=redact_secrets(str(exc))[:300],
                )
                if wait is not None:
                    await self._sleep(wait)
        return None

    async def _start_review(self, t: Trigger) -> None:
        if t.reason != "result_submitted" or t.task_key is None:
            return
        task = self.view.tasks.get(t.task_key)
        if task is None or (task.key, task.rework_count) in self.view.reviews_started:
            return
        await self._emit(
            TaskReviewStarted(task_id=task.id, task_key=task.key), correlation_id=task.id
        )

    async def _follow_up_reviews(self, triggers: Sequence[Trigger]) -> None:
        """A review the model left undecided is queued again, then handed to the user."""
        await self._catch_up()
        for t in triggers:
            if t.reason != "result_submitted" or not self._relevant(t):
                continue
            key = t.task_key or ""
            retries = self._review_retries.get(key, 0)
            if retries < self.config.max_review_retries:
                self._review_retries[key] = retries + 1
                await self._emit(
                    SupervisorTriggered(
                        reason="result_submitted",
                        detail=f"{key}: the review needs a decision (accept or rework)",
                        task_key=key,
                        agent_id=t.agent_id,
                    ),
                    causation_id=t.event_id,
                )
            else:
                self._review_retries.pop(key, None)
                question = f"Please review {key}: the supervisor could not decide."
                await self._record(
                    "ask_user", {"question": question}, "The review stayed undecided.", "recorded"
                )

    async def _frames_for(self, triggers: Sequence[Trigger]) -> list[tuple[str, ImagePart]]:
        """Up to ``max_frames`` screenshots for stuck and review triggers."""
        if self._frames is None:
            return []
        wanted: list[tuple[str, Frame]] = []
        for t in triggers:
            frames: list[Frame] = []
            if t.reason == "stuck":
                ids = self.view.stuck_frames.get(t.causation_id or "", [])
                frames = [self.view.frames[i] for i in ids if i in self.view.frames]
                if not frames and t.agent_id:
                    frames = self.view.agent_frames(t.agent_id, self.config.max_frames)
            elif t.reason == "result_submitted":
                task = self.view.tasks.get(t.task_key or "")
                final = task.result.final_frame_id if task and task.result else None
                if final and final in self.view.frames:
                    frames = [self.view.frames[final]]
            for f in frames:
                name = self.view.agent_names.get(f.agent_id, "agent")
                wanted.append((f"Screenshot ({t.reason}): {name}, step {f.step}", f))
        out: list[tuple[str, ImagePart]] = []
        for label, frame in wanted[-self.config.max_frames :]:
            try:
                data = await self._frames.get(frame.storage_key_small)
                w, h = await asyncio.to_thread(_jpeg_size, data)
            except Exception as exc:
                self._log.warning("frame_unavailable", frame_id=frame.id, error=type(exc).__name__)
                continue
            out.append((label, ImagePart(jpeg_bytes=data, width=w, height=h)))
        return out

    # -- tools ----------------------------------------------------------------------

    async def _execute(self, call: ToolCall) -> tuple[str, int]:
        """Run one tool call. Returns the tool result and whether an action was recorded
        (invalid calls are only reported back to the model)."""
        entry = _TOOLS.get(call.name)
        if entry is None:
            return f"ERROR: unknown tool {call.name!r}; valid tools: {', '.join(_TOOLS)}", 0
        model_cls, _ = entry
        try:
            args = model_cls.model_validate(call.arguments)
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}"
                for e in exc.errors()[:5]
            )
            return f"ERROR: invalid arguments for {call.name}: {problems}"[:_RESULT_CHARS], 0
        why = getattr(args, "why", None) or getattr(args, "reason", "") or call.name
        try:
            outcome = await self._handlers[call.name](args)
            text = outcome.text
            extra = outcome.extra_args
        except AppError as exc:
            text, extra = f"ERROR: {exc}", {}
        dumped = args.model_dump(mode="json", exclude={"why"})
        await self._record(call.name, {**dumped, **extra}, why, text)
        return text[:_RESULT_CHARS], 1

    async def _record(self, tool: str, args: dict[str, Any], why: str, result: str) -> None:
        clipped = str(_clip_why(why)) or tool
        await self._emit(
            SupervisorAction(
                tool=tool,
                args=json.loads(json.dumps(args)),
                why=clipped,
                result=result[:_RESULT_CHARS],
                trigger_event_id=self._current_trigger,
            )
        )

    def _agent_or_error(self, ref: str) -> Agent:
        agent = self.view.agent(ref)
        if agent is None:
            raise InvalidRequestError(
                f"no agent {ref!r}; agents: {', '.join(self.view.agent_names.values())}"
            )
        return agent

    def _task_or_error(self, key: str) -> Task:
        task = self.view.tasks.get(key.strip())
        if task is None:
            raise NotFoundError(f"no task {key!r}; tasks: {', '.join(self.view.tasks)}")
        return task

    async def _answer(self, args: AnswerArgs) -> _Outcome:
        q = self.view.entry(args.question_id)
        if q is None or q.kind != "question":
            raise NotFoundError(f"{args.question_id} is not a question on the board")
        to = (
            [q.author.id] if q.to is not None and q.author.type == "agent" and q.author.id else None
        )
        entry = await posting.answer_as_supervisor(
            self._board, self.session_id, q.id, args.body, to=to
        )
        return _Outcome(f"answered #{q.seq} with #{entry.seq}")

    async def _post_decision(self, args: DecisionArgs) -> _Outcome:
        entry = await posting.post_decision(
            self._board, self.session_id, args.title, args.body, tags=args.tags
        )
        return _Outcome(f"decision posted as #{entry.seq}")

    async def _message_agent(self, args: MessageArgs) -> _Outcome:
        agent = self._agent_or_error(args.agent_id)
        entry = await posting.message_agent(self._board, self.session_id, agent.id, args.body)
        return _Outcome(f"message #{entry.seq} sent to {agent.name}")

    def _check_verdicts(self, task: Task, verdicts: Sequence[Verdict]) -> list[Verdict]:
        """Exactly one verdict per criterion; returns the failed ones."""
        n = len(task.acceptance_criteria)
        numbers = sorted(v.criterion for v in verdicts)
        if numbers != list(range(1, n + 1)):
            raise InvalidRequestError(
                f"give exactly one verdict for each criterion 1..{n} of {task.key}"
            )
        return [v for v in sorted(verdicts, key=lambda v: v.criterion) if not v.passed]

    def _in_review(self, key: str) -> Task:
        task = self._task_or_error(key)
        if task.status != "in_review" or task.result is None:
            raise ConflictError(f"{task.key} is {task.status}, not waiting for review")
        return task

    async def _accept_result(self, args: AcceptArgs) -> _Outcome:
        task = self._in_review(args.task_key)
        failed = self._check_verdicts(task, args.verdicts)
        if failed and task.rework_count < self.config.max_reworks:
            numbers = ", ".join(str(v.criterion) for v in failed)
            raise InvalidRequestError(
                f"criteria {numbers} failed; use request_rework "
                f"({task.rework_count} of {self.config.max_reworks} reworks used)"
            )
        note = args.note or ""
        if failed:
            issues = "; ".join(f"criterion {v.criterion}: {v.reason}" for v in failed)
            note = f"{note} Open issues: {issues}".strip()
        await self._emit(
            TaskAccepted(task_id=task.id, task_key=task.key, note=note or None),
            correlation_id=task.id,
        )
        return _Outcome(f"accepted {task.key}" + (" with open issues" if failed else ""))

    async def _request_rework(self, args: ReworkArgs) -> _Outcome:
        task = self._in_review(args.task_key)
        failed = self._check_verdicts(task, args.verdicts)
        if not failed:
            raise InvalidRequestError("every criterion passed; use accept_result")
        agent_id = self.view.sched.last_agent.get(task.key)
        criteria = ", ".join(str(v.criterion) for v in failed)
        if task.rework_count >= self.config.max_reworks:
            await self._emit(
                TaskFailed(
                    task_id=task.id,
                    task_key=task.key,
                    reason=f"review: rework limit ({self.config.max_reworks}) reached; "
                    f"criteria {criteria} still fail",
                    attempt=task.attempt,
                    will_retry=False,
                    agent_id=agent_id,
                ),
                correlation_id=task.id,
            )
            return _Outcome(
                f"rework limit reached: {task.key} marked failed (replace it with add_task)"
            )
        count = task.rework_count + 1
        await self._emit(
            TaskReworkRequested(
                task_id=task.id,
                task_key=task.key,
                agent_id=agent_id,
                instructions=args.instructions,
                rework_count=count,
            ),
            correlation_id=task.id,
        )
        if agent_id is not None:
            # the agent loop does not read task.rework_requested; the note reaches its digest
            try:
                await posting.message_agent(
                    self._board,
                    self.session_id,
                    agent_id,
                    f"Rework {task.key} (criteria {criteria} failed): {args.instructions}",
                )
            except AppError as exc:
                self._log.warning("rework_note_failed", error=str(exc))
        return _Outcome(f"rework {count} of {self.config.max_reworks} requested for {task.key}")

    async def _reassign_task(self, args: ReassignArgs) -> _Outcome:
        task = self._task_or_error(args.task_key)
        agent = self._agent_or_error(args.agent_id) if args.agent_id else None
        outcome = await self._scheduler.reassign(task.key, agent.id if agent else None)
        target = agent.name if agent else "another agent"
        return _Outcome(f"{task.key} {outcome} ({target})")

    async def _pause_agent(self, args: PauseArgs) -> _Outcome:
        agent = self._agent_or_error(args.agent_id)
        await self._control.pause(agent.id, args.reason)
        return _Outcome(f"{agent.name} paused")

    async def _resume_agent(self, args: ResumeArgs) -> _Outcome:
        agent = self._agent_or_error(args.agent_id)
        await self._control.resume(agent.id, args.note)
        await posting.message_agent(self._board, self.session_id, agent.id, args.note)
        return _Outcome(f"{agent.name} resumed")

    async def _ask_user(self, args: AskUserArgs) -> _Outcome:
        return _Outcome("question recorded for the user; they answer by steering")

    async def _noop(self, args: NoopArgs) -> _Outcome:
        return _Outcome("ok")

    # -- plan changes ---------------------------------------------------------------

    def _plan_tool(self, tool: str) -> _Handler:
        async def handler(args: Any) -> _Outcome:
            if self.view.status not in _RUN_STATUSES:
                raise ConflictError(
                    f"the session is {self.view.status}; the plan cannot change now"
                )
            self._plan_steps(tool, args)  # validate now, so the model gets errors at once
            if self.config.require_human_for_plan_changes:
                return await self._request_plan_approval(tool, args)
            return _Outcome(await self._apply_plan_change(tool, args))

        return handler

    async def _request_plan_approval(self, tool: str, args: Any) -> _Outcome:
        now = self._now()
        approval = Approval(
            id=new_id(),
            session_id=self.session_id,
            agent_id=SUPERVISOR_AGENT_ID,
            task_key=getattr(args, "task_key", None) or getattr(args, "replaces", None),
            action_summary=_plan_summary(tool, args)[:500],
            risk="medium",
            created_at=now,
            expires_at=now + timedelta(seconds=self.config.approval_ttl_s),
        )
        await self._emit(ApprovalRequested(approval=approval), correlation_id=approval.id)
        return _Outcome(f"pending approval {approval.id}", {"approval_id": approval.id})

    async def _apply_decisions(self) -> None:
        """Apply (or drop) supervisor plan changes the user has decided on."""
        v = self.view
        for approval_id, approval in list(v.decisions.items()):
            change = v.pending_changes.get(approval_id)
            if change is None or approval_id in v.resolved_changes:
                continue
            args_cls, _ = _TOOLS[change.tool]
            extra = {"approval_id": approval_id}
            if approval.status != "approved":
                note = f": {approval.note}" if approval.note else ""
                result = f"not applied: {approval.status}{note}"
            elif v.status not in _RUN_STATUSES:
                continue  # paused: apply after resume
            else:
                try:
                    args = args_cls.model_validate({**change.args, "why": change.why})
                    result = "applied: " + await self._apply_plan_change(change.tool, args)
                except (AppError, ValidationError) as exc:
                    result = f"not applied: {exc}"
            self._current_trigger = None
            await self._record(change.tool, {**change.args, **extra}, change.why, result)

    async def _apply_plan_change(self, tool: str, args: Any) -> str:
        summary, steps = self._plan_steps(tool, args)
        for i, step in enumerate(steps):
            for _attempt in range(self.config.cas_attempts):
                await self._catch_up()
                try:
                    payload = step(self.view)
                except AppError as exc:
                    if i == 0:
                        raise
                    raise ConflictError(f"{summary} was only partly applied: {exc}") from exc
                try:
                    await self._emit(payload, expect_last_seq=self.view.last_seq)
                except _StaleLog:
                    continue
                break
            else:
                raise ConflictError("the session changed too often; try again")
        return summary

    def _plan_steps(self, tool: str, args: Any) -> tuple[str, list[_Step]]:
        """Validate a plan change against the current state and return its summary and
        its event steps. Each step re-checks its precondition on the state it is
        applied to and builds the payload from that state."""
        v = self.view
        base = v.plan_version
        version = base + 1
        if tool == "add_task":
            return self._add_steps(args, base, version)
        if tool == "update_task":
            return self._update_steps(args, base, version)
        return self._cancel_steps(args, base, version)

    def _drafts(self, exclude: Iterable[str] = ()) -> list[TaskDraft]:
        skip = set(exclude)
        return [
            t.to_draft()
            for t in self.view.tasks.values()
            if t.status != "cancelled" and t.key not in skip
        ]

    def _validate(self, drafts: Sequence[TaskDraft]) -> None:
        errors = validate_plan(drafts)
        if errors:
            raise InvalidRequestError(
                "the plan would be invalid: " + "; ".join(e.message for e in errors[:5])
            )

    def _check_deps(self, deps: Iterable[str], ignore: str | None = None) -> None:
        for dep in deps:
            task = self.view.tasks.get(dep)
            if dep != ignore and task is not None and task.status in ("failed", "cancelled"):
                raise InvalidRequestError(f"{dep} is {task.status}; a task cannot depend on it")

    def _plan_events(
        self, base: int, version: int, drafts: list[TaskDraft], why: str, approve: bool
    ) -> list[_Step]:
        prev = self.view.latest_proposal
        proposal = PlanProposal(
            tasks=drafts,
            rationale=f"Supervisor change: {why}"[:4000],
            assumptions=list(prev.assumptions) if prev else [],
            questions_for_user=list(prev.questions_for_user) if prev else [],
        )

        def updated(view: SupervisorView) -> EventPayload:
            if view.plan_version != base:
                raise ConflictError(
                    f"the plan changed (now version {view.plan_version}); try again"
                )
            return SessionPlanUpdated(
                plan_version=version, proposal=proposal, source="supervisor", reason=why
            )

        steps: list[_Step] = [updated]
        if approve:

            def approved(view: SupervisorView) -> EventPayload:
                if view.plan_version != version:
                    raise ConflictError("the plan changed before it was approved")
                return SessionApproved(plan_version=version)

            steps.append(approved)
        return steps

    def _add_steps(self, args: AddTaskArgs, base: int, version: int) -> tuple[str, list[_Step]]:
        v = self.view
        old = self._task_or_error(args.replaces) if args.replaces else None
        if old is not None and old.status not in _REPLACEABLE:
            raise ConflictError(
                f"{old.key} is {old.status}; only failed or unstarted tasks can be replaced"
            )
        known: list[TaskDraft] = [t.to_draft() for t in v.tasks.values()]
        for drafts, _ in v.sched.proposals.values():
            known.extend(drafts)
        key = f"T{next_key_number(known)}"
        self._check_deps(args.task.depends_on)
        new = TaskDraft(key=key, **args.task.model_dump())
        rewired: dict[str, TaskDraft] = {}
        if old is not None:
            for t in v.tasks.values():
                if old.key in t.depends_on and t.status not in FINISHED_TASK_STATUSES:
                    rewired[t.key] = _rewire(t.to_draft(), old.key, key)
        drafts = [rewired.get(d.key, d) for d in self._drafts(exclude=[old.key] if old else [])]
        drafts.append(new)
        self._validate(drafts)
        summary = f'plan v{version}: added {key} "{new.title}"'
        if old is not None:
            summary += f" replacing {old.key}"
        steps = self._plan_events(base, version, drafts, args.why, approve=True)
        for dep_key, draft in rewired.items():
            steps.append(self._task_update_step(dep_key, draft, version, {"pending"}))
        if old is not None:
            steps.append(self._cancel_step(old.key, f"replaced by {key}: {args.why}", _REPLACEABLE))
        return summary, steps

    def _update_steps(
        self, args: UpdateTaskArgs, base: int, version: int
    ) -> tuple[str, list[_Step]]:
        task = self._task_or_error(args.task_key)
        if task.status not in _EDITABLE:
            raise ConflictError(
                f"{task.key} is {task.status}; only pending or ready tasks can be updated"
            )
        changes = args.fields.model_dump(exclude_none=True)
        if not changes:
            raise InvalidRequestError("no fields to change")
        if "depends_on" in changes:
            self._check_deps(changes["depends_on"])
        draft = TaskDraft.model_validate({**task.to_draft().model_dump(), **changes})
        drafts = [draft if d.key == task.key else d for d in self._drafts()]
        self._validate(drafts)
        summary = f"plan v{version}: updated {task.key} ({', '.join(changes)})"
        steps = [self._task_update_step(task.key, draft, version, _EDITABLE)]
        steps += self._plan_events(base, version, drafts, args.why, approve=False)
        return summary, steps

    def _cancel_steps(
        self, args: CancelTaskArgs, base: int, version: int
    ) -> tuple[str, list[_Step]]:
        task = self._task_or_error(args.task_key)
        if task.status not in _CANCELLABLE:
            raise ConflictError(
                f"{task.key} is {task.status}; only pending, ready, failed or in-review tasks "
                "can be cancelled (reassign or message the agent of a running task)"
            )
        dependents = self.view.unfinished_dependents(task.key)
        if dependents:
            raise ConflictError(
                f"unfinished tasks depend on {task.key}: {', '.join(dependents)}; update them "
                "(remove the dependency) or cancel them first"
            )
        drafts = self._drafts(exclude=[task.key])
        self._validate(drafts)
        summary = f"plan v{version}: cancelled {task.key}"
        steps = [self._cancel_step(task.key, args.reason, _CANCELLABLE)]
        steps += self._plan_events(base, version, drafts, args.why, approve=False)
        return summary, steps

    def _task_update_step(
        self, key: str, draft: TaskDraft, version: int, allowed: frozenset[str] | set[str]
    ) -> _Step:
        def step(view: SupervisorView) -> EventPayload:
            task = view.tasks.get(key)
            if task is None or task.status not in allowed:
                status = task.status if task else "missing"
                raise ConflictError(f"{key} is now {status}")
            before = task.to_draft().model_dump(mode="json")
            after = draft.model_dump(mode="json")
            changed = [name for name in after if before.get(name) != after[name]]
            snapshot = Task.model_validate(
                {**task.model_dump(), **draft.model_dump(), "plan_version": version}
            )
            return TaskUpdated(task=snapshot, changed_fields=changed)

        return step

    def _cancel_step(self, key: str, reason: str, allowed: frozenset[str]) -> _Step:
        def step(view: SupervisorView) -> EventPayload:
            task = view.tasks.get(key)
            if task is None or task.status not in allowed:
                status = task.status if task else "missing"
                raise ConflictError(f"{key} is now {status}")
            if view.unfinished_dependents(key):
                raise ConflictError(f"tasks depend on {key} again")
            return TaskCancelled(
                task_id=task.id, task_key=key, reason=f"supervisor: {reason}"[:1000]
            )

        return step


def _rewire(draft: TaskDraft, old: str, new: str) -> TaskDraft:
    depends_on = list(dict.fromkeys(new if d == old else d for d in draft.depends_on))
    inputs = [
        InputRef(from_task=new if i.from_task == old else i.from_task, output_key=i.output_key)
        for i in draft.inputs
    ]
    return draft.model_copy(update={"depends_on": depends_on, "inputs": inputs})


def _plan_summary(tool: str, args: Any) -> str:
    if isinstance(args, AddTaskArgs):
        text = f'Add task "{args.task.title}"'
        if args.replaces:
            text += f" replacing {args.replaces}"
    elif isinstance(args, UpdateTaskArgs):
        text = f"Update {args.task_key}: {', '.join(args.fields.model_dump(exclude_none=True))}"
    elif isinstance(args, CancelTaskArgs):
        text = f"Cancel {args.task_key}: {args.reason}"
    else:
        text = tool
    return f"Supervisor plan change — {text}. Why: {args.why}"
