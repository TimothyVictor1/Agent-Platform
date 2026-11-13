"""Read models and session ownership for the runtime (T13; SPEC §5.1, §17).

**Projection.** Every event the runtime appends goes through ``ProjectingEventStore``,
which runs a projector inside the append transaction (``ReadModelUpdate``), before
the caller's own update. ``project_read_models`` keeps the ``sessions``, ``plans``,
``tasks`` and ``agents`` tables in step with the log; the API's state checks
(approve, refine, steer, takeover, ...) read them, and the frame metadata of each
``agent.step`` goes to ``frames`` (replay). Board entries, artifacts and approvals
are written by their own services and are not touched here.

Session status rules (``sessions.status``):

* ``session.plan_proposed`` and a user's ``session.plan_updated`` → ``awaiting_approval``
  (also for a follow-up proposal while the session runs, because approving it needs
  that status); a supervisor's ``plan_updated`` changes nothing (the supervisor
  approves its own change right after);
* ``session.approved`` / ``session.resumed`` → ``running``; ``session.paused`` → ``paused``;
* ``session.completed`` / ``failed`` / ``stopped`` → that status with ``ended_at``.

Task and agent rows mirror the scheduler's fold (``SchedulerState.apply``). A stale
``task.failed`` (from an attempt that is no longer current) is ignored.

**Ownership.** ``SessionDirectory`` answers which sessions a runtime should adopt and
which are stuck in planning, and implements the session lease ("two runtimes never
run the same session"): ``PostgresSessionDirectory`` uses the ``runtime_owner`` /
``runtime_lease_expires_at`` columns with a conditional ``UPDATE``;
``InMemorySessionDirectory`` folds the in-memory event log (tests).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

import structlog
from sqlalchemy import and_, exists, or_, select, update

from agentplatform.contracts.common import SessionStatus, utcnow
from agentplatform.contracts.events import (
    AgentCreated,
    AgentSandboxReady,
    AgentStatusChanged,
    AgentStep,
    AgentStopped,
    Event,
    EventDraft,
    SessionApproved,
    SessionCompleted,
    SessionCreated,
    SessionFailed,
    SessionModelUsage,
    SessionPaused,
    SessionPlanningFailed,
    SessionPlanProposed,
    SessionPlanUpdated,
    SessionResumed,
    SessionStopped,
    TaskAccepted,
    TaskAssigned,
    TaskCancelled,
    TaskCreated,
    TaskFailed,
    TaskReady,
    TaskResultSubmitted,
    TaskReviewStarted,
    TaskReworkRequested,
    TaskStarted,
    TaskUpdated,
)
from agentplatform.contracts.plan import Plan
from agentplatform.contracts.session import Session
from agentplatform.contracts.task import Task
from agentplatform.db.event_store import DraftFactory, EventStore, ReadModelUpdate
from agentplatform.db.models import EventRow, SessionRow
from agentplatform.db.repositories import Repositories, SessionRepository
from agentplatform.db.session import SessionMaker, as_session, transaction

__all__ = [
    "ADOPTABLE_STATUSES",
    "PLANNED_EVENT_TYPES",
    "InMemorySessionDirectory",
    "PostgresSessionDirectory",
    "ProjectingEventStore",
    "Projector",
    "SessionDirectory",
    "project_read_models",
]

log = structlog.get_logger(__name__)

Projector = Callable[[Event, object], Awaitable[None]]
ADOPTABLE_STATUSES: frozenset[SessionStatus] = frozenset(
    {"planning", "awaiting_approval", "running", "paused"}
)
TERMINAL_STATUSES: frozenset[SessionStatus] = frozenset({"failed", "stopped"})
PLANNED_EVENT_TYPES = ("session.plan_proposed", "session.planning_failed")
"""A session with one of these events has been through the planner at least once."""
_LIVE_TASK = frozenset({"assigned", "running"})


# -- projecting store ---------------------------------------------------------------------


class ProjectingEventStore:
    """``EventStore`` that applies ``projector`` in every append's transaction, then
    the caller's ``read_model_update``. Reads go straight to ``inner``."""

    def __init__(self, inner: EventStore, projector: Projector) -> None:
        self.inner = inner
        self._projector = projector

    async def append(
        self,
        session_id: str,
        draft: EventDraft | DraftFactory,
        read_model_update: ReadModelUpdate | None = None,
    ) -> Event:
        projector = self._projector

        async def update(event: Event, tx: object) -> None:
            await projector(event, tx)
            if read_model_update is not None:
                await read_model_update(event, tx)

        return await self.inner.append(session_id, draft, update)

    async def read(self, session_id: str, after_seq: int = 0, limit: int = 500) -> list[Event]:
        return await self.inner.read(session_id, after_seq, limit)

    async def last_seq(self, session_id: str) -> int:
        return await self.inner.last_seq(session_id)


