"""Session endpoints (SPEC §16 "Sessions" and the ws-ticket endpoint).

Reads come from the read models. Every state-changing endpoint except
``POST /sessions`` validates the current state and enqueues a command
(``contracts.commands``), answering ``202 {command_id}``; the runtime does the work.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from datetime import datetime
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Query, Response, status
from sqlalchemy import select, tuple_

from agentplatform.api.deps import Deps, Tx
from agentplatform.api.errors import ServiceUnavailableError
from agentplatform.api.problems import plan_validation_error
from agentplatform.api.routes._common import (
    ACTIVE,
    EDITABLE_PLAN,
    TERMINAL,
    current_plan_version,
    load_session,
    require_status,
)
from agentplatform.contracts.api import (
    ApprovePlanRequest,
    CommandAccepted,
    CreateSessionRequest,
    FollowupRequest,
    PutPlanRequest,
    RefinePlanRequest,
    SessionDetail,
    SessionPage,
    SessionSummary,
    SteerRequest,
    TaskCounts,
    WsTicket,
    WsTicketRequest,
)
from agentplatform.contracts.common import Actor, TaskStatus, new_id, utcnow
from agentplatform.contracts.events import EventDraft, SessionCreated
from agentplatform.contracts.plan import validate_plan
from agentplatform.contracts.session import Budgets, Session
from agentplatform.db.models import SessionRow
from agentplatform.db.repositories import AgentRepository, PlanRepository, TaskRepository
from agentplatform.errors import ConflictError, InvalidRequestError, NotFoundError

__all__ = ["LOCAL_USER_ID", "router"]

log = structlog.get_logger(__name__)

router = APIRouter(tags=["sessions"])

LOCAL_USER_ID = "local"
"""Auth v1 is a single local user (SPEC §16)."""

ACCEPTED: dict[int | str, dict[str, Any]] = {202: {"model": CommandAccepted}}


def _title_from_prompt(prompt: str) -> str:
    first = re.sub(r"\s+", " ", prompt.strip())
    return first if len(first) <= 80 else first[:79].rstrip() + "…"


# --- create / list / get ----------------------------------------------------------------


@router.post("/sessions", status_code=status.HTTP_201_CREATED, response_model=Session)
async def create_session(body: CreateSessionRequest, deps: Deps) -> Session:
    if body.model_profile not in deps.models.profiles:
        raise InvalidRequestError(
            f"unknown model profile {body.model_profile!r}",
            errors=[{"message": "unknown model profile", "loc": ["body", "model_profile"]}],
        )
    if body.sandbox_provider not in deps.sandbox_provider_names:
        raise InvalidRequestError(
            f"sandbox provider {body.sandbox_provider!r} is not configured",
            errors=[{"message": "provider not configured", "loc": ["body", "sandbox_provider"]}],
        )
    budgets = body.budgets or Budgets(max_cost_usd=deps.policies.budgets.default_max_cost_usd)
    session = Session(
        id=new_id(),
        user_id=LOCAL_USER_ID,
        title=body.title or _title_from_prompt(body.prompt),
        prompt=body.prompt,
        status="planning",
        agent_count=body.agent_count,
        model_profile=body.model_profile,
        sandbox_provider=body.sandbox_provider,
        mode=body.mode,
        budgets=budgets,
        created_at=utcnow(),
    )
    # No command caused session.created, so causation_id stays None.
    await deps.recorder.record(
        session.id, EventDraft(payload=SessionCreated(session=session), actor=Actor.user())
    )
    try:
        await deps.commands.issue("command.plan_session", session.id)
    except Exception as exc:
        # The session exists (status planning) but no runtime was told to plan it.
        log.error(
            "plan_command_enqueue_failed", session_id=session.id, error_type=type(exc).__name__
        )
        raise ServiceUnavailableError(
            "The session was created but planning could not be started; try again later."
        ) from None
    return session


def _encode_cursor(created_at: datetime, session_id: str) -> str:
    raw = json.dumps({"c": created_at.isoformat(), "i": session_id}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode()))
        created_at = datetime.fromisoformat(data["c"])
        session_id = data["i"]
        if not isinstance(session_id, str) or created_at.tzinfo is None:
            raise ValueError("bad cursor")
    except (ValueError, KeyError, TypeError, binascii.Error, json.JSONDecodeError):
        raise InvalidRequestError(
            "invalid cursor", errors=[{"message": "invalid cursor", "loc": ["query", "cursor"]}]
        ) from None
    return created_at, session_id


@router.get("/sessions", response_model=SessionPage)
async def list_sessions(
    tx: Tx,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> SessionPage:
    """Newest first, keyset-paginated on ``(created_at, id)``."""
    stmt = select(SessionRow).where(SessionRow.user_id == LOCAL_USER_ID)
    if cursor:
        created_at, session_id = _decode_cursor(cursor)
        stmt = stmt.where(
            tuple_(SessionRow.created_at, SessionRow.id) < tuple_(created_at, session_id)
        )
    stmt = stmt.order_by(SessionRow.created_at.desc(), SessionRow.id.desc()).limit(limit + 1)
    rows = list((await tx.scalars(stmt)).all())
    items = [
        SessionSummary.model_validate(
            {
                "id": r.id,
                "title": r.title,
                "status": r.status,
                "agent_count": r.agent_count,
                "mode": r.mode,
                "created_at": r.created_at,
                "ended_at": r.ended_at,
            }
        )
        for r in rows[:limit]
    ]
    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = _encode_cursor(last.created_at, last.id)
    return SessionPage(items=items, next_cursor=next_cursor)


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get_session(session_id: str, tx: Tx, deps: Deps) -> SessionDetail:
    session = await load_session(tx, session_id)
    plan = await PlanRepository(tx).find(session_id)
    tasks = await TaskRepository(tx).list(session_id)
    agents = await AgentRepository(tx).list(session_id)
    by_status: dict[TaskStatus, int] = {}
    for task in tasks:
        by_status[task.status] = by_status.get(task.status, 0) + 1
    return SessionDetail(
        session=session,
        plan=plan,
        tasks=tasks,
        agents=agents,
        counts=TaskCounts(by_status=by_status, total=len(tasks)),
        last_seq=await deps.events.last_seq(session_id),
    )


# --- planning commands -------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/refine",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def refine_plan(
    session_id: str, body: RefinePlanRequest, tx: Tx, deps: Deps
) -> CommandAccepted:
    session = await load_session(tx, session_id)
    require_status(session, EDITABLE_PLAN, "refine the plan")
    if await current_plan_version(tx, session) < 1:
        raise ConflictError("there is no plan to refine yet")
    return await deps.commands.issue(
        "command.refine_plan", session_id, instruction=body.instruction
    )


@router.put(
    "/sessions/{session_id}/plan",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def put_plan(session_id: str, body: PutPlanRequest, tx: Tx, deps: Deps) -> CommandAccepted:
    """Validate a user-edited plan and ask the runtime to store it as a new version.

    The runtime emits ``session.plan_updated`` with ``causation_id`` = the returned
    ``command_id``."""
    session = await load_session(tx, session_id)
    require_status(session, EDITABLE_PLAN, "edit the plan")
    current = await current_plan_version(tx, session)
    if current < 1:
        raise ConflictError("no plan to edit yet")
    if body.base_version is not None and body.base_version != current:
        raise ConflictError(
            f"the plan changed meanwhile (current version {current}, "
            f"edit based on {body.base_version})"
        )
    errors = validate_plan(body.tasks)
    if errors:
        raise plan_validation_error(errors)
    return await deps.commands.issue(
        "command.update_plan", session_id, tasks=body.tasks, base_version=current
    )


@router.post(
    "/sessions/{session_id}/approve",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def approve_plan(
    session_id: str,
    tx: Tx,
    deps: Deps,
    body: Annotated[ApprovePlanRequest | None, Body()] = None,
) -> CommandAccepted:
    session = await load_session(tx, session_id)
    require_status(session, {"awaiting_approval"}, "approve the plan")
    current = await current_plan_version(tx, session)
    if current < 1:
        raise ConflictError("there is no plan to approve yet")
    requested = body.plan_version if body is not None else None
    if requested is not None and requested != current:
        raise ConflictError(f"plan version {requested} is not the current version {current}")
    return await deps.commands.issue("command.approve_plan", session_id, plan_version=current)


@router.post(
    "/sessions/{session_id}/followup",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def followup(session_id: str, body: FollowupRequest, tx: Tx, deps: Deps) -> CommandAccepted:
    session = await load_session(tx, session_id)
    require_status(session, {"running", "paused", "completed"}, "add a follow-up")
    return await deps.commands.issue("command.followup", session_id, prompt=body.prompt)


# --- control commands --------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/steer",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def steer(session_id: str, body: SteerRequest, tx: Tx, deps: Deps) -> CommandAccepted:
    session = await load_session(tx, session_id)
    require_status(session, ACTIVE, "steer")
    if body.agent_id is not None:
        agent = await AgentRepository(tx).find(body.agent_id)
        if agent is None or agent.session_id != session_id:
            raise NotFoundError(f"agent {body.agent_id} is not part of session {session_id}")
    return await deps.commands.issue(
        "command.steer", session_id, agent_id=body.agent_id, text=body.text
    )


@router.post(
    "/sessions/{session_id}/stop",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def stop(session_id: str, tx: Tx, deps: Deps) -> CommandAccepted:
    session = await load_session(tx, session_id)
    if session.status in TERMINAL:
        raise ConflictError(f"the session is already {session.status}")
    return await deps.commands.issue("command.stop", session_id)


@router.post(
    "/sessions/{session_id}/pause",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def pause(session_id: str, tx: Tx, deps: Deps) -> CommandAccepted:
    session = await load_session(tx, session_id)
    require_status(session, {"running"}, "pause")
    return await deps.commands.issue("command.pause", session_id)


@router.post(
    "/sessions/{session_id}/resume",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def resume(session_id: str, tx: Tx, deps: Deps) -> CommandAccepted:
    session = await load_session(tx, session_id)
    require_status(session, {"paused"}, "resume")
    return await deps.commands.issue("command.resume", session_id)


# --- websocket ticket --------------------------------------------------------------------


@router.post(
    "/sessions/{session_id}/ws-ticket",
    status_code=status.HTTP_201_CREATED,
    response_model=WsTicket,
)
async def create_ws_ticket(
    session_id: str,
    response: Response,
    deps: Deps,
    body: Annotated[WsTicketRequest | None, Body()] = None,
) -> WsTicket:
    """A single-use, 60 s ticket for ``/ws/sessions/{session_id}?ticket=...``.

    Works whether or not ``APP_API_TOKEN`` is set, so clients use one code path."""
    if not await deps.session_exists(session_id):
        raise NotFoundError(f"session {session_id} does not exist")
    response.headers["Cache-Control"] = "no-store"
    ticket = await deps.tickets.issue(session_id)
    deps.metrics.inc("ws.tickets_issued")
    return ticket
