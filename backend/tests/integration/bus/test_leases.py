"""RedisLeaseStore: owner-checked, expiring leases with expiry reporting (SPEC §12)."""

from __future__ import annotations

import asyncio
import time

import pytest
from redis.asyncio import Redis

from agentplatform.bus.base import LeaseStore
from agentplatform.bus.leases import RedisLeaseStore

pytestmark = pytest.mark.infra

SHORT = 0.25


@pytest.fixture
def leases(redis: Redis, prefix: str) -> RedisLeaseStore:
    return RedisLeaseStore(redis, prefix=prefix)


def test_implements_protocol(leases: RedisLeaseStore) -> None:
    assert isinstance(leases, LeaseStore)


async def test_acquire_is_exclusive_and_owner_checked(leases: RedisLeaseStore) -> None:
    before_ms = int(time.time() * 1000)
    assert await leases.acquire("task:T1", "agent-a", 30)
    assert not await leases.acquire("task:T1", "agent-b", 30)
    assert await leases.acquire("task:T1", "agent-a", 60)  # re-acquire renews

    info = await leases.get("task:T1")
    assert info is not None
    assert (info.key, info.owner) == ("task:T1", "agent-a")
    # Redis server clock vs. local clock: allow a little skew.
    assert before_ms + 55_000 <= info.expires_at_ms <= before_ms + 65_000

    assert not await leases.renew("task:T1", "agent-b", 30)
    assert await leases.renew("task:T1", "agent-a", 30)
    assert not await leases.release("task:T1", "agent-b")
    assert await leases.release("task:T1", "agent-a")
    assert not await leases.release("task:T1", "agent-a")
    assert await leases.get("task:T1") is None
    assert await leases.acquire("task:T1", "agent-b", 30)
    assert await leases.expired("task:") == []


async def test_expired_lease_is_reported_until_forgotten(leases: RedisLeaseStore) -> None:
    assert await leases.acquire("task:T1", "agent-a", SHORT)
    assert await leases.acquire("task:T2", "agent-b", 30)
    assert await leases.acquire("session:S1", "runtime-1", SHORT)
    assert await leases.expired("task:") == []

    await asyncio.sleep(SHORT + 0.1)

    assert await leases.get("task:T1") is None
    assert not await leases.renew("task:T1", "agent-a", 30)
    expired = await leases.expired("task:")
    assert [(e.key, e.owner) for e in expired] == [("task:T1", "agent-a")]
    assert expired[0].expires_at_ms <= int(time.time() * 1000) + 1000
    # Still reported on the next scan; prefixes are respected.
    assert [e.key for e in await leases.expired("task:")] == ["task:T1"]
    assert [e.key for e in await leases.expired("")] == ["session:S1", "task:T1"]

    await leases.forget("task:T1")
    assert await leases.expired("task:") == []
    await leases.forget("task:T2")  # live lease: no effect
    info = await leases.get("task:T2")
    assert info is not None
    assert info.owner == "agent-b"


async def test_reacquiring_an_expired_lease_clears_the_report(leases: RedisLeaseStore) -> None:
    assert await leases.acquire("task:T1", "agent-a", SHORT)
    await asyncio.sleep(SHORT + 0.1)
    assert await leases.acquire("task:T1", "agent-b", 30)
    assert await leases.expired("task:") == []
    assert not await leases.release("task:T1", "agent-a")  # no longer the holder
    info = await leases.get("task:T1")
    assert info is not None
    assert info.owner == "agent-b"


async def test_last_owner_can_release_an_expired_lease(leases: RedisLeaseStore) -> None:
    assert await leases.acquire("task:T1", "agent-a", SHORT)
    await asyncio.sleep(SHORT + 0.1)
    assert await leases.release("task:T1", "agent-a")
    assert await leases.expired("task:") == []


async def test_concurrent_acquire_has_one_winner(leases: RedisLeaseStore) -> None:
    results = await asyncio.gather(*(leases.acquire("task:T9", f"o{i}", 30) for i in range(20)))
    assert results.count(True) == 1


async def test_rejects_non_positive_ttl(leases: RedisLeaseStore) -> None:
    with pytest.raises(ValueError, match="positive"):
        await leases.acquire("k", "o", 0)
