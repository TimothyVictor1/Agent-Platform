"""Event bus and lease interfaces (SPEC §5.1, §7, §12).

The bus carries three kinds of traffic:

1. **Session stream** (fan-out, ordered): every persisted ``Event`` of a session is
   published after its transaction commits. The WebSocket gateway and runtime
   components subscribe. Delivery is best-effort live fan-out: the database is the
   source of truth, so a subscriber that may have missed something re-reads the
   ``EventStore`` (``read(session_id, after_seq)``) and de-duplicates by ``seq``.
2. **Ephemeral messages** (fan-out, not persisted): ``Heartbeat`` and ``Thumbnail``.
   Short retention; subscribers get them interleaved with events.
3. **Work queues** (point-to-point, at-least-once, acked): API→runtime commands and
   scheduler→agent assignments. A dequeued item that is not acked within the
   visibility timeout is delivered again (``Delivery.attempt`` increments).
   Consumers must be idempotent (de-duplicate by event id / command id).

Implementations: ``bus/redis_bus.py`` (T04) and
``agentplatform.testing.memory_bus.InMemoryBus``.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agentplatform.contracts.commands import COMMAND_ADAPTER, Command, CommandBase
from agentplatform.contracts.events import (
    EVENT_ADAPTER,
    EphemeralMessage,
    Event,
    EventEnvelope,
)

__all__ = [
    "NEW_SESSIONS_QUEUE",
    "BusRecord",
    "Delivery",
    "EventBus",
    "LeaseInfo",
    "LeaseStore",
    "QueueItem",
    "agent_queue",
    "decode_queue_item",
    "encode_queue_item",
    "session_command_queue",
]

NEW_SESSIONS_QUEUE = "sessions:new"
"""Queue of ``PlanSessionCommand``s for sessions no runtime owns yet."""


def agent_queue(agent_id: str) -> str:
    """Queue that delivers ``task.assigned`` events to exactly one agent (SPEC §12.4)."""
    return f"agent:{agent_id}"


def session_command_queue(session_id: str) -> str:
    """Queue of commands for a session, consumed by the runtime that owns it."""
    return f"session:{session_id}:commands"


QueueItem = Event | Command
"""What work queues carry: a command (API→runtime) or an event (e.g. ``task.assigned``)."""


def encode_queue_item(item: QueueItem) -> str:
    """Serialize a queue item to JSON text (``{"kind": ..., "data": ...}``)."""
    if isinstance(item, CommandBase):
        return '{"kind":"command","data":' + item.model_dump_json() + "}"
    assert isinstance(item, EventEnvelope)
    return '{"kind":"event","data":' + item.model_dump_json() + "}"


def decode_queue_item(text: str | bytes) -> QueueItem:
    """Inverse of ``encode_queue_item``. Raises ``ValueError`` (``json`` and pydantic
    errors included) for anything that is not a valid envelope, so callers can
    drop poison entries with one ``except ValueError``."""
    raw = json.loads(text)
    if not isinstance(raw, dict) or set(raw) != {"kind", "data"}:
        raise ValueError("queue item is not a {kind, data} envelope")
    kind = raw["kind"]
    if kind == "command":
        return COMMAND_ADAPTER.validate_python(raw["data"])
    if kind == "event":
        return EVENT_ADAPTER.validate_python(raw["data"])
    raise ValueError(f"unknown queue item kind {str(kind)[:40]!r}")


@dataclass(frozen=True, slots=True)
class BusRecord:
    """One item received from a session subscription.

    ``position`` is an opaque, increasing bus position for persisted events (pass
    it back as ``from_id`` to resume); it is ``None`` for ephemeral messages.
    """

    position: str | None
    item: Event | EphemeralMessage


@dataclass(frozen=True, slots=True)
class Delivery:
    """One dequeued work item. Pass it to ``EventBus.ack`` after handling it."""

    queue: str
    delivery_id: str
    item: QueueItem
    attempt: int
    """1 on first delivery, 2+ on redelivery after a visibility timeout."""


@runtime_checkable
class EventBus(Protocol):
    """Process-to-process messaging between the API and the runtime(s)."""

    async def publish(self, event: Event) -> str:
        """Append a persisted event to its session stream (call only after the event
        is committed to the EventStore). Returns the bus position. Events of one
        session must be published in ``seq`` order by a single writer at a time;
        the bus preserves publish order per session."""
        ...

    async def publish_ephemeral(self, message: EphemeralMessage) -> None:
        """Fan out a heartbeat or thumbnail to the session's subscribers. Never
        persisted; retention is short (implementation-defined, ~1000 per session)."""
        ...

    def subscribe(
        self,
        session_id: str,
        from_id: str | None = None,
        *,
        include_ephemeral: bool = True,
    ) -> AsyncIterator[BusRecord]:
        """Iterate records of one session, forever, until the consumer stops.

        ``from_id=None`` starts with the next published record. A position from a
        previous ``BusRecord`` resumes strictly after it (if still retained).
        Ephemeral messages are only delivered live (never replayed). Breaking out of
        the ``async for`` (or ``aclose()`` on the iterator) releases resources.
        """
        ...

    async def enqueue(self, queue: str, item: QueueItem) -> str:
        """Add an item to a work queue. Returns its delivery id."""
        ...

    async def dequeue(self, queue: str, consumer: str, *, timeout_s: float) -> Delivery | None:
        """Take the next item for ``consumer`` (a stable name such as the runtime or
        agent id), waiting up to ``timeout_s``. Returns ``None`` on timeout.

        Unacked items whose visibility timeout elapsed are redelivered (to any
        consumer of the queue) before new items, with ``attempt`` incremented.
        """
        ...

    async def ack(self, delivery: Delivery) -> None:
        """Mark a delivery as handled so it is never redelivered. Idempotent."""
        ...

    async def aclose(self) -> None:
        """Close connections. Idempotent."""
        ...


@dataclass(frozen=True, slots=True)
class LeaseInfo:
    key: str
    owner: str
    expires_at_ms: int
    """Unix time in milliseconds at which the lease expires/expired."""


@runtime_checkable
class LeaseStore(Protocol):
    """Expiring, owner-checked leases (task leases, session ownership).

    Implementations: ``bus/leases.py`` (Redis, T04) and
    ``agentplatform.testing.memory_bus.InMemoryLeaseStore``.
    """

    async def acquire(self, key: str, owner: str, ttl_s: float) -> bool:
        """Take the lease if it is free or expired. True if ``owner`` now holds it.
        Re-acquiring a lease you already hold renews it and returns True."""
        ...

    async def renew(self, key: str, owner: str, ttl_s: float) -> bool:
        """Extend the lease only if ``owner`` holds it and it has not expired."""
        ...

    async def release(self, key: str, owner: str) -> bool:
        """Release the lease only if ``owner`` holds it. True if released."""
        ...

    async def get(self, key: str) -> LeaseInfo | None:
        """Current holder of an unexpired lease, or ``None``."""
        ...

    async def expired(self, prefix: str) -> list[LeaseInfo]:
        """Leases under ``prefix`` that expired without being released. Each expired
        lease is reported until ``forget(key)`` is called or it is re-acquired."""
        ...

    async def forget(self, key: str) -> None:
        """Stop reporting an expired lease (after the caller handled it)."""
        ...
