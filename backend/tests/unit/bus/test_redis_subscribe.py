"""First light: ``scheduler_feeder_exited`` right after planning.

redis-py 8 gives every client a 5 s socket timeout and the bus used ``XREAD BLOCK
5000``, so a stream that stayed quiet for one block (a planner call) raised
``TimeoutError`` out of ``subscribe`` and ended the scheduler's and supervisor's feed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from agentplatform.bus import redis_bus
from agentplatform.bus.redis_bus import RedisEventBus
from agentplatform.contracts.common import Actor
from agentplatform.contracts.events import EventDraft, SessionPaused, build_event


def _event_json(seq: int) -> str:
    draft = EventDraft(payload=SessionPaused(), actor=Actor.system())
    return build_event(draft, session_id="s1", seq=seq).model_dump_json()


class FlakyRedis:
    """``xread`` raises the scripted errors first, then returns one entry per call."""

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        self.cursors: list[dict[str, str]] = []
        self.seq = 0

    async def xrevrange(self, key: str, *args: Any, **kwargs: Any) -> list[Any]:
        return [("5-0", {})]

    async def xread(self, streams: dict[str, str], block: int) -> list[Any]:
        self.cursors.append(dict(streams))
        if self.errors:
            raise self.errors.pop(0)
        self.seq += 1
        key = next(iter(streams))
        return [(key, [(f"{5 + self.seq}-0", {"e": _event_json(self.seq)})])]


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_bus, "_SUBSCRIBE_RETRY_S", (0.0,))


async def test_subscribe_survives_socket_timeouts_and_reconnects() -> None:
    fake = FlakyRedis(
        [RedisTimeoutError("Timeout reading from redis:6379"), RedisConnectionError("reset")]
    )
    bus = RedisEventBus(fake)  # type: ignore[arg-type]
    stream = bus.subscribe("s1", include_ephemeral=False)
    first = await asyncio.wait_for(anext(stream), 1)
    second = await asyncio.wait_for(anext(stream), 1)
    await stream.aclose()
    assert [first.position, second.position] == ["6-0", "7-0"]
    key = bus.events_key("s1")
    # the failed reads kept the cursor, so nothing was skipped or read twice
    assert [c[key] for c in fake.cursors] == ["5-0", "5-0", "5-0", "6-0"]


def test_from_url_socket_timeout_outlasts_the_block() -> None:
    bus = RedisEventBus.from_url("redis://localhost:1/0", subscribe_block_s=5.0)
    kwargs = bus._redis.connection_pool.connection_kwargs
    assert kwargs["socket_timeout"] > 5.0
