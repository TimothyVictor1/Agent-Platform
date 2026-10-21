"""BoardService interface (SPEC §6.4, §13.1). The implementation is added here by T14.

The board is the shared, append-only collaboration space of a session. Every post
becomes a ``board.entry_posted`` event; status changes become
``board.entry_status_changed`` events. Entries are untrusted data: they are never
executed, never rendered as HTML, and only ``decision`` entries (supervisor/user)
and user steering notes count as instructions for agents.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import Actor, BoardEntryKind, BoardEntryStatus

__all__ = ["BoardService"]


@runtime_checkable
class BoardService(Protocol):
    """Posting, searching and lifecycle of board entries."""

    async def post(
        self,
        session_id: str,
        draft: BoardEntryDraft,
        *,
        author: Actor,
        step: int | None = None,
    ) -> BoardEntry:
        """Validate and post an entry; returns it with ``id``/``seq``/``status`` set.

        Kind rules (raise ``BoardRuleError`` with a model-friendly message):

        * ``answer`` must have exactly one ref, to an open ``question`` of the same
          session; posting it resolves the question (status event).
        * ``decision`` only from supervisor or user authors.
        * ``question``/``claim``/``blocker`` start with status ``open``; other kinds
          have ``status=None``.
        * ``to`` (direct message) must list agent ids of the same session.
        * ``artifact`` entries need ``artifact_id``.

        Agent authors are rate limited per SPEC §13.1 using ``step`` (the agent's
        current step number): max 1 ``progress`` per 10 steps, max 6 posts per 10
        steps; over the limit raise ``RateLimitedError``. A new claim overlapping an
        open claim triggers a ``supervisor.triggered`` (``claim_conflict``) event.
        """
        ...

    async def set_status(
        self,
        entry_id: str,
        status: BoardEntryStatus,
        *,
        actor: Actor,
        reason: str | None = None,
    ) -> BoardEntry:
        """Change the status of a question/claim/blocker (e.g. release a claim).
        Only the author, the supervisor or the user may do so (``BoardRuleError``)."""
        ...

    async def get(self, entry_id: str) -> BoardEntry:
        """Raise ``NotFoundError`` if missing."""
        ...

    async def search(
        self,
        session_id: str,
        *,
        query: str | None = None,
        kinds: list[BoardEntryKind] | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        viewer_agent_id: str | None = None,
    ) -> list[BoardEntry]:
        """Full-text search on title + body with filters, newest first. When
        ``viewer_agent_id`` is set, direct messages not addressed to (or written by)
        that agent are excluded."""
        ...

    async def entries_after(
        self,
        session_id: str,
        after_seq: int,
        *,
        viewer_agent_id: str | None = None,
        limit: int = 200,
    ) -> list[BoardEntry]:
        """Entries with ``seq > after_seq`` in ascending seq order, visibility-filtered
        like ``search`` (used by the digest builder and the API)."""
        ...

    async def wait_for_answer(self, question_id: str, timeout_s: float) -> BoardEntry | None:
        """Wait until an ``answer`` referencing ``question_id`` is posted (or already
        exists). Returns it, or ``None`` after ``timeout_s``."""
        ...
