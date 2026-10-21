"""In-memory ``EventBus`` and ``LeaseStore`` with the same semantics as the Redis
implementations (single process only). See ``bus.base`` for the contract."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from agentplatform.bus.base import BusRecord, Delivery, LeaseInfo, QueueItem
from agentplatform.contracts.common import new_id
from agentplatform.contracts.events import EphemeralMessage, Event

__all__ = ["InMemoryBus", "InMemoryLeaseStore"]


@dataclass(slots=True)
class _InFlight:
    queue: str
    item: QueueItem
    attempt: int
    deadline: float


@dataclass(slots=True, eq=False)
class _Subscriber:
    queue: asyncio.Queue[BusRecord]
    include_ephemeral: bool


class InMemoryBus:
    """``EventBus`` for tests.

    ``visibility_timeout_s`` controls redelivery of unacked queue items; ``clock``
    (monotonic seconds) can be replaced to make timeouts deterministic. Retains up
    to ``max_stream_len`` events per session for ``subscribe(from_id=...)``.
    ``published`` and ``ephemeral`` keep everything that was sent, for assertions.
    """

    def __init__(
        self,
        *,
        visibility_timeout_s: float = 30.0,
        max_stream_len: int = 10_000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._visibility = visibility_timeout_s
        self._max_len = max_stream_len
        self._clock = clock
        self._streams: dict[str, deque[tuple[int, Event]]] = {}
        self._positions: dict[str, int] = {}
        self._subs: dict[str, set[_Subscriber]] = {}
        self._queues: dict[str, deque[tuple[str, QueueItem]]] = {}
        self._inflight: dict[str, _InFlight] = {}
        self._acked: set[str] = set()
        self._changed = asyncio.Condition()
        self.published: list[Event] = []
        self.ephemeral: list[EphemeralMessage] = []
        self.enqueued: list[tuple[str, QueueItem]] = []
        self.closed = False

    # -- session streams --------------------------------------------------------------

    async def publish(self, event: Event) -> str:
        pos = self._positions.get(event.session_id, 0) + 1
        self._positions[event.session_id] = pos
        stream = self._streams.setdefault(event.session_id, deque(maxlen=self._max_len))
        stream.append((pos, event))
        self.published.append(event)
        record = BusRecord(position=str(pos), item=event)
        for sub in self._subs.get(event.session_id, set()):
            sub.queue.put_nowait(record)
        return str(pos)

    async def publish_ephemeral(self, message: EphemeralMessage) -> None:
        self.ephemeral.append(message)
        record = BusRecord(position=None, item=message)
        for sub in self._subs.get(message.session_id, set()):
            if sub.include_ephemeral:
                sub.queue.put_nowait(record)

    async def subscribe(
        self,
        session_id: str,
        from_id: str | None = None,
        *,
        include_ephemeral: bool = True,
    ) -> AsyncIterator[BusRecord]:
        sub = _Subscriber(queue=asyncio.Queue(), include_ephemeral=include_ephemeral)
        self._subs.setdefault(session_id, set()).add(sub)
        try:
            last = 0
            if from_id is not None:
                last = int(from_id)
                for pos, event in list(self._streams.get(session_id, ())):
                    if pos > last:
                        last = pos
                        yield BusRecord(position=str(pos), item=event)
            else:
                last = self._positions.get(session_id, 0)
            while True:
                record = await sub.queue.get()
                if record.position is not None:
                    if int(record.position) <= last:
                        continue  # already replayed from history
                    last = int(record.position)
                yield record
        finally:
            self._subs.get(session_id, set()).discard(sub)

    # -- work queues ------------------------------------------------------------------

    async def enqueue(self, queue: str, item: QueueItem) -> str:
        delivery_id = new_id()
        self._queues.setdefault(queue, deque()).append((delivery_id, item))
        self.enqueued.append((queue, item))
        async with self._changed:
            self._changed.notify_all()
        return delivery_id

    def _take(self, queue: str) -> Delivery | None:
        now = self._clock()
        for delivery_id, flight in sorted(self._inflight.items()):
            if flight.queue == queue and flight.deadline <= now:
                flight.attempt += 1
                flight.deadline = now + self._visibility
                return Delivery(queue, delivery_id, flight.item, flight.attempt)
        pending = self._queues.get(queue)
        if pending:
            delivery_id, item = pending.popleft()
            self._inflight[delivery_id] = _InFlight(queue, item, 1, now + self._visibility)
            return Delivery(queue, delivery_id, item, 1)
        return None

    def _next_deadline(self, queue: str) -> float | None:
        deadlines = [f.deadline for f in self._inflight.values() if f.queue == queue]
        return min(deadlines) if deadlines else None

    async def dequeue(self, queue: str, consumer: str, *, timeout_s: float) -> Delivery | None:
        loop = asyncio.get_running_loop()
        end = loop.time() + timeout_s
        async with self._changed:
            while True:
                delivery = self._take(queue)
                if delivery is not None:
                    return delivery
                remaining = end - loop.time()
                if remaining <= 0:
                    return None
                wait = remaining
                deadline = self._next_deadline(queue)
                if deadline is not None:
                    wait = min(wait, max(0.0, deadline - self._clock()) + 0.001)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._changed.wait(), timeout=wait)

    async def ack(self, delivery: Delivery) -> None:
        self._inflight.pop(delivery.delivery_id, None)
        self._acked.add(delivery.delivery_id)

    def pending_count(self, queue: str) -> int:
        """Items waiting or in flight on ``queue`` (test helper)."""
        waiting = len(self._queues.get(queue, ()))
        return waiting + sum(1 for f in self._inflight.values() if f.queue == queue)

    async def aclose(self) -> None:
        self.closed = True


class InMemoryLeaseStore:
    """``LeaseStore`` for tests. ``clock`` returns Unix seconds (replaceable)."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._leases: dict[str, LeaseInfo] = {}

    def _now_ms(self) -> int:
        return int(self._clock() * 1000)

    async def acquire(self, key: str, owner: str, ttl_s: float) -> bool:
        now = self._now_ms()
        current = self._leases.get(key)
        if current is not None and current.expires_at_ms > now and current.owner != owner:
            return False
        self._leases[key] = LeaseInfo(key, owner, now + int(ttl_s * 1000))
        return True

    async def renew(self, key: str, owner: str, ttl_s: float) -> bool:
        now = self._now_ms()
        current = self._leases.get(key)
        if current is None or current.owner != owner or current.expires_at_ms <= now:
            return False
        self._leases[key] = LeaseInfo(key, owner, now + int(ttl_s * 1000))
        return True

    async def release(self, key: str, owner: str) -> bool:
        current = self._leases.get(key)
        if current is None or current.owner != owner:
            return False
        del self._leases[key]
        return True

    async def get(self, key: str) -> LeaseInfo | None:
        current = self._leases.get(key)
        if current is None or current.expires_at_ms <= self._now_ms():
            return None
        return current

    async def expired(self, prefix: str) -> list[LeaseInfo]:
        now = self._now_ms()
        return sorted(
            (
                lease
                for k, lease in self._leases.items()
                if k.startswith(prefix) and lease.expires_at_ms <= now
            ),
            key=lambda lease: lease.key,
        )

    async def forget(self, key: str) -> None:
        current = self._leases.get(key)
        if current is not None and current.expires_at_ms <= self._now_ms():
            del self._leases[key]
