"""Shared building blocks for every contract: base model, ids, time, enums, Actor."""

from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import UTC, datetime
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, PlainSerializer

__all__ = [
    "Actor",
    "ActorType",
    "AgentStatus",
    "ApprovalStatus",
    "BoardEntryKind",
    "BoardEntryStatus",
    "Confidence",
    "Contract",
    "CoordinateMode",
    "HealthStatus",
    "OutputKind",
    "RiskLevel",
    "SandboxProviderName",
    "SessionMode",
    "SessionStatus",
    "SuggestedTool",
    "TaskCreator",
    "TaskStatus",
    "UtcDatetime",
    "new_id",
    "utcnow",
]


class Contract(BaseModel):
    """Base for every contract model: unknown fields are rejected.

    json_schema_serialization_defaults_required makes fields with defaults
    required in the exported (serialization) schema, because serialized contracts
    always contain them; the generated TypeScript types then need no ?.
    """

    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


def _serialize_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDatetime = Annotated[AwareDatetime, PlainSerializer(_serialize_utc, return_type=str)]
"""A timezone-aware datetime, always serialized as UTC ISO-8601 with a ``Z`` suffix."""


def utcnow() -> datetime:
    """Current time as an aware UTC datetime (millisecond precision, matching Postgres)."""
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


_uuid7_lock = threading.Lock()
_uuid7_last_ms = 0
_uuid7_counter = 0


def new_id() -> str:
    """Return a new UUIDv7 string (RFC 9562): time-ordered, sortable as text.

    48-bit Unix milliseconds, then a 12-bit counter (``rand_a``) that keeps ids
    generated within the same millisecond in this process strictly increasing,
    then 62 random bits.
    """
    global _uuid7_last_ms, _uuid7_counter
    with _uuid7_lock:
        ms = time.time_ns() // 1_000_000
        if ms > _uuid7_last_ms:
            _uuid7_last_ms = ms
            _uuid7_counter = int.from_bytes(os.urandom(2), "big") & 0x3FF  # leave headroom
        else:
            _uuid7_counter += 1
            if _uuid7_counter > 0xFFF:  # counter overflow: borrow the next millisecond
                _uuid7_last_ms += 1
                _uuid7_counter = 0
            ms = _uuid7_last_ms
        counter = _uuid7_counter
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)
    value = (ms & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= counter << 64
    value |= 0b10 << 62  # variant
    value |= rand_b
    return str(uuid.UUID(int=value))


# --- enumerations (Literal unions so they export cleanly to TypeScript) ---------------

SessionStatus = Literal[
    "planning", "awaiting_approval", "running", "paused", "completed", "failed", "stopped"
]
SessionMode = Literal["coordinated", "parallel_isolated", "solo"]
SandboxProviderName = Literal["docker_local", "e2b"]

TaskStatus = Literal[
    "pending",
    "ready",
    "assigned",
    "running",
    "blocked",
    "in_review",
    "done",
    "failed",
    "cancelled",
]
TaskCreator = Literal["planner", "supervisor", "user"]
OutputKind = Literal["text", "json", "artifact"]
SuggestedTool = Literal["browser", "terminal", "office", "files"]
Confidence = Literal["low", "medium", "high"]

AgentStatus = Literal[
    "booting",
    "idle",
    "working",
    "waiting",
    "paused_by_user",
    "awaiting_approval",
    "stuck",
    "error",
    "stopped",
]

ActorType = Literal["agent", "supervisor", "user", "system"]

BoardEntryKind = Literal[
    "fact", "claim", "question", "answer", "decision", "blocker", "progress", "artifact", "note"
]
BoardEntryStatus = Literal["open", "resolved", "superseded"]

RiskLevel = Literal["low", "medium", "high"]
ApprovalStatus = Literal["pending", "approved", "denied", "expired"]

CoordinateMode = Literal["absolute", "normalized_1000", "relative"]


class Actor(Contract):
    """Who did something. ``id`` is the agent id for agents, absent for system."""

    type: ActorType
    id: str | None = None
    name: str = Field(min_length=1, max_length=80)

    @classmethod
    def system(cls) -> Actor:
        return cls(type="system", name="system")

    @classmethod
    def supervisor(cls) -> Actor:
        return cls(type="supervisor", id="supervisor", name="Supervisor")

    @classmethod
    def user(cls, user_id: str | None = None, name: str = "You") -> Actor:
        return cls(type="user", id=user_id, name=name)

    @classmethod
    def agent(cls, agent_id: str, name: str) -> Actor:
        return cls(type="agent", id=agent_id, name=name)


class HealthStatus(Contract):
    """Result of a provider ``health()`` check (models and sandboxes)."""

    ok: bool
    latency_ms: int | None = None
    error: str | None = None
    """Short, user-safe description of the failure. Never contains secrets."""
