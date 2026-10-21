"""EventRecorder: the one way to emit a persisted event (append, then publish)."""

from __future__ import annotations

import asyncio

import structlog

from agentplatform.bus.base import EventBus
from agentplatform.contracts.events import Event, EventDraft
from agentplatform.db.event_store import DraftFactory, EventStore, ReadModelUpdate

__all__ = ["EventRecorder"]

log = structlog.get_logger(__name__)


class EventRecorder:
    """Appends an event to the store and then publishes it on the bus.

    Publishing happens after the commit, under a per-session lock so events of a
    session reach the bus in ``seq`` order from this process. A failed publish is
    logged and swallowed: the event is already durable, and subscribers recover
    missed events from the store (see ``bus.base``).
    """

    def __init__(self, store: EventStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus
        self._locks: dict[str, asyncio.Lock] = {}

    async def record(
        self,
        session_id: str,
        draft: EventDraft | DraftFactory,
        read_model_update: ReadModelUpdate | None = None,
    ) -> Event:
        lock = self._locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            event = await self._store.append(session_id, draft, read_model_update)
            try:
                await self._bus.publish(event)
            except Exception:
                log.warning(
                    "event_publish_failed", session_id=session_id, seq=event.seq, type=event.type
                )
        return event

    def forget_session(self, session_id: str) -> None:
        """Drop the per-session lock once a session is finished."""
        self._locks.pop(session_id, None)
