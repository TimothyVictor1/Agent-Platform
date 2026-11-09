"""Request/response DTOs for every endpoint in SPEC §16, plus WebSocket messages.

Endpoint → models:

* ``POST /sessions``: ``CreateSessionRequest`` → ``Session`` (201)
* ``GET /sessions``: query ``limit``/``cursor`` → ``SessionPage``
* ``GET /sessions/{id}``: ``SessionDetail``
* ``POST /sessions/{id}/refine``: ``RefinePlanRequest`` → ``CommandAccepted`` (202)
* ``PUT /sessions/{id}/plan``: ``PutPlanRequest`` → ``CommandAccepted`` (202) or
  problem+json 422 with ``errors`` = ``PlanError`` dumps
* ``POST /sessions/{id}/approve``: ``ApprovePlanRequest`` (optional body) → ``CommandAccepted``
* ``POST /sessions/{id}/followup``: ``FollowupRequest`` → ``CommandAccepted``
* ``POST /sessions/{id}/steer``: ``SteerRequest`` → ``CommandAccepted``
* ``POST /sessions/{id}/stop|pause|resume``: no body → ``CommandAccepted``
* ``POST /agents/{id}/takeover|release``: no body → ``CommandAccepted``
* ``GET /sessions/{id}/board``: query ``kinds``, ``tags``, ``q``, ``after_seq``, ``limit``
  → ``BoardPage``
* ``POST /sessions/{id}/board``: ``UserBoardPostRequest`` → ``CommandAccepted``
* ``GET /sessions/{id}/approvals``: query ``status`` → ``ApprovalList``
* ``POST /approvals/{id}/decide``: ``ApprovalDecisionRequest`` → ``CommandAccepted``
* ``GET /sessions/{id}/artifacts``: ``ArtifactList``; ``GET /artifacts/{id}/download``:
  bytes; ``POST /sessions/{id}/artifacts``: multipart ``file`` (+ ``description``) →
  ``Artifact`` (201)
* ``GET /sessions/{id}/events``: query ``after_seq``, ``limit`` → ``EventPage``
* ``GET /sessions/{id}/frames``: query ``agent_id``, ``from_step``, ``to_step`` →
  ``FrameList``; ``GET /frames/{id}``: JPEG bytes
* ``GET /config/profiles``: ``ProfileList``; ``GET /config/providers/health``:
  ``ProvidersHealth``
* ``GET /health``: ``HealthResponse``; ``GET /metrics``: ``MetricsResponse``
* ``POST /sessions/{id}/ws-ticket``: ``WsTicketRequest`` (optional body) → ``WsTicket``
  (201); single-use, valid ``WS_TICKET_TTL_S`` seconds
* ``WS /ws/sessions/{id}?ticket=<ticket>&since=<seq>``: server sends ``WsServerMessage``;
  closes with ``WS_CLOSE_TICKET_REJECTED`` (4401) on a missing/expired/used ticket
* errors: ``Problem`` (``application/problem+json``)
"""

from __future__ import annotations

from typing import Annotated, Final, Literal

from pydantic import Field, JsonValue

from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Approval, Artifact, Frame
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.common import (
    Contract,
    CoordinateMode,
    SandboxProviderName,
    SessionMode,
    SessionStatus,
    TaskStatus,
    UtcDatetime,
)
from agentplatform.contracts.events import Event
from agentplatform.contracts.plan import Plan
from agentplatform.contracts.session import Budgets, Session
from agentplatform.contracts.task import Task, TaskDraft

__all__ = [
    "WS_CLOSE_TICKET_REJECTED",
    "WS_TICKET_TTL_S",
    "ApprovalDecisionRequest",
    "ApprovalList",
    "ApprovePlanRequest",
    "ArtifactList",
    "BoardPage",
    "CommandAccepted",
    "CreateSessionRequest",
    "EventPage",
    "FollowupRequest",
    "FrameList",
    "HealthResponse",
    "MetricsResponse",
    "Problem",
    "ProblemError",
    "ProfileList",
    "ProfileSummary",
    "ProviderHealth",
    "ProvidersHealth",
    "PutPlanRequest",
    "RefinePlanRequest",
    "RoleSummary",
    "SessionDetail",
    "SessionPage",
    "SessionSummary",
    "SteerRequest",
    "TaskCounts",
    "UserBoardPostRequest",
    "WsEventMessage",
    "WsServerMessage",
    "WsThumbnailMessage",
    "WsTicket",
    "WsTicketRequest",
]


# --- sessions --------------------------------------------------------------------------


class CreateSessionRequest(Contract):
    prompt: str = Field(min_length=1, max_length=8000)
    agent_count: int = Field(default=3, ge=1, le=6)
    model_profile: str = Field(min_length=1)
    sandbox_provider: SandboxProviderName = "docker_local"
    mode: SessionMode = "coordinated"
    budgets: Budgets | None = None
    title: str | None = Field(default=None, max_length=200)


class SessionSummary(Contract):
    id: str
    title: str
    status: SessionStatus
    agent_count: int
    mode: SessionMode
    created_at: UtcDatetime
    ended_at: UtcDatetime | None = None


