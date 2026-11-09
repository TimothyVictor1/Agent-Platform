"""``EventBus`` on Redis Streams (SPEC §5.1, §7.2 rules, §12.4). See ``bus.base``.

Keys (``prefix`` defaults to ``agentplatform``):

* ``{prefix}:s:{session_id}:events`` — persisted events of a session, in publish
  (= ``seq``) order, ``MAXLEN ~ 10000``. The stream entry id is the ``BusRecord``
  position. Field ``e`` holds the event JSON.
* ``{prefix}:s:{session_id}:ephemeral`` — heartbeats and thumbnails,
  ``MAXLEN ~ 1000``, delivered live only. Field ``m``.
* ``{prefix}:q:{queue}`` — a work queue with one consumer group (``workers``). Field
  ``d`` holds ``encode_queue_item`` output. Delivery is at-least-once: an entry read
  with ``XREADGROUP`` stays pending until ``ack`` (``XACK`` + ``XDEL``); once it has
  been idle longer than the visibility timeout, ``XAUTOCLAIM`` hands it to the next
  ``dequeue`` call (any consumer) with ``attempt`` = Redis' delivery count.

Everything the bus carries is already validated contract JSON; nothing read from
Redis is evaluated or used to build commands. Poison entries (undecodable) on a
work queue are logged without their content and dropped, so they cannot wedge the
queue.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any, cast

import structlog
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from agentplatform.bus.base import (
    BusRecord,
    Delivery,
    QueueItem,
    decode_queue_item,
    encode_queue_item,
)
from agentplatform.contracts.events import (
    EPHEMERAL_ADAPTER,
    EVENT_ADAPTER,
    EphemeralMessage,
    Event,
)

__all__ = ["RedisEventBus"]

log = structlog.get_logger(__name__)

GROUP = "workers"
_EMPTY_STREAM_ID = "0-0"

_StreamEntries = list[tuple[str, dict[str, str]]]
_ReadResponse = list[tuple[str, _StreamEntries]] | None
"""Shape of ``XREAD``/``XREADGROUP`` replies with ``decode_responses=True``."""
_Streams = dict[Any, Any]


class RedisEventBus:
    """``EventBus`` backed by Redis Streams.

    ``redis`` must be created with ``decode_responses=True``; ``from_url`` does that
    and makes ``aclose`` close the client. ``claim_interval_s`` bounds how long a
    blocked ``dequeue`` waits before re-checking for timed-out deliveries;
    ``subscribe_block_s`` is the ``XREAD BLOCK`` slice of a subscription.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = "agentplatform",
        visibility_timeout_s: float = 30.0,
        event_maxlen: int = 10_000,
        ephemeral_maxlen: int = 1_000,
        claim_interval_s: float = 1.0,
        subscribe_block_s: float = 5.0,
        owns_client: bool = False,
    ) -> None:
        if visibility_timeout_s <= 0 or claim_interval_s <= 0 or subscribe_block_s <= 0:
            raise ValueError("timeouts must be positive")
        self._redis = redis
        self._prefix = prefix
        self._visibility_ms = max(1, int(visibility_timeout_s * 1000))
        self._event_maxlen = event_maxlen
        self._ephemeral_maxlen = ephemeral_maxlen
        self._claim_interval_s = claim_interval_s
        self._block_ms = max(1, int(subscribe_block_s * 1000))
        self._owns_client = owns_client
        self._groups: set[str] = set()
        self._closed = False

    @classmethod
    def from_url(cls, url: str, **kwargs: Any) -> RedisEventBus:
        client: Redis = Redis.from_url(url, decode_responses=True)
        return cls(client, owns_client=True, **kwargs)

    # -- keys -------------------------------------------------------------------------

    def events_key(self, session_id: str) -> str:
        return f"{self._prefix}:s:{session_id}:events"

    def ephemeral_key(self, session_id: str) -> str:
        return f"{self._prefix}:s:{session_id}:ephemeral"

    def queue_key(self, queue: str) -> str:
        return f"{self._prefix}:q:{queue}"

    # -- session streams --------------------------------------------------------------

    async def publish(self, event: Event) -> str:
        position = await self._redis.xadd(
            self.events_key(event.session_id),
            {"e": event.model_dump_json()},
            maxlen=self._event_maxlen,
            approximate=True,
        )
        return str(position)

    async def publish_ephemeral(self, message: EphemeralMessage) -> None:
        await self._redis.xadd(
            self.ephemeral_key(message.session_id),
            {"m": message.model_dump_json()},
            maxlen=self._ephemeral_maxlen,
            approximate=True,
        )

    async def _last_id(self, key: str) -> str:
        newest = await self._redis.xrevrange(key, "+", "-", count=1)
        return str(newest[0][0]) if newest else _EMPTY_STREAM_ID

    async def subscribe(
        self,
        session_id: str,
        from_id: str | None = None,
        *,
        include_ephemeral: bool = True,
    ) -> AsyncGenerator[BusRecord]:
        events_key = self.events_key(session_id)
        ephemeral_key = self.ephemeral_key(session_id)
        # Resolve "$" to concrete ids once, so nothing published between two XREAD
        # calls is skipped.
        cursors: _Streams = {
            events_key: from_id if from_id is not None else await self._last_id(events_key)
        }
        if include_ephemeral:
            cursors[ephemeral_key] = await self._last_id(ephemeral_key)
        while True:
            response = cast(_ReadResponse, await self._redis.xread(cursors, block=self._block_ms))
            for key, entries in response or []:
                for entry_id, fields in entries:
                    cursors[key] = entry_id
                    record = self._decode_record(key == events_key, entry_id, fields)
                    if record is not None:
                        yield record

    def _decode_record(
        self, is_event: bool, entry_id: str, fields: dict[str, str]
    ) -> BusRecord | None:
        try:
            if is_event:
                return BusRecord(position=entry_id, item=EVENT_ADAPTER.validate_json(fields["e"]))
            return BusRecord(position=None, item=EPHEMERAL_ADAPTER.validate_json(fields["m"]))
        except (KeyError, ValidationError):
            log.warning("bus_record_undecodable", entry_id=entry_id, is_event=is_event)
            return None

    # -- work queues ------------------------------------------------------------------

    async def _ensure_group(self, key: str) -> None:
        if key in self._groups:
            return
        try:
            # id "0": entries enqueued before the group existed are still delivered.
            await self._redis.xgroup_create(key, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups.add(key)

    async def enqueue(self, queue: str, item: QueueItem) -> str:
        key = self.queue_key(queue)
        await self._ensure_group(key)
        entry_id = await self._redis.xadd(key, {"d": encode_queue_item(item)})
        return str(entry_id)

    async def dequeue(self, queue: str, consumer: str, *, timeout_s: float) -> Delivery | None:
        key = self.queue_key(queue)
        await self._ensure_group(key)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        while True:
            delivery = await self._claim_expired(queue, key, consumer)
            if delivery is not None:
                return delivery
            remaining = deadline - loop.time()
            block_ms = int(min(remaining, self._claim_interval_s) * 1000)
            delivery = await self._read_new(queue, key, consumer, block_ms)
            if delivery is not None:
                return delivery
            if loop.time() >= deadline:
                return None

    async def _claim_expired(self, queue: str, key: str, consumer: str) -> Delivery | None:
        # XAUTOCLAIM scans a bounded slice of the pending list per call; follow its
        # cursor until it wraps to 0-0 so expired entries behind fresh ones are found.
        cursor = _EMPTY_STREAM_ID
        while True:
            response = await self._redis.xautoclaim(
                key, GROUP, consumer, min_idle_time=self._visibility_ms, start_id=cursor, count=1
            )
            cursor = str(response[0])
            claimed = cast(list[Any], response[1])
            if not claimed:
                if cursor == _EMPTY_STREAM_ID:
                    return None
                continue
            entry_id, fields = claimed[0]
            if fields is None:  # trimmed or deleted entry
                await self._redis.xack(key, GROUP, entry_id)
                continue
            pending = await self._redis.xpending_range(
                key, GROUP, min=entry_id, max=entry_id, count=1
            )
            attempt = int(pending[0]["times_delivered"]) if pending else 2
            delivery = await self._to_delivery(queue, key, entry_id, fields, attempt)
            if delivery is not None:
                return delivery

    async def _read_new(
        self, queue: str, key: str, consumer: str, block_ms: int
    ) -> Delivery | None:
        streams: _Streams = {key: ">"}
        # BLOCK 0 would wait forever; without BLOCK the read returns immediately.
        response = cast(
            _ReadResponse,
            await self._redis.xreadgroup(
                GROUP, consumer, streams, count=1, block=block_ms if block_ms > 0 else None
            ),
        )
        for _key, entries in response or []:
            for entry_id, fields in entries:
                return await self._to_delivery(queue, key, entry_id, fields, 1)
        return None

    async def _to_delivery(
        self, queue: str, key: str, entry_id: str, fields: dict[str, str], attempt: int
    ) -> Delivery | None:
        try:
            item = decode_queue_item(fields["d"])
        except (KeyError, TypeError, ValueError, ValidationError):
            log.warning("queue_item_undecodable_dropped", queue=queue, entry_id=entry_id)
            await self._drop(key, entry_id)
            return None
        return Delivery(queue=queue, delivery_id=entry_id, item=item, attempt=attempt)

    async def _drop(self, key: str, entry_id: str) -> None:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xack(key, GROUP, entry_id)
            pipe.xdel(key, entry_id)
            await pipe.execute()

    async def ack(self, delivery: Delivery) -> None:
        await self._drop(self.queue_key(delivery.queue), delivery.delivery_id)

    async def pending_count(self, queue: str) -> int:
        """Entries waiting or in flight on ``queue`` (diagnostics and tests)."""
        length = await self._redis.xlen(self.queue_key(queue))
        return int(length)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_client:
            await self._redis.aclose()
