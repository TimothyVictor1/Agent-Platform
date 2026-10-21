"""Agents and their sandbox reference (SPEC §6.3)."""

from __future__ import annotations

from pydantic import Field

from agentplatform.contracts.common import (
    AgentStatus,
    Contract,
    SandboxProviderName,
    UtcDatetime,
)

__all__ = ["AGENT_COLORS", "AGENT_NAMES", "Agent", "SandboxRef", "agent_identity"]

AGENT_NAMES: tuple[str, ...] = ("Ada", "Brook", "Cyra", "Dex", "Echo", "Faye")
"""Deterministic agent names, assigned in order of creation within a session."""

AGENT_COLORS: tuple[str, ...] = ("#4F8EF7", "#F59E0B", "#10B981", "#EC4899", "#8B5CF6", "#06B6D4")
"""Agent colors (hex), parallel to ``AGENT_NAMES``."""


def agent_identity(index: int) -> tuple[str, str]:
    """Name and color for the ``index``-th agent of a session (0-based)."""
    if not 0 <= index < len(AGENT_NAMES):
        raise ValueError(f"agent index must be 0..{len(AGENT_NAMES) - 1}")
    return AGENT_NAMES[index], AGENT_COLORS[index]


class SandboxRef(Contract):
    """Enough to reconnect to a sandbox after a runtime restart (no secrets)."""

    provider: SandboxProviderName
    sandbox_id: str
    created_at: UtcDatetime
    expires_at: UtcDatetime | None = None


class Agent(Contract):
    id: str
    session_id: str
    name: str = Field(min_length=1, max_length=40)
    color: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    status: AgentStatus = "booting"
    sandbox: SandboxRef | None = None
    current_task_id: str | None = None
    step: int = Field(default=0, ge=0)
    stream_url: str | None = None
    interactive_stream_url: str | None = None
    last_heartbeat_at: UtcDatetime | None = None
