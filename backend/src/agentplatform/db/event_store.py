"""EventStore interface (SPEC §5.1 decision 5, §7.2 rules, §17).

``PostgresEventStore`` (T04) implements it in this module; the test double is
``agentplatform.testing.memory_event_store.InMemoryEventStore``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol, runtime_checkable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agentplatform.contracts.events import (
    EVENT_ADAPTER,
    Event,
    EventDraft,
    SessionCreated,
    build_event,
)
from agentplatform.db.models import EventRow, SessionCounterRow
from agentplatform.db.repositories import SessionRepository
from agentplatform.errors import ConflictError, InvalidRequestError, NotFoundError

__all__ = ["DraftFactory", "EventStore", "PostgresEventStore", "ReadModelUpdate"]

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


# --- Postgres implementation (T04) ------------------------------------------------------


def _event_to_row(event: Event) -> EventRow:
    return EventRow(
        id=event.id,
        session_id=event.session_id,
        seq=event.seq,
        type=event.type,
        ts=event.ts,
        actor=event.actor.model_dump(mode="json"),
        payload=event.payload.model_dump(mode="json"),
        causation_id=event.causation_id,
        correlation_id=event.correlation_id,
    )


def _row_to_event(row: EventRow) -> Event:
    return EVENT_ADAPTER.validate_python(
        {
            "id": row.id,
            "session_id": row.session_id,
            "seq": row.seq,
            "type": row.type,
            "ts": row.ts,
            "actor": row.actor,
            "payload": row.payload,
            "causation_id": row.causation_id,
            "correlation_id": row.correlation_id,
        }
    )


class PostgresEventStore:
    """``EventStore`` on Postgres (SPEC §7.2 rules, §17).

    ``append`` runs one transaction:

    1. ``SELECT next_seq FROM session_counters WHERE session_id = :id FOR UPDATE``.
       The row lock serialises appenders of one session across processes and is held
       until commit, so commit order equals ``seq`` order.
    2. If there is no counter row, the draft must be ``session.created``: the store
       inserts the ``sessions`` row from the payload (and a ``users`` row for its
       ``user_id`` if missing) and the counter row. A second ``session.created`` for
       the same session raises ``ConflictError``.
    3. Insert the event, bump the counter, ``await read_model_update(event, tx)``
       with the same ``AsyncSession``, commit. Any exception rolls back everything,
       so a failed read-model update never leaves a gap or a half-applied event.
    """

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sessionmaker = sessionmaker

    async def append(
        self,
        session_id: str,
        draft: EventDraft | DraftFactory,
        read_model_update: ReadModelUpdate | None = None,
    ) -> Event:
        async with self._sessionmaker() as tx, tx.begin():
            next_seq = (
                await tx.execute(
                    select(SessionCounterRow.next_seq)
                    .where(SessionCounterRow.session_id == session_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            seq = 1 if next_seq is None else next_seq
            resolved = draft if isinstance(draft, EventDraft) else draft(seq)
            payload = resolved.payload

            if next_seq is None:
                if not isinstance(payload, SessionCreated):
                    raise NotFoundError(f"session {session_id} does not exist")
                await self._create_session(tx, session_id, payload)
            elif isinstance(payload, SessionCreated):
                raise ConflictError(f"session {session_id} was already created")

            event = build_event(resolved, session_id=session_id, seq=seq)
            if next_seq is not None:
                await tx.execute(
                    update(SessionCounterRow)
                    .where(SessionCounterRow.session_id == session_id)
                    .values(next_seq=seq + 1)
                )
            tx.add(_event_to_row(event))
            await tx.flush()
            if read_model_update is not None:
                await read_model_update(event, tx)
        return event

    async def _create_session(
        self, tx: AsyncSession, session_id: str, payload: SessionCreated
    ) -> None:
        if payload.session.id != session_id:
            raise InvalidRequestError(
                f"session.created payload is for session {payload.session.id}, not {session_id}"
            )
        await SessionRepository(tx).insert(payload.session, if_missing=True)
        try:
            async with tx.begin_nested():
                tx.add(SessionCounterRow(session_id=session_id, next_seq=2))
        except IntegrityError:
            # A concurrent appender created the session first.
            raise ConflictError(f"session {session_id} was already created") from None

    async def read(self, session_id: str, after_seq: int = 0, limit: int = 500) -> list[Event]:
        limit = max(1, min(limit, 5000))
        async with self._sessionmaker() as tx:
            rows = (
                await tx.scalars(
                    select(EventRow)
                    .where(EventRow.session_id == session_id, EventRow.seq > after_seq)
                    .order_by(EventRow.seq)
                    .limit(limit)
                )
            ).all()
        return [_row_to_event(row) for row in rows]

    async def last_seq(self, session_id: str) -> int:
        async with self._sessionmaker() as tx:
            next_seq = await tx.scalar(
                select(SessionCounterRow.next_seq).where(SessionCounterRow.session_id == session_id)
            )
        return 0 if next_seq is None else next_seq - 1
