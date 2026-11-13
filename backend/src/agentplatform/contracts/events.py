"""Event envelope, one payload model per event type, ephemeral bus messages (SPEC §7).

Design:

* Every persisted event type has a payload model (subclass of ``EventPayload``,
  with ``event_type`` as a class variable) and an envelope model (subclass of
  ``EventEnvelope`` with ``type: Literal[...]`` and a typed ``payload``).
* ``Event`` is the discriminated union of all envelopes on ``type``. Use
  ``EVENT_ADAPTER`` to validate/serialize any event.
* Producers build an ``EventDraft(payload=..., actor=...)``; the event store
  assigns ``id``, ``seq`` and ``ts`` and calls ``build_event``.
* ``agent.heartbeat`` and live thumbnails are never persisted, so they are not
  ``Event``s: they are ``Heartbeat`` / ``Thumbnail`` ephemeral messages on the bus.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Annotated, Any, ClassVar, Literal, cast

from pydantic import Field, JsonValue, TypeAdapter

from agentplatform.contracts.agent import Agent, SandboxRef
from agentplatform.contracts.artifact import Approval, Artifact, Frame
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.common import (
    Actor,
    AgentStatus,
    BoardEntryStatus,
    Contract,
    SandboxProviderName,
    UtcDatetime,
    new_id,
    utcnow,
)
from agentplatform.contracts.plan import PlanDiff, PlanProposal
from agentplatform.contracts.session import Session, Usage
from agentplatform.contracts.task import Task, TaskResult

__all__ = [
    "EPHEMERAL_ADAPTER",
    "EVENT_ADAPTER",
    "EVENT_TYPES",
    "PAYLOAD_TYPES",
    "ActionRecord",
    "BudgetMetric",
    "EphemeralMessage",
    "Event",
    "EventDraft",
    "EventEnvelope",
    "EventPayload",
    "Heartbeat",
    "StuckReason",
    "SupervisorTriggerReason",
    "Thumbnail",
    "build_event",
]


class EventPayload(Contract):
    """Base class for event payloads. ``event_type`` names the event."""

    event_type: ClassVar[str]


class EventEnvelope(Contract):
    """Fields shared by every event (SPEC §7.1)."""

    id: str
    session_id: str
    seq: int = Field(ge=1)
    ts: UtcDatetime
    actor: Actor
    causation_id: str | None = None
    """Id of the event or command that caused this event. On ``session.plan_proposed``
    and ``session.plan_updated`` it is the ``command_id`` of the originating request
    (the ``CommandAccepted.command_id`` the API returned), so the UI can match a
    stored plan to its ``PUT /plan``, refine or follow-up request; ``None`` when no
    request caused it (e.g. a supervisor plan change)."""
    correlation_id: str | None = None


# --- session ---------------------------------------------------------------------------


class SessionCreated(EventPayload):
    event_type = "session.created"
    session: Session


class SessionPlanningStarted(EventPayload):
    event_type = "session.planning_started"
    reason: Literal["initial", "refine", "followup"]
    instruction: str | None = None


class SessionPlanProposed(EventPayload):
    """The planner produced a proposal. It becomes plan ``plan_version``.

    The envelope's ``causation_id`` is the id of the command that ran the planner
    (plan_session, refine_plan or followup)."""

    event_type = "session.plan_proposed"
    plan_version: int = Field(ge=1)
    proposal: PlanProposal
    diff: PlanDiff | None = None
    """Diff against the previous version (refine/followup); ``None`` for the first plan."""


class SessionPlanningFailed(EventPayload):
    """The planner failed after its repair attempt. The session stays in ``planning``
    (status unchanged) so the user can retry or edit; ``errors`` are PlanError dumps."""

    event_type = "session.planning_failed"
    reason: str
    errors: list[dict[str, JsonValue]] = Field(default_factory=list)


class SessionPlanUpdated(EventPayload):
    """A plan version was stored that did not come from the planner (user edit via
    ``PUT /plan`` or a supervisor plan change).

    For a user edit the envelope's ``causation_id`` is the ``command_id`` returned by
    that ``PUT /plan``; ``None`` for supervisor changes."""

    event_type = "session.plan_updated"
    plan_version: int = Field(ge=1)
    proposal: PlanProposal
    source: Literal["user", "supervisor"]
    reason: str | None = None


class SessionApproved(EventPayload):
    event_type = "session.approved"
    plan_version: int = Field(ge=1)


class SessionPaused(EventPayload):
    event_type = "session.paused"
    reason: str | None = None


class SessionResumed(EventPayload):
    event_type = "session.resumed"


class SessionCompleted(EventPayload):
    event_type = "session.completed"
    summary: str | None = None


class SessionFailed(EventPayload):
    event_type = "session.failed"
    reason: str


class SessionStopped(EventPayload):
    event_type = "session.stopped"
    reason: Literal["user", "budget", "idle", "error"]
    detail: str | None = None


BudgetMetric = Literal["total_steps", "cost_usd", "tokens", "wall_minutes"]


class SessionBudgetWarning(EventPayload):
    event_type = "session.budget_warning"
    metric: BudgetMetric
    used: float
    limit: float


class SessionBudgetExceeded(EventPayload):
    event_type = "session.budget_exceeded"
    metric: BudgetMetric
    used: float
    limit: float


class SessionModelUsage(EventPayload):
    """Usage of one model call outside the agent loop (planner, supervisor, summarizer).
    Agent calls are counted in ``agent.step``; together they are the session's usage
    and what the token and cost budgets meter. ``usage.steps`` is always 0."""

    event_type = "session.model_usage"
    role: Literal["planner", "supervisor", "summarizer"]
    model: str = Field(max_length=200)
    agent_id: str | None = None
    """The agent a summarizer call worked for; ``None`` for planner and supervisor."""
    usage: Usage = Field(default_factory=Usage)


# --- task ------------------------------------------------------------------------------


class _TaskRef(EventPayload):
    task_id: str
    task_key: str


class TaskCreated(EventPayload):
    event_type = "task.created"
    task: Task


class TaskUpdated(EventPayload):
    """Full snapshot of a task after a change, plus the names of changed fields."""

    event_type = "task.updated"
    task: Task
    changed_fields: list[str] = Field(default_factory=list)


class TaskReady(_TaskRef):
    event_type = "task.ready"


class TaskAssigned(_TaskRef):
    """Addressed to exactly one agent (SPEC §12.4)."""

    event_type = "task.assigned"
    agent_id: str
    attempt: int = Field(ge=1)
    ack_deadline: UtcDatetime
    lease_expires_at: UtcDatetime


class TaskAssignmentAcked(_TaskRef):
    event_type = "task.assignment_acked"
    agent_id: str
    attempt: int = Field(ge=1)


class TaskStarted(_TaskRef):
    event_type = "task.started"
    agent_id: str
    attempt: int = Field(ge=1)


class TaskResultSubmitted(_TaskRef):
    event_type = "task.result_submitted"
    agent_id: str
    result: TaskResult


class TaskReviewStarted(_TaskRef):
    event_type = "task.review_started"


class TaskAccepted(_TaskRef):
    event_type = "task.accepted"
    note: str | None = None


class TaskReworkRequested(_TaskRef):
    event_type = "task.rework_requested"
    agent_id: str | None = None
    instructions: str
    rework_count: int = Field(ge=1)


class TaskFailed(_TaskRef):
    event_type = "task.failed"
    reason: str
    attempt: int = Field(ge=0)
    will_retry: bool
    agent_id: str | None = None


class TaskCancelled(_TaskRef):
    event_type = "task.cancelled"
    reason: str


# --- agent -----------------------------------------------------------------------------


class AgentCreated(EventPayload):
    event_type = "agent.created"
    agent: Agent


class AgentSandboxBooting(EventPayload):
    event_type = "agent.sandbox_booting"
    agent_id: str
    provider: SandboxProviderName


class AgentSandboxReady(EventPayload):
    event_type = "agent.sandbox_ready"
    agent_id: str
    sandbox: SandboxRef
    stream_url: str
    interactive_stream_url: str


class ActionRecord(Contract):
    """One executed tool call, as stored in ``agent.step``."""

    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    result: str = Field(default="", max_length=600)
    is_error: bool = False
    latency_ms: int | None = None


class AgentStep(EventPayload):
    """One iteration of the agent loop (SPEC §10.1)."""

    event_type = "agent.step"
    agent_id: str
    task_id: str
    task_key: str
    step: int = Field(ge=1)
    coordination_calls: list[ActionRecord] = Field(default_factory=list)
    action: ActionRecord | None = None
    """The desktop or control call of this step; ``None`` if the model produced none."""
    reasoning_excerpt: str = Field(default="", max_length=400)
    frame: Frame | None = None
    usage: Usage = Field(default_factory=Usage)
    model_latency_ms: int = Field(default=0, ge=0)


class AgentStatusChanged(EventPayload):
    event_type = "agent.status_changed"
    agent_id: str
    status: AgentStatus
    previous: AgentStatus | None = None
    reason: str | None = None


StuckReason = Literal["screen_unchanged", "repetition", "no_progress", "error_streak", "no_tool"]


class AgentStuckDetected(EventPayload):
    event_type = "agent.stuck_detected"
    agent_id: str
    task_key: str | None = None
    reason: StuckReason
    detail: str = ""
    frame_ids: list[str] = Field(default_factory=list, max_length=3)


class AgentError(EventPayload):
    event_type = "agent.error"
    agent_id: str
    error: str
    recoverable: bool = True


class AgentStopped(EventPayload):
    event_type = "agent.stopped"
    agent_id: str
    reason: str


# --- board -----------------------------------------------------------------------------


class BoardEntryPosted(EventPayload):
    event_type = "board.entry_posted"
    entry: BoardEntry


class BoardEntryStatusChanged(EventPayload):
    event_type = "board.entry_status_changed"
    entry_id: str
    status: BoardEntryStatus
    previous: BoardEntryStatus | None = None
    reason: str | None = None


# --- artifacts -------------------------------------------------------------------------


class ArtifactPublished(EventPayload):
    event_type = "artifact.published"
    artifact: Artifact


class ArtifactFetched(EventPayload):
    event_type = "artifact.fetched"
    artifact_id: str
    agent_id: str
    sandbox_path: str


# --- human -----------------------------------------------------------------------------


class UserSteer(EventPayload):
    """A steering message. ``agent_id=None`` addresses every agent and the supervisor.
    ``entry_id`` is the board note that carries it into digests."""

    event_type = "user.steer"
    agent_id: str | None = None
    text: str = Field(min_length=1, max_length=1500)
    entry_id: str | None = None


class UserTakeoverStarted(EventPayload):
    event_type = "user.takeover_started"
    agent_id: str


class UserTakeoverEnded(EventPayload):
    event_type = "user.takeover_ended"
    agent_id: str


class ApprovalRequested(EventPayload):
    event_type = "approval.requested"
    approval: Approval


class ApprovalDecided(EventPayload):
    event_type = "approval.decided"
    approval: Approval


# --- supervisor ------------------------------------------------------------------------

SupervisorTriggerReason = Literal[
    "result_submitted",
    "question",
    "unanswered_question",
    "blocker",
    "claim_conflict",
    "stuck",
    "task_failed",
    "plan_impact",
    "user_message",
    "budget_warning",
]


class SupervisorTriggered(EventPayload):
    event_type = "supervisor.triggered"
    reason: SupervisorTriggerReason
    detail: str = ""
    task_key: str | None = None
    agent_id: str | None = None
    entry_ids: list[str] = Field(default_factory=list)


class SupervisorAction(EventPayload):
    event_type = "supervisor.action"
    tool: str
    args: dict[str, JsonValue] = Field(default_factory=dict)
    why: str = Field(max_length=400)
    result: str | None = Field(default=None, max_length=600)
    trigger_event_id: str | None = None


# --- envelopes -------------------------------------------------------------------------
# One envelope per payload. Keep this list in sync with the payloads above; the
# registry check at the bottom of this module fails at import time if they diverge.


class SessionCreatedEvent(EventEnvelope):
    type: Literal["session.created"] = "session.created"
    payload: SessionCreated


class SessionPlanningStartedEvent(EventEnvelope):
    type: Literal["session.planning_started"] = "session.planning_started"
    payload: SessionPlanningStarted


class SessionPlanProposedEvent(EventEnvelope):
    type: Literal["session.plan_proposed"] = "session.plan_proposed"
    payload: SessionPlanProposed


class SessionPlanningFailedEvent(EventEnvelope):
    type: Literal["session.planning_failed"] = "session.planning_failed"
    payload: SessionPlanningFailed


class SessionPlanUpdatedEvent(EventEnvelope):
    type: Literal["session.plan_updated"] = "session.plan_updated"
    payload: SessionPlanUpdated


class SessionApprovedEvent(EventEnvelope):
    type: Literal["session.approved"] = "session.approved"
    payload: SessionApproved


class SessionPausedEvent(EventEnvelope):
    type: Literal["session.paused"] = "session.paused"
    payload: SessionPaused


class SessionResumedEvent(EventEnvelope):
    type: Literal["session.resumed"] = "session.resumed"
    payload: SessionResumed


class SessionCompletedEvent(EventEnvelope):
    type: Literal["session.completed"] = "session.completed"
    payload: SessionCompleted


class SessionFailedEvent(EventEnvelope):
    type: Literal["session.failed"] = "session.failed"
    payload: SessionFailed


class SessionStoppedEvent(EventEnvelope):
    type: Literal["session.stopped"] = "session.stopped"
    payload: SessionStopped


class SessionBudgetWarningEvent(EventEnvelope):
    type: Literal["session.budget_warning"] = "session.budget_warning"
    payload: SessionBudgetWarning


class SessionBudgetExceededEvent(EventEnvelope):
    type: Literal["session.budget_exceeded"] = "session.budget_exceeded"
    payload: SessionBudgetExceeded


class SessionModelUsageEvent(EventEnvelope):
    type: Literal["session.model_usage"] = "session.model_usage"
    payload: SessionModelUsage


class TaskCreatedEvent(EventEnvelope):
    type: Literal["task.created"] = "task.created"
    payload: TaskCreated


class TaskUpdatedEvent(EventEnvelope):
    type: Literal["task.updated"] = "task.updated"
    payload: TaskUpdated


class TaskReadyEvent(EventEnvelope):
    type: Literal["task.ready"] = "task.ready"
    payload: TaskReady


class TaskAssignedEvent(EventEnvelope):
    type: Literal["task.assigned"] = "task.assigned"
    payload: TaskAssigned


class TaskAssignmentAckedEvent(EventEnvelope):
    type: Literal["task.assignment_acked"] = "task.assignment_acked"
    payload: TaskAssignmentAcked


class TaskStartedEvent(EventEnvelope):
    type: Literal["task.started"] = "task.started"
    payload: TaskStarted


class TaskResultSubmittedEvent(EventEnvelope):
    type: Literal["task.result_submitted"] = "task.result_submitted"
    payload: TaskResultSubmitted


class TaskReviewStartedEvent(EventEnvelope):
    type: Literal["task.review_started"] = "task.review_started"
    payload: TaskReviewStarted


class TaskAcceptedEvent(EventEnvelope):
    type: Literal["task.accepted"] = "task.accepted"
    payload: TaskAccepted


class TaskReworkRequestedEvent(EventEnvelope):
    type: Literal["task.rework_requested"] = "task.rework_requested"
    payload: TaskReworkRequested


class TaskFailedEvent(EventEnvelope):
    type: Literal["task.failed"] = "task.failed"
    payload: TaskFailed


class TaskCancelledEvent(EventEnvelope):
    type: Literal["task.cancelled"] = "task.cancelled"
    payload: TaskCancelled


class AgentCreatedEvent(EventEnvelope):
    type: Literal["agent.created"] = "agent.created"
    payload: AgentCreated


class AgentSandboxBootingEvent(EventEnvelope):
    type: Literal["agent.sandbox_booting"] = "agent.sandbox_booting"
    payload: AgentSandboxBooting


class AgentSandboxReadyEvent(EventEnvelope):
    type: Literal["agent.sandbox_ready"] = "agent.sandbox_ready"
    payload: AgentSandboxReady


class AgentStepEvent(EventEnvelope):
    type: Literal["agent.step"] = "agent.step"
    payload: AgentStep


class AgentStatusChangedEvent(EventEnvelope):
    type: Literal["agent.status_changed"] = "agent.status_changed"
    payload: AgentStatusChanged


class AgentStuckDetectedEvent(EventEnvelope):
    type: Literal["agent.stuck_detected"] = "agent.stuck_detected"
    payload: AgentStuckDetected


class AgentErrorEvent(EventEnvelope):
    type: Literal["agent.error"] = "agent.error"
    payload: AgentError


class AgentStoppedEvent(EventEnvelope):
    type: Literal["agent.stopped"] = "agent.stopped"
    payload: AgentStopped


class BoardEntryPostedEvent(EventEnvelope):
    type: Literal["board.entry_posted"] = "board.entry_posted"
    payload: BoardEntryPosted


class BoardEntryStatusChangedEvent(EventEnvelope):
    type: Literal["board.entry_status_changed"] = "board.entry_status_changed"
    payload: BoardEntryStatusChanged


class ArtifactPublishedEvent(EventEnvelope):
    type: Literal["artifact.published"] = "artifact.published"
    payload: ArtifactPublished


class ArtifactFetchedEvent(EventEnvelope):
    type: Literal["artifact.fetched"] = "artifact.fetched"
    payload: ArtifactFetched


class UserSteerEvent(EventEnvelope):
    type: Literal["user.steer"] = "user.steer"
    payload: UserSteer


class UserTakeoverStartedEvent(EventEnvelope):
    type: Literal["user.takeover_started"] = "user.takeover_started"
    payload: UserTakeoverStarted


class UserTakeoverEndedEvent(EventEnvelope):
    type: Literal["user.takeover_ended"] = "user.takeover_ended"
    payload: UserTakeoverEnded


class ApprovalRequestedEvent(EventEnvelope):
    type: Literal["approval.requested"] = "approval.requested"
    payload: ApprovalRequested


class ApprovalDecidedEvent(EventEnvelope):
    type: Literal["approval.decided"] = "approval.decided"
    payload: ApprovalDecided


class SupervisorTriggeredEvent(EventEnvelope):
    type: Literal["supervisor.triggered"] = "supervisor.triggered"
    payload: SupervisorTriggered


class SupervisorActionEvent(EventEnvelope):
    type: Literal["supervisor.action"] = "supervisor.action"
    payload: SupervisorAction


Event = Annotated[
    SessionCreatedEvent
    | SessionPlanningStartedEvent
    | SessionPlanProposedEvent
    | SessionPlanningFailedEvent
    | SessionPlanUpdatedEvent
    | SessionApprovedEvent
    | SessionPausedEvent
    | SessionResumedEvent
    | SessionCompletedEvent
    | SessionFailedEvent
    | SessionStoppedEvent
    | SessionBudgetWarningEvent
    | SessionBudgetExceededEvent
    | SessionModelUsageEvent
    | TaskCreatedEvent
    | TaskUpdatedEvent
    | TaskReadyEvent
    | TaskAssignedEvent
    | TaskAssignmentAckedEvent
    | TaskStartedEvent
    | TaskResultSubmittedEvent
    | TaskReviewStartedEvent
    | TaskAcceptedEvent
    | TaskReworkRequestedEvent
    | TaskFailedEvent
    | TaskCancelledEvent
    | AgentCreatedEvent
    | AgentSandboxBootingEvent
    | AgentSandboxReadyEvent
    | AgentStepEvent
    | AgentStatusChangedEvent
    | AgentStuckDetectedEvent
    | AgentErrorEvent
    | AgentStoppedEvent
    | BoardEntryPostedEvent
    | BoardEntryStatusChangedEvent
    | ArtifactPublishedEvent
    | ArtifactFetchedEvent
    | UserSteerEvent
    | UserTakeoverStartedEvent
    | UserTakeoverEndedEvent
    | ApprovalRequestedEvent
    | ApprovalDecidedEvent
    | SupervisorTriggeredEvent
    | SupervisorActionEvent,
    Field(discriminator="type"),
]
"""Any persisted event. Narrow on ``event.type`` (or ``isinstance``) to get the payload."""

EVENT_ADAPTER: TypeAdapter[Event] = TypeAdapter(Event)


def _collect() -> tuple[dict[str, type[EventEnvelope]], dict[str, type[EventPayload]]]:
    envelopes: dict[str, type[EventEnvelope]] = {}
    payloads: dict[str, type[EventPayload]] = {}
    for env_cls in EventEnvelope.__subclasses__():
        type_name = cast(str, env_cls.model_fields["type"].default)
        payload_cls = cast(type[EventPayload], env_cls.model_fields["payload"].annotation)
        if payload_cls.event_type != type_name:  # pragma: no cover - import-time guard
            raise RuntimeError(f"{env_cls.__name__} wraps {payload_cls.__name__} of wrong type")
        envelopes[type_name] = env_cls
        payloads[type_name] = payload_cls
    return envelopes, payloads


EVENT_TYPES, PAYLOAD_TYPES = _collect()
"""``EVENT_TYPES[type]`` is the envelope class; ``PAYLOAD_TYPES[type]`` its payload class."""


@dataclass(frozen=True, slots=True)
class EventDraft:
    """An event before the store assigned ``id``/``seq``/``ts``."""

    payload: EventPayload
    actor: Actor
    causation_id: str | None = None
    correlation_id: str | None = None

    @property
    def type(self) -> str:
        return self.payload.event_type


def build_event(
    draft: EventDraft,
    *,
    session_id: str,
    seq: int,
    event_id: str | None = None,
    ts: datetime | None = None,
) -> Event:
    """Turn a draft into a full, validated ``Event`` (used by event store implementations)."""
    if draft.type not in EVENT_TYPES:
        raise ValueError(f"unknown event type {draft.type!r}")
    data: dict[str, Any] = {
        "id": event_id or new_id(),
        "session_id": session_id,
        "seq": seq,
        "ts": ts or utcnow(),
        "actor": draft.actor.model_dump(),
        "causation_id": draft.causation_id,
        "correlation_id": draft.correlation_id,
        "type": draft.type,
        # dump + validate gives the event its own copy of the payload
        "payload": draft.payload.model_dump(),
    }
    return EVENT_ADAPTER.validate_python(data)


# --- ephemeral (bus-only) messages -----------------------------------------------------


class Heartbeat(Contract):
    """``agent.heartbeat``: sent every 10 s by a working agent; renews its task lease."""

    type: Literal["agent.heartbeat"] = "agent.heartbeat"
    session_id: str
    agent_id: str
    task_id: str | None = None
    step: int = Field(default=0, ge=0)
    ts: UtcDatetime


class Thumbnail(Contract):
    """Latest small screenshot of an agent for live tiles (JPEG, base64)."""

    type: Literal["thumbnail"] = "thumbnail"
    session_id: str
    agent_id: str
    step: int = Field(ge=0)
    jpeg_b64: str
    ts: UtcDatetime


EphemeralMessage = Annotated[Heartbeat | Thumbnail, Field(discriminator="type")]
EPHEMERAL_ADAPTER: TypeAdapter[EphemeralMessage] = TypeAdapter(EphemeralMessage)
