"""In-memory ``EventStore`` with the same ordering guarantees as Postgres (one process)."""

from __future__ import annotations

import asyncio

from agentplatform.contracts.events import Event, EventDraft, SessionCreated, build_event
from agentplatform.db.event_store import DraftFactory, ReadModelUpdate
from agentplatform.errors import NotFoundError

__all__ = ["InMemoryEventStore"]


class InMemoryEventStore:
    """``EventStore`` for tests.

    With ``strict=True`` appending to a session that has no ``session.created`` event
    raises ``NotFoundError`` (like Postgres without a counter row). The default
    (``strict=False``) creates the counter on first append so tests can emit any
    event directly. ``read_model_update`` is awaited with ``tx=None``; if it raises,
    the event is not stored.
    """

    def __init__(self, *, strict: bool = False) -> None:
        self._strict = strict
        self._events: dict[str, list[Event]] = {}
        self._lock = asyncio.Lock()

    async def append(
        self,
        session_id: str,
        draft: EventDraft | DraftFactory,
        read_model_update: ReadModelUpdate | None = None,
    ) -> Event:
        async with self._lock:
            events = self._events.get(session_id)
            seq = (len(events) if events else 0) + 1
            resolved = draft if isinstance(draft, EventDraft) else draft(seq)
            if events is None:
                if self._strict and not isinstance(resolved.payload, SessionCreated):
                    raise NotFoundError(f"session {session_id} does not exist")
                events = []
            event = build_event(resolved, session_id=session_id, seq=seq)
            if read_model_update is not None:
                await read_model_update(event, None)
            events.append(event)
            self._events[session_id] = events
            return event

    async def read(self, session_id: str, after_seq: int = 0, limit: int = 500) -> list[Event]:
        limit = max(1, min(limit, 5000))
        events = self._events.get(session_id, [])
        return events[after_seq : after_seq + limit]

    async def last_seq(self, session_id: str) -> int:
        return len(self._events.get(session_id, []))

    def all(self, session_id: str) -> list[Event]:
        """Every event of a session (test helper)."""
        return list(self._events.get(session_id, []))

    def of_type(self, session_id: str, event_type: str) -> list[Event]:
        """Events of one type (test helper)."""
        return [e for e in self._events.get(session_id, []) if e.type == event_type]
