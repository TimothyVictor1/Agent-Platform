"""``LeaseStore`` on Redis (SPEC §5.1 decision 3, §12). See ``bus.base.LeaseStore``.

Keys (``prefix`` defaults to ``agentplatform``):

* ``{prefix}:lease:{key}`` — the holder's owner string, ``SET ... PX ttl``. Redis
  expires it, so a holder that dies loses the lease without any cleanup.
* ``{prefix}:leases:expiry`` — sorted set ``key -> expires_at_ms``.
* ``{prefix}:leases:owner`` — hash ``key -> owner`` of the latest holder.

The two index keys outlive an expired lease so ``expired(prefix)`` can report it
(with its last owner) until ``forget`` or a new ``acquire``. Every operation is one
Lua script, so check-and-set is atomic, and all times come from the Redis server
clock (``TIME``), so hosts with skewed clocks agree on expiry.
"""

from __future__ import annotations

from typing import Any, cast

from redis.asyncio import Redis

from agentplatform.bus.base import LeaseInfo

__all__ = ["RedisLeaseStore"]

_NOW_MS = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
"""

# KEYS: lease, expiry index, owner index. ARGV: key, owner, ttl_ms.
_ACQUIRE = (
    _NOW_MS
    + """
local current = redis.call('GET', KEYS[1])
if current and current ~= ARGV[2] then
  return 0
end
local ttl = tonumber(ARGV[3])
redis.call('SET', KEYS[1], ARGV[2], 'PX', ttl)
redis.call('ZADD', KEYS[2], now + ttl, ARGV[1])
redis.call('HSET', KEYS[3], ARGV[1], ARGV[2])
return 1
"""
)

# Same KEYS/ARGV as acquire; only the live holder may renew.
_RENEW = (
    _NOW_MS
    + """
if redis.call('GET', KEYS[1]) ~= ARGV[2] then
  return 0
end
local ttl = tonumber(ARGV[3])
redis.call('PEXPIRE', KEYS[1], ttl)
redis.call('ZADD', KEYS[2], now + ttl, ARGV[1])
return 1
"""
)

# KEYS: lease, expiry index, owner index. ARGV: key, owner.
# The owner index decides, so the last holder can release an expired lease too.
_RELEASE = """
if redis.call('HGET', KEYS[3], ARGV[1]) ~= ARGV[2] then
  return 0
end
redis.call('DEL', KEYS[1])
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
return 1
"""

# KEYS: lease. Returns {owner, expires_at_ms} or nil.
_GET = (
    _NOW_MS
    + """
local owner = redis.call('GET', KEYS[1])
if not owner then
  return nil
end
local ttl = redis.call('PTTL', KEYS[1])
if ttl < 0 then
  return nil
end
return {owner, now + ttl}
"""
)

# KEYS: expiry index, owner index. ARGV: key prefix, lease key prefix.
# Returns a flat list key1, owner1, expires1, key2, ...
_EXPIRED = (
    _NOW_MS
    + """
local due = redis.call('ZRANGEBYSCORE', KEYS[1], '-inf', now, 'WITHSCORES')
local out = {}
local plen = string.len(ARGV[1])
for i = 1, #due, 2 do
  local key = due[i]
  if string.sub(key, 1, plen) == ARGV[1]
     and redis.call('EXISTS', ARGV[2] .. key) == 0 then
    table.insert(out, key)
    table.insert(out, redis.call('HGET', KEYS[2], key) or '')
    table.insert(out, due[i + 1])
  end
end
return out
"""
)

# KEYS: lease, expiry index, owner index. ARGV: key.
_FORGET = (
    _NOW_MS
    + """
local expires = redis.call('ZSCORE', KEYS[2], ARGV[1])
if not expires or tonumber(expires) > now or redis.call('EXISTS', KEYS[1]) == 1 then
  return 0
end
redis.call('ZREM', KEYS[2], ARGV[1])
redis.call('HDEL', KEYS[3], ARGV[1])
return 1
"""
)


def _ttl_ms(ttl_s: float) -> int:
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive")
    return max(1, int(ttl_s * 1000))


class RedisLeaseStore:
    """Expiring, owner-checked leases on Redis. ``redis`` needs ``decode_responses=True``."""

    def __init__(self, redis: Redis, *, prefix: str = "agentplatform") -> None:
        self._redis = redis
        self._lease_prefix = f"{prefix}:lease:"
        self._expiry_key = f"{prefix}:leases:expiry"
        self._owner_key = f"{prefix}:leases:owner"
        self._acquire = redis.register_script(_ACQUIRE)
        self._renew = redis.register_script(_RENEW)
        self._release = redis.register_script(_RELEASE)
        self._get = redis.register_script(_GET)
        self._expired = redis.register_script(_EXPIRED)
        self._forget = redis.register_script(_FORGET)

    def _keys(self, key: str) -> list[str]:
        return [self._lease_prefix + key, self._expiry_key, self._owner_key]

    async def acquire(self, key: str, owner: str, ttl_s: float) -> bool:
        result = await self._acquire(keys=self._keys(key), args=[key, owner, _ttl_ms(ttl_s)])
        return bool(result)

    async def renew(self, key: str, owner: str, ttl_s: float) -> bool:
        result = await self._renew(keys=self._keys(key), args=[key, owner, _ttl_ms(ttl_s)])
        return bool(result)

    async def release(self, key: str, owner: str) -> bool:
        result = await self._release(keys=self._keys(key), args=[key, owner])
        return bool(result)

    async def get(self, key: str) -> LeaseInfo | None:
        result = cast(list[Any] | None, await self._get(keys=[self._lease_prefix + key]))
        if not result:
            return None
        return LeaseInfo(key=key, owner=str(result[0]), expires_at_ms=int(result[1]))

    async def expired(self, prefix: str) -> list[LeaseInfo]:
        flat = cast(
            list[Any],
            await self._expired(
                keys=[self._expiry_key, self._owner_key], args=[prefix, self._lease_prefix]
            ),
        )
        leases = [
            LeaseInfo(
                key=str(flat[i]), owner=str(flat[i + 1]), expires_at_ms=int(float(flat[i + 2]))
            )
            for i in range(0, len(flat), 3)
        ]
        return sorted(leases, key=lambda lease: lease.key)

    async def forget(self, key: str) -> None:
        await self._forget(keys=self._keys(key), args=[key])
