"""Repositories over the read-model tables (SPEC §6, §17).

Every repository wraps one ``AsyncSession`` (a unit of work) and returns Pydantic
contract models, never ORM rows. They do not commit: the caller owns the
transaction, either ``db.session.transaction(sm)`` or the ``tx`` handed to a
``ReadModelUpdate`` by ``PostgresEventStore.append``, so read models change
atomically with the event that caused them::

    async def apply(event: Event, tx: object) -> None:
        await BoardRepository(as_session(tx)).insert(entry)

Conventions:

* ``get`` raises ``NotFoundError``; ``find`` returns ``None``.
* Inserting a duplicate key raises ``ConflictError`` (the surrounding transaction
  stays usable because inserts run in a savepoint).
* Reads use ``populate_existing`` so rows changed by bulk ``UPDATE`` statements earlier
  in the same transaction are never served stale from the identity map.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import ColumnElement, Select, case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Approval, Artifact, Frame
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.common import (
    Actor,
    AgentStatus,
    ApprovalStatus,
    BoardEntryKind,
    BoardEntryStatus,
    Contract,
    SessionStatus,
    TaskStatus,
)
from agentplatform.contracts.plan import Plan, PlanProposal
from agentplatform.contracts.session import Session, Usage
from agentplatform.contracts.task import Task
from agentplatform.db.models import (
    AgentRow,
    ApprovalRow,
    ArtifactRow,
    Base,
    BoardEntryRow,
    FrameRow,
    PlanRow,
    SessionRow,
    TaskRow,
    UserRow,
)
from agentplatform.errors import ConflictError, NotFoundError

__all__ = [
    "AgentRepository",
    "ApprovalRepository",
    "ArtifactRepository",
    "BoardRepository",
    "ClaimMatch",
    "FrameRepository",
    "PlanRepository",
    "Repositories",
    "SessionRepository",
    "TaskRepository",
]

RowT = TypeVar("RowT", bound=Base)

TERMINAL_SESSION_STATUSES: frozenset[SessionStatus] = frozenset({"completed", "failed", "stopped"})


class _Repository:
    def __init__(self, tx: AsyncSession) -> None:
        self.tx = tx

    async def _all(self, stmt: Select[RowT]) -> Sequence[RowT]:
        return (await self.tx.scalars(stmt.execution_options(populate_existing=True))).all()

    async def _one_or_none(self, stmt: Select[RowT]) -> RowT | None:
        result = await self.tx.scalars(stmt.execution_options(populate_existing=True))
        return result.one_or_none()

    async def _add(self, row: Base, what: str) -> None:
        try:
            async with self.tx.begin_nested():
                self.tx.add(row)
        except IntegrityError:
            raise ConflictError(f"{what} already exists") from None


def _row_values(model: Contract) -> dict[str, Any]:
    """Column values for a contract: nested objects as JSON, top-level datetimes kept
    as ``datetime`` (``UtcDatetime`` serializes to text even in python mode)."""
    data = model.model_dump(mode="json")
    for name in type(model).model_fields:
        value = getattr(model, name)
        if isinstance(value, datetime):
            data[name] = value
    return data


# --- sessions ---------------------------------------------------------------------------


def _session_from_row(row: SessionRow) -> Session:
    return Session.model_validate(
        {
            "id": row.id,
            "user_id": row.user_id,
            "title": row.title,
            "prompt": row.prompt,
            "status": row.status,
            "agent_count": row.agent_count,
            "model_profile": row.model_profile,
            "sandbox_provider": row.sandbox_provider,
            "mode": row.mode,
            "plan_version": row.plan_version,
            "budgets": row.budgets,
            "usage": row.usage,
            "created_at": row.created_at,
            "started_at": row.started_at,
            "ended_at": row.ended_at,
        }
    )


class SessionRepository(_Repository):
    async def insert(self, session: Session, *, if_missing: bool = False) -> None:
        """Insert a session (and a ``users`` row for its ``user_id`` if missing).

        With ``if_missing=True`` an existing row is left untouched instead of raising
        ``ConflictError``.
        """
        await self.tx.execute(
            pg_insert(UserRow)
            .values(id=session.user_id, name=session.user_id)
            .on_conflict_do_nothing(index_elements=[UserRow.id])
        )
        stmt = pg_insert(SessionRow).values(**_row_values(session))
        if if_missing:
            await self.tx.execute(stmt.on_conflict_do_nothing(index_elements=[SessionRow.id]))
            return
        try:
            async with self.tx.begin_nested():
                await self.tx.execute(stmt)
        except IntegrityError:
            raise ConflictError(f"session {session.id} already exists") from None

    async def find(self, session_id: str) -> Session | None:
        row = await self._one_or_none(select(SessionRow).where(SessionRow.id == session_id))
        return None if row is None else _session_from_row(row)

    async def get(self, session_id: str) -> Session:
        session = await self.find(session_id)
        if session is None:
            raise NotFoundError(f"session {session_id} does not exist")
        return session

    async def list(
        self,
        *,
        user_id: str | None = None,
        statuses: Sequence[SessionStatus] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> builtins.list[Session]:
        """Newest first."""
        stmt = select(SessionRow)
        if user_id is not None:
            stmt = stmt.where(SessionRow.user_id == user_id)
        if statuses is not None:
            stmt = stmt.where(SessionRow.status.in_(statuses))
        stmt = stmt.order_by(SessionRow.created_at.desc(), SessionRow.id.desc())
        stmt = stmt.limit(max(1, min(limit, 500))).offset(max(0, offset))
        return [_session_from_row(r) for r in await self._all(stmt)]

    async def _update(self, session_id: str, **values: Any) -> Session:
        result = await self.tx.execute(
            update(SessionRow).where(SessionRow.id == session_id).values(**values)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError(f"session {session_id} does not exist")
        return await self.get(session_id)

    async def save(self, session: Session) -> Session:
        """Overwrite every contract field of an existing session with ``session``."""
        values = _row_values(session)
        values.pop("id")
        return await self._update(session.id, **values)

    async def set_status(
        self,
        session_id: str,
        status: SessionStatus,
        *,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
    ) -> Session:
        """Change the status; ``started_at``/``ended_at`` are only written when given."""
        values: dict[str, Any] = {"status": status}
        if started_at is not None:
            values["started_at"] = started_at
        if ended_at is not None:
            values["ended_at"] = ended_at
        return await self._update(session_id, **values)

    async def set_plan_version(self, session_id: str, plan_version: int) -> Session:
        return await self._update(session_id, plan_version=plan_version)

    async def add_usage(self, session_id: str, delta: Usage) -> Session:
        """Atomically add ``delta`` to the session's usage (row locked ``FOR UPDATE``)."""
        row = await self._one_or_none(
            select(SessionRow).where(SessionRow.id == session_id).with_for_update()
        )
        if row is None:
            raise NotFoundError(f"session {session_id} does not exist")
        total = Usage.model_validate(row.usage).plus(delta)
        return await self._update(session_id, usage=total.model_dump(mode="json"))

    async def set_runtime_owner(
        self, session_id: str, owner: str | None, lease_expires_at: datetime | None
    ) -> None:
        """Record which runtime process owns the session (SPEC §5.1 decision 3)."""
        await self._update(
            session_id, runtime_owner=owner, runtime_lease_expires_at=lease_expires_at
        )