# -- Postgres projector -------------------------------------------------------------------


async def project_read_models(event: Event, tx: object) -> None:
    """Apply ``event`` to the read-model tables (Postgres ``tx``; see module docstring)."""
    repos = Repositories(as_session(tx))
    p = event.payload
    if isinstance(p, SessionCreated | SessionPlanningFailed):
        return  # the store inserts the session; planning failures change no row
    if isinstance(
        p,
        SessionPlanProposed
        | SessionPlanUpdated
        | SessionApproved
        | SessionPaused
        | SessionResumed
        | SessionCompleted
        | SessionFailed
        | SessionStopped,
    ):
        await _project_session(repos, event)
    elif isinstance(p, SessionModelUsage):
        await repos.sessions.add_usage(event.session_id, p.usage)
    elif isinstance(p, AgentStep):
        await repos.sessions.add_usage(event.session_id, p.usage)
        if p.frame is not None and await repos.frames.find(p.frame.id) is None:
            await repos.frames.insert(p.frame)
        agent = await repos.agents.find(p.agent_id)
        if agent is not None:
            await repos.agents.upsert(
                agent.model_copy(
                    update={
                        "step": p.step,
                        "current_task_id": p.task_id,
                        "last_heartbeat_at": event.ts,
                    }
                )
            )
    elif isinstance(p, AgentCreated | AgentSandboxReady | AgentStatusChanged | AgentStopped):
        await _project_agent(repos, event)
    else:
        await _project_task(repos, event)


async def _project_session(repos: Repositories, event: Event) -> None:
    p = event.payload
    sid = event.session_id
    session = await repos.sessions.find(sid)
    if session is None:
        log.warning("readmodel_session_missing", session_id=sid, event_type=event.type)
        return
    status: SessionStatus = session.status
    changes: dict[str, object] = {}
    if isinstance(p, SessionPlanProposed | SessionPlanUpdated):
        source = "planner" if isinstance(p, SessionPlanProposed) else p.source
        plan = Plan(
            session_id=sid, version=p.plan_version, proposal=p.proposal, created_at=event.ts
        )
        if await repos.plans.find(sid, p.plan_version) is None:
            await repos.plans.insert(plan, source=source)
        changes["plan_version"] = max(session.plan_version, p.plan_version)
        if source != "supervisor" and status not in TERMINAL_STATUSES:
            status = "awaiting_approval"
    elif isinstance(p, SessionApproved):
        if await repos.plans.find(sid, p.plan_version) is not None:
            await repos.plans.approve(sid, p.plan_version, event.ts)
        changes["plan_version"] = max(session.plan_version, p.plan_version)
        if status not in TERMINAL_STATUSES:
            status = "running"
        if session.started_at is None:
            changes["started_at"] = event.ts
    elif isinstance(p, SessionPaused):
        status = "paused"
    elif isinstance(p, SessionResumed):
        status = "running"
    elif isinstance(p, SessionCompleted):
        status, changes["ended_at"] = "completed", event.ts
    elif isinstance(p, SessionFailed):
        status, changes["ended_at"] = "failed", event.ts
    elif isinstance(p, SessionStopped):
        status, changes["ended_at"] = "stopped", event.ts
    if status != "completed" and isinstance(p, SessionApproved):
        changes["ended_at"] = None  # a follow-up reopened a completed session
    await repos.sessions.save(session.model_copy(update={"status": status, **changes}))


