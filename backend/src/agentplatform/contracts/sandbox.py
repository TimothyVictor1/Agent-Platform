"""Value types used by the sandbox interface (SPEC §9.1)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from agentplatform.contracts.common import Contract, UtcDatetime

__all__ = ["CommandResult", "FileInfo", "SandboxSpec", "StreamUrls"]

COMMAND_OUTPUT_LIMIT = 8 * 1024


class SandboxSpec(Contract):
    """How to create a sandbox. ``env`` must never contain secrets.

    ``session_id``/``agent_id`` are used only for provider-side labels (cleanup,
    debugging); they are not secret.
    """

    screen: tuple[int, int] = (1280, 800)
    timeout_minutes: int = Field(default=60, ge=1, le=24 * 60)
    env: dict[str, str] = Field(default_factory=dict)
    network: Literal["open", "allowlist"] = "open"
    allowlist: list[str] = Field(default_factory=list)
    session_id: str | None = None
    agent_id: str | None = None


class CommandResult(Contract):
    """Result of ``run_command``. stdout/stderr are truncated to 8 KB each."""

    stdout: str = ""
    stderr: str = ""
    exit_code: int
    truncated: bool = False
    timed_out: bool = False


class FileInfo(Contract):
    path: str
    name: str
    is_dir: bool
    size_bytes: int = Field(default=0, ge=0)
    modified_at: UtcDatetime | None = None


class StreamUrls(Contract):
    view_only_url: str
    interactive_url: str
