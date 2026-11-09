"""Fixtures for Redis-backed tests (marker ``infra``; needs ``make db-up``).

Every test gets its own key prefix and deletes its keys afterwards, so tests never
see each other's (or a running app's) streams.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from agentplatform.config import load_settings
from agentplatform.contracts.common import new_id


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client: Redis = Redis.from_url(load_settings().effective_redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
async def prefix(redis: Redis) -> AsyncIterator[str]:
    value = f"test-{new_id()}"
    yield value
    keys = [key async for key in redis.scan_iter(match=f"{value}:*")]
    if keys:
        await redis.delete(*keys)
