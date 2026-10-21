"""EventStore interface (SPEC §5.1 decision 5, §7.2 rules, §17).

``PostgresEventStore`` (T04) implements it in this module; the test double is
``agentplatform.testing.memory_event_store.InMemoryEventStore``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from agentplatform.contracts.events import Event, EventDraft

__all__ = ["DraftFactory", "EventStore", "ReadModelUpdate"]

ReadModelUpdate = Callable[[Event, object], Awaitable[None]]
"""Applies read-model changes for an event inside the append transaction.

Called as ``await update(event, tx)`` where ``tx`` is the implementation's
transaction handle: a SQLAlchemy ``AsyncSession`` for Postgres, ``None`` for the
in-memory store. If it raises, the whole append (event + read models) is rolled back
and the exception propagates.
"""

DraftFactory = Callable[[int], EventDraft]
"""Builds the draft once the seq is known, for payloads that embed their own seq
(e.g. ``BoardEntry.seq`` equals the seq of its ``board.entry_posted`` event)."""


@runtime_checkable
class EventStore(Protocol):
    """Append-only, per-session ordered event log.

    Guarantees:

    * ``seq`` starts at 1 per session and increases by exactly 1 per appended event
      (no gaps, no duplicates) even with concurrent appenders in several processes.
      Postgres does this with the ``session_counters`` row locked ``FOR UPDATE``.
    * Commit order equals ``seq`` order within a session, so a reader that saw
      ``seq = n`` will never later see a new event with ``seq < n``.
    * The event and its read-model updates commit atomically.
    * ``append`` does not publish to the bus; callers use
      ``agentplatform.eventing.EventRecorder`` which appends, then publishes.
    """

    async def append(
        self,
        session_id: str,
        draft: EventDraft | DraftFactory,
        read_model_update: ReadModelUpdate | None = None,
    ) -> Event:
        """Assign ``id``/``seq``/``ts``, persist the event, apply ``read_model_update``
        in the same transaction, and return the full event.

        ``draft`` may be a factory ``seq -> EventDraft`` for payloads that need the
        seq. Raises ``NotFoundError`` if the session has no counter row (the
        ``session.created`` append creates it).
        """
        ...

    async def read(self, session_id: str, after_seq: int = 0, limit: int = 500) -> list[Event]:
        """Events with ``seq > after_seq`` in ascending order, at most ``limit``
        (1..5000)."""
        ...

    async def last_seq(self, session_id: str) -> int:
        """Highest seq of the session, 0 if none."""
        ...