class SessionPage(Contract):
    items: list[SessionSummary]
    next_cursor: str | None = None


class TaskCounts(Contract):
    """Number of tasks per status; statuses with zero tasks may be omitted."""

    by_status: dict[TaskStatus, int] = Field(default_factory=dict)
    total: int = 0


class SessionDetail(Contract):
    session: Session
    plan: Plan | None = None
    """Current plan version (latest proposal, or the approved/executing plan)."""
    tasks: list[Task] = Field(default_factory=list)
    agents: list[Agent] = Field(default_factory=list)
    counts: TaskCounts = Field(default_factory=TaskCounts)
    last_seq: int = 0
    """Seq of the latest persisted event; open the WebSocket with ``since=last_seq``."""


class RefinePlanRequest(Contract):
    instruction: str = Field(min_length=1, max_length=4000)


class PutPlanRequest(Contract):
    tasks: list[TaskDraft]
    base_version: int | None = Field(default=None, ge=1)
    """Plan version the edit started from; 409 if the plan moved on meanwhile."""


class ApprovePlanRequest(Contract):
    plan_version: int | None = Field(default=None, ge=1)
    """Version being approved; 409 if it is not the current one. Defaults to current."""


class FollowupRequest(Contract):
    prompt: str = Field(min_length=1, max_length=8000)


class SteerRequest(Contract):
    agent_id: str | None = None
    text: str = Field(min_length=1, max_length=1500)


class CommandAccepted(Contract):
    command_id: str


# --- board / approvals / artifacts / replay ---------------------------------------------


class BoardPage(Contract):
    items: list[BoardEntry]
    next_after_seq: int | None = None


class UserBoardPostRequest(Contract):
    kind: Literal["note", "decision", "answer"]
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=1500)
    tags: list[str] = Field(default_factory=list, max_length=10)
    to: list[str] | None = None
    refs: list[str] = Field(default_factory=list)
    """For ``answer``: exactly one question id."""


class ApprovalList(Contract):
    items: list[Approval]


class ApprovalDecisionRequest(Contract):
    decision: Literal["approve", "deny"]
    note: str | None = Field(default=None, max_length=1000)


class ArtifactList(Contract):
    items: list[Artifact]


class EventPage(Contract):
    items: list[Event]
    next_after_seq: int | None = None
    """``None`` when there are no more events right now."""


class FrameList(Contract):
    items: list[Frame]


# --- config / health -------------------------------------------------------------------


class RoleSummary(Contract):
    provider: Literal["openai_compatible", "anthropic"]
    model: str
    base_url: str | None = None
    vision: bool = False
    coordinate_mode: CoordinateMode | None = None


class ProfileSummary(Contract):
    name: str
    roles: dict[Literal["planner", "supervisor", "agent", "summarizer"], RoleSummary]


class ProfileList(Contract):
    items: list[ProfileSummary]
    default_profile: str | None = None


class ProviderHealth(Contract):
    name: str
    kind: Literal["model", "sandbox"]
    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    checked_at: UtcDatetime


class ProvidersHealth(Contract):
    items: list[ProviderHealth]


class HealthResponse(Contract):
    status: Literal["ok", "degraded"]
    version: str
    checks: dict[str, bool] = Field(default_factory=dict)


class MetricsResponse(Contract):
    counters: dict[str, float] = Field(default_factory=dict)


# --- errors ----------------------------------------------------------------------------


class ProblemError(Contract):
    """One structured error inside a problem response (field or plan error)."""

    message: str
    code: str | None = None
    loc: list[str | int] | None = None
    task_key: str | None = None
    details: dict[str, JsonValue] | None = None


class Problem(Contract):
    """RFC 7807 problem+json body."""

    type: str = "about:blank"
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    errors: list[ProblemError] | None = None


# --- websocket -------------------------------------------------------------------------

WS_TICKET_TTL_S: Final = 60
"""Seconds a WebSocket ticket stays valid after it was issued (SPEC §16)."""

WS_CLOSE_TICKET_REJECTED: Final = 4401
"""WebSocket close code for a missing, expired or already-used ticket. The client
fetches a new ticket and reconnects; every other 4000-4999 close code is terminal."""


class WsTicketRequest(Contract):
    """Body of ``POST /sessions/{id}/ws-ticket``. Empty today; the body is optional."""


class WsTicket(Contract):
    """A single-use ticket for ``WS /ws/sessions/{id}?ticket=<ticket>``.

    Bound to the session it was issued for, valid for ``ttl_s`` seconds, consumed by
    the first WebSocket handshake that presents it. Lets the browser authenticate the
    WebSocket without putting the API token in a URL.
    """

    ticket: str = Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    """Opaque URL-safe random string; the server stores only its hash."""
    expires_at: UtcDatetime
    ttl_s: int = Field(default=WS_TICKET_TTL_S, ge=1)



class WsEventMessage(Contract):
    type: Literal["event"] = "event"
    event: Event


class WsThumbnailMessage(Contract):
    type: Literal["thumbnail"] = "thumbnail"
    agent_id: str
    step: int = 0
    jpeg_b64: str


WsServerMessage = Annotated[WsEventMessage | WsThumbnailMessage, Field(discriminator="type")]