async def _project_agent(repos: Repositories, event: Event) -> None:
    p = event.payload
    if isinstance(p, AgentCreated):
        await repos.agents.upsert(p.agent)
        return
    assert isinstance(p, AgentSandboxReady | AgentStatusChanged | AgentStopped)
    agent = await repos.agents.find(p.agent_id)
    if agent is None:
        log.warning("readmodel_agent_missing", agent_id=p.agent_id, event_type=event.type)
        return
    if isinstance(p, AgentSandboxReady):
        agent = agent.model_copy(
            update={
                "sandbox": p.sandbox,
                "stream_url": p.stream_url,
                "interactive_stream_url": p.interactive_stream_url,
            }
        )
    elif isinstance(p, AgentStatusChanged):
        agent = agent.model_copy(update={"status": p.status})
    else:
        agent = agent.model_copy(update={"status": "stopped", "current_task_id": None})
    await repos.agents.upsert(agent)


async def _project_task(repos: Repositories, event: Event) -> None:
    p = event.payload
    if isinstance(p, TaskCreated | TaskUpdated):
        await repos.tasks.upsert(p.task)
        return
    if not isinstance(
        p,
        TaskReady
        | TaskAssigned
        | TaskStarted
        | TaskResultSubmitted
        | TaskReviewStarted
        | TaskAccepted
        | TaskReworkRequested
        | TaskFailed
        | TaskCancelled,
    ):
        return  # board, artifacts, approvals, user, supervisor events: no task row change
    task = await repos.tasks.find(p.task_id)
    if task is None:
        log.warning("readmodel_task_missing", task_id=p.task_id, event_type=event.type)
        return
    updated = _task_after(task, event)
    if updated is not None:
        await repos.tasks.upsert(updated)
    if isinstance(p, TaskStarted):
        await _set_current_task(repos, p.agent_id, p.task_id)
    elif isinstance(p, TaskResultSubmitted | TaskFailed) and p.agent_id is not None:
        await _set_current_task(repos, p.agent_id, None, only_if=p.task_id)


def _task_after(task: Task, event: Event) -> Task | None:
    p = event.payload
    if isinstance(p, TaskReady):
        return task.model_copy(update={"status": "ready", "assigned_agent_id": None})
    if isinstance(p, TaskAssigned):
        return task.model_copy(update={"status": "assigned", "assigned_agent_id": p.agent_id})
    if isinstance(p, TaskStarted):
        if task.status in _LIVE_TASK and task.assigned_agent_id not in (None, p.agent_id):
            return None
        return task.model_copy(
            update={"status": "running", "attempt": p.attempt, "assigned_agent_id": p.agent_id}
        )
    if isinstance(p, TaskResultSubmitted):
        if task.status in _LIVE_TASK and task.assigned_agent_id not in (None, p.agent_id):
            return None
        return task.model_copy(update={"status": "in_review", "result": p.result})
    if isinstance(p, TaskReviewStarted):
        return task.model_copy(update={"status": "in_review"})
    if isinstance(p, TaskAccepted):
        return task.model_copy(update={"status": "done"})
    if isinstance(p, TaskReworkRequested):
        return task.model_copy(
            update={"status": "pending", "assigned_agent_id": None, "rework_count": p.rework_count}
        )
    if isinstance(p, TaskFailed):
        live = task.status in _LIVE_TASK
        stale = live and (
            (p.agent_id is not None and task.assigned_agent_id not in (None, p.agent_id))
            or (task.status == "running" and p.attempt < task.attempt)
        )
        if stale:
            return None
        return task.model_copy(
            update={
                "status": "pending" if p.will_retry else "failed",
                "assigned_agent_id": None,
                "attempt": max(task.attempt, p.attempt),
            }
        )
    if isinstance(p, TaskCancelled):
        return task.model_copy(update={"status": "cancelled", "assigned_agent_id": None})
    return None


async def _set_current_task(
    repos: Repositories, agent_id: str, task_id: str | None, *, only_if: str | None = None
) -> None:
    agent = await repos.agents.find(agent_id)
    if agent is None:
        return
    if only_if is not None and agent.current_task_id != only_if:
        return
    await repos.agents.upsert(agent.model_copy(update={"current_task_id": task_id}))


# -- session directory --------------------------------------------------------------------


