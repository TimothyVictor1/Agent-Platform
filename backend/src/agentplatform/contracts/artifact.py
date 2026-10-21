"""Artifacts, approvals and frames (SPEC §6.5–§6.7)."""

from __future__ import annotations

from pydantic import Field

from agentplatform.contracts.common import (
    Actor,
    ApprovalStatus,
    Contract,
    RiskLevel,
    UtcDatetime,
)

__all__ = ["Approval", "Artifact", "Frame"]


class Artifact(Contract):
    id: str
    session_id: str
    name: str = Field(min_length=1, max_length=255)
    mime: str
    size_bytes: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    produced_by: Actor
    task_key: str | None = None
    source_path: str | None = None
    storage_key: str
    version: int = Field(default=1, ge=1)
    description: str | None = Field(default=None, max_length=500)
    created_at: UtcDatetime


class Approval(Contract):
    id: str
    session_id: str
    agent_id: str
    task_key: str | None = None
    action_summary: str = Field(min_length=1, max_length=500)
    risk: RiskLevel
    frame_id: str | None = None
    status: ApprovalStatus = "pending"
    decided_by: Actor | None = None
    note: str | None = Field(default=None, max_length=1000)
    created_at: UtcDatetime
    decided_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None


class Frame(Contract):
    """Metadata of one saved screenshot. Bytes live in the ArtifactStore."""

    id: str
    session_id: str
    agent_id: str
    step: int = Field(ge=0)
    task_key: str | None = None
    ts: UtcDatetime
    storage_key_small: str
    storage_key_full: str | None = None
    action_label: str = Field(default="", max_length=200)
    phash: str