# --- plans ------------------------------------------------------------------------------


def _plan_from_row(row: PlanRow) -> Plan:
    return Plan(
        session_id=row.session_id,
        version=row.version,
        proposal=PlanProposal.model_validate(row.proposal),
        created_at=row.created_at,
        approved_at=row.approved_at,
    )


class PlanRepository(_Repository):
    """Plans are immutable versions 1, 2, 3, ... per session; only ``approved_at``
    changes after insert."""

    async def insert(self, plan: Plan, *, source: str = "planner") -> Plan:
        """Store a plan version. ``source`` is ``planner``, ``user`` or ``supervisor``."""
        await self._add(
            PlanRow(
                session_id=plan.session_id,
                version=plan.version,
                proposal=plan.proposal.model_dump(mode="json"),
                source=source,
                created_at=plan.created_at,
                approved_at=plan.approved_at,
            ),
            f"plan version {plan.version} of session {plan.session_id}",
        )
        return plan

    async def next_version(self, session_id: str) -> int:
        current = await self.tx.scalar(
            select(func.max(PlanRow.version)).where(PlanRow.session_id == session_id)
        )
        return (current or 0) + 1

    async def find(self, session_id: str, version: int | None = None) -> Plan | None:
        """The given version, or the latest when ``version`` is ``None``."""
        stmt = select(PlanRow).where(PlanRow.session_id == session_id)
        if version is None:
            stmt = stmt.order_by(PlanRow.version.desc()).limit(1)
        else:
            stmt = stmt.where(PlanRow.version == version)
        row = await self._one_or_none(stmt)
        return None if row is None else _plan_from_row(row)

    async def get(self, session_id: str, version: int | None = None) -> Plan:
        plan = await self.find(session_id, version)
        if plan is None:
            which = "any plan" if version is None else f"plan version {version}"
            raise NotFoundError(f"session {session_id} has no {which}")
        return plan

    async def list(self, session_id: str) -> builtins.list[Plan]:
        """All versions, oldest first."""
        stmt = select(PlanRow).where(PlanRow.session_id == session_id).order_by(PlanRow.version)
        return [_plan_from_row(r) for r in await self._all(stmt)]

    async def approve(self, session_id: str, version: int, approved_at: datetime) -> Plan:
        result = await self.tx.execute(
            update(PlanRow)
            .where(PlanRow.session_id == session_id, PlanRow.version == version)
            .values(approved_at=approved_at)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError(f"session {session_id} has no plan version {version}")
        return await self.get(session_id, version)


# --- tasks ------------------------------------------------------------------------------


def _task_from_row(row: TaskRow) -> Task:
    return Task.model_validate(
        {
            "id": row.id,
            "session_id": row.session_id,
            "plan_version": row.plan_version,
            "key": row.key,
            "title": row.title,
            "goal": row.goal,
            "acceptance_criteria": row.acceptance_criteria,
            "depends_on": row.depends_on,
            "inputs": row.inputs,
            "outputs": row.outputs,
            "tags": row.tags,
            "suggested_tools": row.suggested_tools,
            "max_attempts": row.max_attempts,
            "status": row.status,
            "assigned_agent_id": row.assigned_agent_id,
            "attempt": row.attempt,
            "rework_count": row.rework_count,
            "result": row.result,
            "created_by": row.created_by,
        }
    )


class TaskRepository(_Repository):
    async def upsert(self, task: Task) -> Task:
        """Insert the task or overwrite every field of the row with the same id
        (``task.created`` / ``task.updated`` carry full snapshots)."""
        values = _row_values(task)
        changes = {k: v for k, v in values.items() if k != "id"}
        changes["updated_at"] = func.now()
        try:
            async with self.tx.begin_nested():
                await self.tx.execute(
                    pg_insert(TaskRow)
                    .values(**values)
                    .on_conflict_do_update(index_elements=[TaskRow.id], set_=changes)
                )
        except IntegrityError:
            raise ConflictError(
                f"task key {task.key} already exists in session {task.session_id}"
            ) from None
        return task

    async def find(self, task_id: str) -> Task | None:
        row = await self._one_or_none(select(TaskRow).where(TaskRow.id == task_id))
        return None if row is None else _task_from_row(row)

    async def get(self, task_id: str) -> Task:
        task = await self.find(task_id)
        if task is None:
            raise NotFoundError(f"task {task_id} does not exist")
        return task

    async def get_by_key(self, session_id: str, key: str) -> Task:
        row = await self._one_or_none(
            select(TaskRow).where(TaskRow.session_id == session_id, TaskRow.key == key)
        )
        if row is None:
            raise NotFoundError(f"task {key} does not exist in session {session_id}")
        return _task_from_row(row)

    async def list(
        self, session_id: str, *, statuses: Sequence[TaskStatus] | None = None
    ) -> builtins.list[Task]:
        """Tasks of a session in creation order (then key)."""
        stmt = select(TaskRow).where(TaskRow.session_id == session_id)
        if statuses is not None:
            stmt = stmt.where(TaskRow.status.in_(statuses))
        stmt = stmt.order_by(TaskRow.created_at, TaskRow.key)
        return [_task_from_row(r) for r in await self._all(stmt)]

    async def set_status(self, task_id: str, status: TaskStatus) -> Task:
        result = await self.tx.execute(
            update(TaskRow)
            .where(TaskRow.id == task_id)
            .values(status=status, updated_at=func.now())
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError(f"task {task_id} does not exist")
        return await self.get(task_id)


# --- agents -----------------------------------------------------------------------------


def _agent_from_row(row: AgentRow) -> Agent:
    return Agent.model_validate(
        {
            "id": row.id,
            "session_id": row.session_id,
            "name": row.name,
            "color": row.color,
            "status": row.status,
            "sandbox": row.sandbox,
            "current_task_id": row.current_task_id,
            "step": row.step,
            "stream_url": row.stream_url,
            "interactive_stream_url": row.interactive_stream_url,
            "last_heartbeat_at": row.last_heartbeat_at,
        }
    )


class AgentRepository(_Repository):
    async def upsert(self, agent: Agent) -> Agent:
        """Insert the agent or overwrite its row (``agent.*`` events carry snapshots)."""
        values = _row_values(agent)
        changes = {k: v for k, v in values.items() if k != "id"}
        try:
            async with self.tx.begin_nested():
                await self.tx.execute(
                    pg_insert(AgentRow)
                    .values(**values)
                    .on_conflict_do_update(index_elements=[AgentRow.id], set_=changes)
                )
        except IntegrityError:
            raise ConflictError(
                f"agent name {agent.name} already exists in session {agent.session_id}"
            ) from None
        return agent

    async def find(self, agent_id: str) -> Agent | None:
        row = await self._one_or_none(select(AgentRow).where(AgentRow.id == agent_id))
        return None if row is None else _agent_from_row(row)

    async def get(self, agent_id: str) -> Agent:
        agent = await self.find(agent_id)
        if agent is None:
            raise NotFoundError(f"agent {agent_id} does not exist")
        return agent

    async def list(self, session_id: str) -> builtins.list[Agent]:
        stmt = (
            select(AgentRow)
            .where(AgentRow.session_id == session_id)
            .order_by(AgentRow.created_at, AgentRow.name)
        )
        return [_agent_from_row(r) for r in await self._all(stmt)]

    async def _update(self, agent_id: str, **values: Any) -> Agent:
        result = await self.tx.execute(
            update(AgentRow).where(AgentRow.id == agent_id).values(**values)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError(f"agent {agent_id} does not exist")
        return await self.get(agent_id)

    async def set_status(self, agent_id: str, status: AgentStatus) -> Agent:
        return await self._update(agent_id, status=status)

    async def record_heartbeat(self, agent_id: str, at: datetime, step: int | None = None) -> Agent:
        values: dict[str, Any] = {"last_heartbeat_at": at}
        if step is not None:
            values["step"] = step
        return await self._update(agent_id, **values)


# --- board ------------------------------------------------------------------------------


def _board_from_row(row: BoardEntryRow) -> BoardEntry:
    return BoardEntry.model_validate(
        {
            "id": row.id,
            "session_id": row.session_id,
            "seq": row.seq,
            "author": row.author,
            "kind": row.kind,
            "title": row.title,
            "body": row.body,
            "tags": row.tags,
            "task_key": row.task_key,
            "refs": row.refs,
            "to": row.to_agents,
            "status": row.status,
            "artifact_id": row.artifact_id,
            "created_at": row.created_at,
        }
    )


@dataclass(frozen=True, slots=True)
class ClaimMatch:
    """An open claim similar to a proposed one; ``score`` is in 0..1.1."""

    entry: BoardEntry
    score: float


CLAIM_TAG_BOOST = 0.1
"""Added to the title similarity of a claim that shares at least one tag."""


class BoardRepository(_Repository):
    async def insert(self, entry: BoardEntry) -> BoardEntry:
        await self._add(
            BoardEntryRow(
                id=entry.id,
                session_id=entry.session_id,
                seq=entry.seq,
                author=entry.author.model_dump(mode="json"),
                author_type=entry.author.type,
                author_id=entry.author.id,
                kind=entry.kind,
                title=entry.title,
                body=entry.body,
                tags=list(entry.tags),
                task_key=entry.task_key,
                refs=list(entry.refs),
                to_agents=None if entry.to is None else list(entry.to),
                status=entry.status,
                artifact_id=entry.artifact_id,
                created_at=entry.created_at,
            ),
            f"board entry {entry.id} (or seq {entry.seq} of session {entry.session_id})",
        )
        return entry

    async def find(self, entry_id: str) -> BoardEntry | None:
        row = await self._one_or_none(select(BoardEntryRow).where(BoardEntryRow.id == entry_id))
        return None if row is None else _board_from_row(row)

    async def get(self, entry_id: str) -> BoardEntry:
        entry = await self.find(entry_id)
        if entry is None:
            raise NotFoundError(f"board entry {entry_id} does not exist")
        return entry

    async def set_status(self, entry_id: str, status: BoardEntryStatus) -> BoardEntry:
        result = await self.tx.execute(
            update(BoardEntryRow).where(BoardEntryRow.id == entry_id).values(status=status)
        )
        if result.rowcount == 0:  # type: ignore[attr-defined]
            raise NotFoundError(f"board entry {entry_id} does not exist")
        return await self.get(entry_id)

    async def list(
        self,
        session_id: str,
        *,
        after_seq: int = 0,
        kinds: Sequence[BoardEntryKind] | None = None,
        statuses: Sequence[BoardEntryStatus] | None = None,
        tags: Sequence[str] | None = None,
        task_key: str | None = None,
        author_id: str | None = None,
        limit: int = 200,
    ) -> builtins.list[BoardEntry]:
        """Entries with ``seq > after_seq`` in ``seq`` order. ``tags`` matches entries
        sharing at least one tag."""
        stmt = select(BoardEntryRow).where(
            BoardEntryRow.session_id == session_id, BoardEntryRow.seq > after_seq
        )
        if kinds is not None:
            stmt = stmt.where(BoardEntryRow.kind.in_(kinds))
        if statuses is not None:
            stmt = stmt.where(BoardEntryRow.status.in_(statuses))
        if tags:
            stmt = stmt.where(BoardEntryRow.tags.overlap(list(tags)))
        if task_key is not None:
            stmt = stmt.where(BoardEntryRow.task_key == task_key)
        if author_id is not None:
            stmt = stmt.where(BoardEntryRow.author_id == author_id)
        stmt = stmt.order_by(BoardEntryRow.seq).limit(max(1, min(limit, 1000)))
        return [_board_from_row(r) for r in await self._all(stmt)]

    async def search(
        self,
        session_id: str,
        query: str,
        *,
        kinds: Sequence[BoardEntryKind] | None = None,
        limit: int = 20,
    ) -> builtins.list[BoardEntry]:
        """Full-text search over title + body (English stemming), best match first.

        ``query`` uses web-search syntax (``"exact phrase"``, ``or``, ``-exclude``) and
        is passed as a bind parameter, never interpolated.
        """
        if not query.strip():
            return []
        tsquery = func.websearch_to_tsquery("english", query)
        stmt = select(BoardEntryRow).where(
            BoardEntryRow.session_id == session_id, BoardEntryRow.search.op("@@")(tsquery)
        )
        if kinds is not None:
            stmt = stmt.where(BoardEntryRow.kind.in_(kinds))
        stmt = stmt.order_by(
            func.ts_rank(BoardEntryRow.search, tsquery).desc(), BoardEntryRow.seq.desc()
        ).limit(max(1, min(limit, 200)))
        return [_board_from_row(r) for r in await self._all(stmt)]

    async def similar_claims(
        self,
        session_id: str,
        title: str,
        tags: Sequence[str] = (),
        threshold: float = 0.35,
        *,
        exclude_author_id: str | None = None,
        limit: int = 5,
    ) -> builtins.list[ClaimMatch]:
        """Open claims that look like the same scope as ``title``/``tags``.

        ``score = similarity(title, :title)`` (pg_trgm, 0..1) plus ``CLAIM_TAG_BOOST``
        when the claim shares a tag. Returns claims with ``score >= threshold``, best
        first. ``exclude_author_id`` skips the claimant's own claims.
        """
        score: ColumnElement[float] = func.similarity(BoardEntryRow.title, title)
        if tags:
            score = score + case(
                (BoardEntryRow.tags.overlap(list(tags)), CLAIM_TAG_BOOST), else_=0.0
            )
        scored = score.label("score")
        stmt = select(BoardEntryRow, scored).where(
            BoardEntryRow.session_id == session_id,
            BoardEntryRow.kind == "claim",
            BoardEntryRow.status == "open",
        )
        if exclude_author_id is not None:
            stmt = stmt.where(BoardEntryRow.author_id.is_distinct_from(exclude_author_id))
        stmt = (
            stmt.where(score >= threshold)
            .order_by(scored.desc(), BoardEntryRow.seq)
            .limit(max(1, min(limit, 50)))
        )
        rows = (await self.tx.execute(stmt.execution_options(populate_existing=True))).all()
        return [ClaimMatch(entry=_board_from_row(row), score=float(s)) for row, s in rows]


# --- artifacts --------------------------------------------------------------------------


def _artifact_from_row(row: ArtifactRow) -> Artifact:
    return Artifact(
        id=row.id,
        session_id=row.session_id,
        name=row.name,
        mime=row.mime,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        produced_by=Actor.model_validate(row.produced_by),
        task_key=row.task_key,
        source_path=row.source_path,
        storage_key=row.storage_key,
        version=row.version,
        description=row.description,
        created_at=row.created_at,
    )


class ArtifactRepository(_Repository):
    async def insert(self, artifact: Artifact) -> Artifact:
        await self._add(
            ArtifactRow(**_row_values(artifact)),
            f"artifact {artifact.name} v{artifact.version} in session {artifact.session_id}",
        )
        return artifact

    async def find(self, artifact_id: str) -> Artifact | None:
        row = await self._one_or_none(select(ArtifactRow).where(ArtifactRow.id == artifact_id))
        return None if row is None else _artifact_from_row(row)

    async def get(self, artifact_id: str) -> Artifact:
        artifact = await self.find(artifact_id)
        if artifact is None:
            raise NotFoundError(f"artifact {artifact_id} does not exist")
        return artifact

    async def list(
        self, session_id: str, *, task_key: str | None = None
    ) -> builtins.list[Artifact]:
        stmt = select(ArtifactRow).where(ArtifactRow.session_id == session_id)
        if task_key is not None:
            stmt = stmt.where(ArtifactRow.task_key == task_key)
        stmt = stmt.order_by(ArtifactRow.created_at, ArtifactRow.name, ArtifactRow.version)
        return [_artifact_from_row(r) for r in await self._all(stmt)]

    async def latest(self, session_id: str, name: str) -> Artifact | None:
        row = await self._one_or_none(
            select(ArtifactRow)
            .where(ArtifactRow.session_id == session_id, ArtifactRow.name == name)
            .order_by(ArtifactRow.version.desc())
            .limit(1)
        )
        return None if row is None else _artifact_from_row(row)

    async def next_version(self, session_id: str, name: str) -> int:
        current = await self.tx.scalar(
            select(func.max(ArtifactRow.version)).where(
                ArtifactRow.session_id == session_id, ArtifactRow.name == name
            )
        )
        return (current or 0) + 1


# --- approvals --------------------------------------------------------------------------


def _approval_from_row(row: ApprovalRow) -> Approval:
    return Approval.model_validate(
        {
            "id": row.id,
            "session_id": row.session_id,
            "agent_id": row.agent_id,
            "task_key": row.task_key,
            "action_summary": row.action_summary,
            "risk": row.risk,
            "frame_id": row.frame_id,
            "status": row.status,
            "decided_by": row.decided_by,
            "note": row.note,
            "created_at": row.created_at,
            "decided_at": row.decided_at,
            "expires_at": row.expires_at,
        }
    )


class ApprovalRepository(_Repository):
    async def insert(self, approval: Approval) -> Approval:
        await self._add(ApprovalRow(**_row_values(approval)), f"approval {approval.id}")
        return approval

    async def find(self, approval_id: str) -> Approval | None:
        row = await self._one_or_none(select(ApprovalRow).where(ApprovalRow.id == approval_id))
        return None if row is None else _approval_from_row(row)

    async def get(self, approval_id: str) -> Approval:
        approval = await self.find(approval_id)
        if approval is None:
            raise NotFoundError(f"approval {approval_id} does not exist")
        return approval

    async def list(
        self, session_id: str, *, statuses: Sequence[ApprovalStatus] | None = None
    ) -> builtins.list[Approval]:
        stmt = select(ApprovalRow).where(ApprovalRow.session_id == session_id)
        if statuses is not None:
            stmt = stmt.where(ApprovalRow.status.in_(statuses))
        stmt = stmt.order_by(ApprovalRow.created_at, ApprovalRow.id)
        return [_approval_from_row(r) for r in await self._all(stmt)]

    async def decide(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        decided_by: Actor | None,
        decided_at: datetime,
        note: str | None = None,
    ) -> Approval:
        """Move a pending approval to ``approved``/``denied``/``expired``.

        Locks the row; raises ``ConflictError`` if it was already decided, so two
        racing decisions cannot both win.
        """
        if status == "pending":
            raise ValueError("decide() needs a final status")
        row = await self._one_or_none(
            select(ApprovalRow).where(ApprovalRow.id == approval_id).with_for_update()
        )
        if row is None:
            raise NotFoundError(f"approval {approval_id} does not exist")
        if row.status != "pending":
            raise ConflictError(f"approval {approval_id} is already {row.status}")
        await self.tx.execute(
            update(ApprovalRow)
            .where(ApprovalRow.id == approval_id)
            .values(
                status=status,
                decided_by=None if decided_by is None else decided_by.model_dump(mode="json"),
                decided_at=decided_at,
                note=note,
            )
        )
        return await self.get(approval_id)

    async def due_for_expiry(self, now: datetime) -> builtins.list[Approval]:
        """Pending approvals whose ``expires_at`` has passed (all sessions)."""
        stmt = (
            select(ApprovalRow)
            .where(ApprovalRow.status == "pending", ApprovalRow.expires_at <= now)
            .order_by(ApprovalRow.expires_at)
        )
        return [_approval_from_row(r) for r in await self._all(stmt)]


# --- frames -----------------------------------------------------------------------------


def _frame_from_row(row: FrameRow) -> Frame:
    return Frame(
        id=row.id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        step=row.step,
        task_key=row.task_key,
        ts=row.ts,
        storage_key_small=row.storage_key_small,
        storage_key_full=row.storage_key_full,
        action_label=row.action_label,
        phash=row.phash,
    )


class FrameRepository(_Repository):
    async def insert(self, frame: Frame) -> Frame:
        await self._add(FrameRow(**_row_values(frame)), f"frame {frame.id}")
        return frame

    async def find(self, frame_id: str) -> Frame | None:
        row = await self._one_or_none(select(FrameRow).where(FrameRow.id == frame_id))
        return None if row is None else _frame_from_row(row)

    async def get(self, frame_id: str) -> Frame:
        frame = await self.find(frame_id)
        if frame is None:
            raise NotFoundError(f"frame {frame_id} does not exist")
        return frame

    async def list(
        self,
        session_id: str,
        *,
        agent_id: str | None = None,
        after_ts: datetime | None = None,
        limit: int = 500,
    ) -> builtins.list[Frame]:
        """Frames in time order (replay)."""
        stmt = select(FrameRow).where(FrameRow.session_id == session_id)
        if agent_id is not None:
            stmt = stmt.where(FrameRow.agent_id == agent_id)
        if after_ts is not None:
            stmt = stmt.where(FrameRow.ts > after_ts)
        stmt = stmt.order_by(FrameRow.ts, FrameRow.id).limit(max(1, min(limit, 5000)))
        return [_frame_from_row(r) for r in await self._all(stmt)]

    async def latest(self, session_id: str, agent_id: str) -> Frame | None:
        row = await self._one_or_none(
            select(FrameRow)
            .where(FrameRow.session_id == session_id, FrameRow.agent_id == agent_id)
            .order_by(FrameRow.step.desc(), FrameRow.ts.desc())
            .limit(1)
        )
        return None if row is None else _frame_from_row(row)


# --- bundle -----------------------------------------------------------------------------


class Repositories:
    """Every repository bound to one transaction: ``repos = Repositories(tx)``."""

    def __init__(self, tx: AsyncSession) -> None:
        self.tx = tx
        self.sessions = SessionRepository(tx)
        self.plans = PlanRepository(tx)
        self.tasks = TaskRepository(tx)
        self.agents = AgentRepository(tx)
        self.board = BoardRepository(tx)
        self.artifacts = ArtifactRepository(tx)
        self.approvals = ApprovalRepository(tx)
        self.frames = FrameRepository(tx)