class SessionDirectory(Protocol):
    """Session lookup and the session lease (see the module docstring)."""

    async def get(self, session_id: str) -> Session | None: ...

    async def adoptable(self, owner: str, *, completed_since: datetime) -> list[Session]:
        """Sessions this runtime should run: adoptable statuses, or completed since
        ``completed_since``; unowned, lease expired, or already owned by ``owner``."""
        ...

    async def stuck_planning(self, *, created_before: datetime) -> list[Session]:
        """Sessions in ``planning`` created before the cutoff that never reached the
        planner and whose lease is free."""
        ...

    async def alive_ids(self, *, completed_since: datetime) -> set[str]:
        """Sessions whose sandboxes must survive an orphan cleanup."""
        ...

    async def claim(self, session_id: str, owner: str, ttl_s: float) -> bool:
        """Take (or renew) the lease if it is free, expired or already ``owner``'s."""
        ...

    async def renew(self, session_id: str, owner: str, ttl_s: float) -> bool:
        """Extend the lease only if ``owner`` still holds it."""
        ...

    async def release(self, session_id: str, owner: str) -> None: ...


class PostgresSessionDirectory:
    """``SessionDirectory`` over the ``sessions`` table (lease columns
    ``runtime_owner``/``runtime_lease_expires_at``, compared with ``now()`` of this
    process; runtimes are expected to have roughly synchronised clocks)."""

    def __init__(self, sessionmaker: SessionMaker, *, now: Callable[[], datetime] = utcnow):
        self._sm = sessionmaker
        self._now = now

    def _free(self, owner: str | None = None) -> object:
        now = self._now()
        cond = or_(
            SessionRow.runtime_owner.is_(None),
            SessionRow.runtime_lease_expires_at.is_(None),
            SessionRow.runtime_lease_expires_at < now,
        )
        return cond if owner is None else or_(cond, SessionRow.runtime_owner == owner)

    async def get(self, session_id: str) -> Session | None:
        async with transaction(self._sm) as tx:
            return await SessionRepository(tx).find(session_id)

    async def _ids(self, stmt: object) -> list[str]:
        async with transaction(self._sm) as tx:
            return [str(i) for i in (await tx.scalars(stmt)).all()]  # type: ignore[call-overload]

    async def _sessions(self, ids: Collection[str]) -> list[Session]:
        out: list[Session] = []
        async with transaction(self._sm) as tx:
            repo = SessionRepository(tx)
            for sid in ids:
                found = await repo.find(sid)
                if found is not None:
                    out.append(found)
        return out

    async def adoptable(self, owner: str, *, completed_since: datetime) -> list[Session]:
        stmt = (
            select(SessionRow.id)
            .where(
                or_(
                    SessionRow.status.in_(sorted(ADOPTABLE_STATUSES)),
                    and_(SessionRow.status == "completed", SessionRow.ended_at >= completed_since),
                ),
                self._free(owner),  # type: ignore[arg-type]
            )
            .order_by(SessionRow.created_at)
        )
        return await self._sessions(await self._ids(stmt))

    async def stuck_planning(self, *, created_before: datetime) -> list[Session]:
        planned = exists().where(
            EventRow.session_id == SessionRow.id, EventRow.type.in_(PLANNED_EVENT_TYPES)
        )
        stmt = (
            select(SessionRow.id)
            .where(
                SessionRow.status == "planning",
                SessionRow.created_at < created_before,
                ~planned,
                self._free(),  # type: ignore[arg-type]
            )
            .order_by(SessionRow.created_at)
        )
        return await self._sessions(await self._ids(stmt))

    async def alive_ids(self, *, completed_since: datetime) -> set[str]:
        stmt = select(SessionRow.id).where(
            or_(
                SessionRow.status.in_(sorted(ADOPTABLE_STATUSES)),
                and_(SessionRow.status == "completed", SessionRow.ended_at >= completed_since),
            )
        )
        return set(await self._ids(stmt))

    async def _set_lease(self, session_id: str, owner: str, ttl_s: float, cond: object) -> bool:
        expires = self._now() + timedelta(seconds=ttl_s)
        async with transaction(self._sm) as tx:
            result = await tx.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id, cond)  # type: ignore[arg-type]
                .values(runtime_owner=owner, runtime_lease_expires_at=expires)
            )
            return bool(result.rowcount)  # type: ignore[attr-defined]

    async def claim(self, session_id: str, owner: str, ttl_s: float) -> bool:
        return await self._set_lease(session_id, owner, ttl_s, self._free(owner))

    async def renew(self, session_id: str, owner: str, ttl_s: float) -> bool:
        cond = and_(
            SessionRow.runtime_owner == owner, SessionRow.runtime_lease_expires_at >= self._now()
        )
        return await self._set_lease(session_id, owner, ttl_s, cond)

    async def release(self, session_id: str, owner: str) -> None:
        async with transaction(self._sm) as tx:
            await tx.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id, SessionRow.runtime_owner == owner)
                .values(runtime_owner=None, runtime_lease_expires_at=None)
            )


