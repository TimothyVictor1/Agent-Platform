"""SQLAlchemy ORM models for every table in SPEC §17.

The schema is created only by Alembic migrations (``backend/alembic``); never call
``create_all`` outside tests. Repositories (T04) map these rows to contract models.

Conventions: ids are UUIDv7 text; timestamps are ``timestamptz``; nested contract
objects are ``jsonb`` holding ``model_dump(mode="json")`` output.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, ClassVar

from sqlalchemy import (
    BigInteger,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "AgentRow",
    "ApprovalRow",
    "ArtifactRow",
    "Base",
    "BoardEntryRow",
    "EventRow",
    "FrameRow",
    "PlanRow",
    "SessionCounterRow",
    "SessionRow",
    "TaskRow",
    "UserRow",
]

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
    type_annotation_map: ClassVar[dict[Any, Any]] = {
        dict[str, Any]: JSONB,
        list[Any]: JSONB,
        datetime: TS,
    }


def _session_fk() -> Any:
    return ForeignKey("sessions.id", ondelete="CASCADE")


class UserRow(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    agent_count: Mapped[int] = mapped_column(Integer)
    model_profile: Mapped[str] = mapped_column(String(100))
    sandbox_provider: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(32))
    plan_version: Mapped[int] = mapped_column(Integer, default=0)
    budgets: Mapped[dict[str, Any]]
    usage: Mapped[dict[str, Any]]
    created_at: Mapped[datetime]
    started_at: Mapped[datetime | None]
    ended_at: Mapped[datetime | None]
    # Runtime ownership lease (SPEC §5.1 decision 3): which runtime process runs it.
    runtime_owner: Mapped[str | None] = mapped_column(String(200))
    runtime_lease_expires_at: Mapped[datetime | None]


class SessionCounterRow(Base):
    """Per-session event sequence counter, locked ``FOR UPDATE`` on append."""

    __tablename__ = "session_counters"

    session_id: Mapped[str] = mapped_column(_session_fk(), primary_key=True)
    next_seq: Mapped[int] = mapped_column(BigInteger, default=1)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
        Index("ix_events_session_id_type", "session_id", "type"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(_session_fk())
    seq: Mapped[int] = mapped_column(BigInteger)
    type: Mapped[str] = mapped_column(String(64))
    ts: Mapped[datetime]
    actor: Mapped[dict[str, Any]]
    payload: Mapped[dict[str, Any]]
    causation_id: Mapped[str | None] = mapped_column(String(36))
    correlation_id: Mapped[str | None] = mapped_column(String(64))


class PlanRow(Base):
    __tablename__ = "plans"

    session_id: Mapped[str] = mapped_column(_session_fk(), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    proposal: Mapped[dict[str, Any]]
    source: Mapped[str] = mapped_column(String(32), default="planner")
    created_at: Mapped[datetime]
    approved_at: Mapped[datetime | None]


class TaskRow(Base):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("session_id", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(_session_fk(), index=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    key: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(80))
    goal: Mapped[str] = mapped_column(Text)
    acceptance_criteria: Mapped[list[Any]]
    depends_on: Mapped[list[Any]]
    inputs: Mapped[list[Any]]
    outputs: Mapped[list[Any]]
    tags: Mapped[list[Any]]
    suggested_tools: Mapped[list[Any]]
    status: Mapped[str] = mapped_column(String(32))
    assigned_agent_id: Mapped[str | None] = mapped_column(String(36))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    rework_count: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict[str, Any] | None]
    created_by: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now())


class AgentRow(Base):
    __tablename__ = "agents"
    __table_args__ = (UniqueConstraint("session_id", "name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(_session_fk(), index=True)
    name: Mapped[str] = mapped_column(String(40))
    color: Mapped[str] = mapped_column(String(7))
    status: Mapped[str] = mapped_column(String(32))
    sandbox: Mapped[dict[str, Any] | None]
    current_task_id: Mapped[str | None] = mapped_column(String(36))
    step: Mapped[int] = mapped_column(Integer, default=0)
    stream_url: Mapped[str | None] = mapped_column(Text)
    interactive_stream_url: Mapped[str | None] = mapped_column(Text)
    last_heartbeat_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class BoardEntryRow(Base):
    __tablename__ = "board_entries"
    __table_args__ = (
        UniqueConstraint("session_id", "seq"),
        Index("ix_board_entries_search", "search", postgresql_using="gin"),
        Index(
            "ix_board_entries_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index("ix_board_entries_tags", "tags", postgresql_using="gin"),
        Index("ix_board_entries_session_id_kind_status", "session_id", "kind", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(_session_fk())
    seq: Mapped[int] = mapped_column(BigInteger)
    author: Mapped[dict[str, Any]]
    author_type: Mapped[str] = mapped_column(String(16))
    author_id: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    task_key: Mapped[str | None] = mapped_column(String(16))
    refs: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    to_agents: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    """``BoardEntry.to``: ``NULL`` = everyone."""
    status: Mapped[str | None] = mapped_column(String(16))
    artifact_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime]
    search: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(body, ''))",
            persisted=True,
        ),
    )


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("session_id", "name", "version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(_session_fk(), index=True)
    name: Mapped[str] = mapped_column(String(255))
    mime: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64))
    produced_by: Mapped[dict[str, Any]]
    task_key: Mapped[str | None] = mapped_column(String(16))
    source_path: Mapped[str | None] = mapped_column(Text)
    storage_key: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]


class ApprovalRow(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_session_id_status", "session_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(_session_fk())
    agent_id: Mapped[str] = mapped_column(String(36))
    task_key: Mapped[str | None] = mapped_column(String(16))
    action_summary: Mapped[str] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(String(8))
    frame_id: Mapped[str | None] = mapped_column(String(36))
    status: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[dict[str, Any] | None]
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    decided_at: Mapped[datetime | None]
    expires_at: Mapped[datetime | None]


class FrameRow(Base):
    __tablename__ = "frames"
    __table_args__ = (
        Index("ix_frames_session_id_agent_id_step", "session_id", "agent_id", "step"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(_session_fk())
    agent_id: Mapped[str] = mapped_column(String(36))
    step: Mapped[int] = mapped_column(Integer)
    task_key: Mapped[str | None] = mapped_column(String(16))
    ts: Mapped[datetime]
    storage_key_small: Mapped[str] = mapped_column(Text)
    storage_key_full: Mapped[str | None] = mapped_column(Text)
    action_label: Mapped[str] = mapped_column(String(200), default="")
    phash: Mapped[str] = mapped_column(String(32))
