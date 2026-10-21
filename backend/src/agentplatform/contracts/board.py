"""Board entries (SPEC §6.4)."""

from __future__ import annotations

from pydantic import Field

from agentplatform.contracts.common import (
    Actor,
    BoardEntryKind,
    BoardEntryStatus,
    Contract,
    UtcDatetime,
)

__all__ = ["BoardEntry", "BoardEntryDraft"]


class BoardEntryDraft(Contract):
    """What an author submits; the BoardService fills id, seq, author, status, time.

    ``to`` = ``None`` means everyone; a list of agent ids makes it a direct message.
    """

    kind: BoardEntryKind
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(default="", max_length=1500)
    tags: list[str] = Field(default_factory=list, max_length=10)
    task_key: str | None = None
    refs: list[str] = Field(default_factory=list)
    to: list[str] | None = None
    artifact_id: str | None = None


class BoardEntry(BoardEntryDraft):
    """A posted entry. ``seq`` equals the ``seq`` of the ``board.entry_posted`` event
    that created it, so digest cursors and event cursors share one ordering."""

    id: str
    session_id: str
    seq: int = Field(ge=1)
    author: Actor
    status: BoardEntryStatus | None = None
    created_at: UtcDatetime