@dataclass(slots=True)
class _Known:
    session: Session
    planned: bool = False
    owner: str | None = None
    expires: float = 0.0


class InMemorySessionDirectory:
    """``SessionDirectory`` for tests: pass ``project`` as the projector of a
    ``ProjectingEventStore`` over the in-memory store. Leases use ``clock``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.known: dict[str, _Known] = {}

    async def project(self, event: Event, tx: object) -> None:
        p = event.payload
        if isinstance(p, SessionCreated):
            self.known.setdefault(event.session_id, _Known(p.session))
            return
        k = self.known.get(event.session_id)
        if k is None:
            return
        s = k.session
        if event.type in PLANNED_EVENT_TYPES:
            k.planned = True
        status: SessionStatus = s.status
        if isinstance(p, SessionPlanProposed) or (
            isinstance(p, SessionPlanUpdated) and p.source == "user"
        ):
            status = "awaiting_approval" if status not in TERMINAL_STATUSES else status
        elif isinstance(p, SessionApproved | SessionResumed):
            status = "running" if status not in TERMINAL_STATUSES else status
        elif isinstance(p, SessionPaused):
            status = "paused"
        elif isinstance(p, SessionCompleted):
            status = "completed"
        elif isinstance(p, SessionFailed):
            status = "failed"
        elif isinstance(p, SessionStopped):
            status = "stopped"
        else:
            return
        ended = event.ts if status in ("completed", "failed", "stopped") else None
        k.session = s.model_copy(update={"status": status, "ended_at": ended})

    def _free(self, k: _Known, owner: str | None = None) -> bool:
        return k.owner is None or k.expires < self._clock() or k.owner == owner

    async def get(self, session_id: str) -> Session | None:
        k = self.known.get(session_id)
        return None if k is None else k.session

    async def adoptable(self, owner: str, *, completed_since: datetime) -> list[Session]:
        return [
            k.session
            for k in self.known.values()
            if self._free(k, owner)
            and (
                k.session.status in ADOPTABLE_STATUSES
                or (
                    k.session.status == "completed"
                    and k.session.ended_at is not None
                    and k.session.ended_at >= completed_since
                )
            )
        ]

    async def stuck_planning(self, *, created_before: datetime) -> list[Session]:
        return [
            k.session
            for k in self.known.values()
            if k.session.status == "planning"
            and k.session.created_at < created_before
            and not k.planned
            and self._free(k)
        ]

    async def alive_ids(self, *, completed_since: datetime) -> set[str]:
        return {
            sid
            for sid, k in self.known.items()
            if k.session.status in ADOPTABLE_STATUSES
            or (
                k.session.status == "completed"
                and k.session.ended_at is not None
                and k.session.ended_at >= completed_since
            )
        }

    async def claim(self, session_id: str, owner: str, ttl_s: float) -> bool:
        k = self.known.get(session_id)
        if k is None or not self._free(k, owner):
            return False
        k.owner, k.expires = owner, self._clock() + ttl_s
        return True

    async def renew(self, session_id: str, owner: str, ttl_s: float) -> bool:
        k = self.known.get(session_id)
        if k is None or k.owner != owner or k.expires < self._clock():
            return False
        k.expires = self._clock() + ttl_s
        return True

    async def release(self, session_id: str, owner: str) -> None:
        k = self.known.get(session_id)
        if k is not None and k.owner == owner:
            k.owner = None

    def expire(self, session_id: str) -> None:
        """Test helper: the owner crashed and its lease ran out."""
        k = self.known.get(session_id)
        if k is not None:
            k.expires = float("-inf")
