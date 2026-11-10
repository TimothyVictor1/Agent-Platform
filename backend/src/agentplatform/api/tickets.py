"""Single-use WebSocket tickets (SPEC §16 "WebSocket auth").

A browser cannot set ``Authorization`` on a WebSocket and the API token must never
appear in a URL. So the client asks ``POST /sessions/{id}/ws-ticket`` (normal bearer
auth) for a ticket and connects with ``?ticket=<ticket>``.

* The ticket is ``secrets.token_urlsafe(32)`` (43 URL-safe characters, 256 bits).
* Only ``sha256(ticket)`` is stored, under ``<prefix>:<hex digest>`` with value
  ``session_id`` and a ``WS_TICKET_TTL_S`` (60 s) expiry, so a dump of Redis does not
  reveal usable tickets.
* ``consume`` removes the key with one atomic ``GETDEL``: of any number of concurrent
  handshakes presenting the same ticket, exactly one wins. A ticket presented for the
  wrong session is consumed and rejected.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol, runtime_checkable

from redis.asyncio import Redis

from agentplatform.contracts.api import WS_TICKET_TTL_S, WsTicket
from agentplatform.contracts.common import utcnow

__all__ = [
    "DEFAULT_TICKET_PREFIX",
    "InMemoryTicketStore",
    "RedisTicketStore",
    "TicketStore",
    "hash_ticket",
    "new_ticket",
]

DEFAULT_TICKET_PREFIX = "ws-ticket"
_MAX_TICKET_LEN = 128


def new_ticket() -> str:
    return secrets.token_urlsafe(32)


def hash_ticket(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


def _plausible(ticket: str) -> bool:
    return 0 < len(ticket) <= _MAX_TICKET_LEN


@runtime_checkable
class TicketStore(Protocol):
    async def issue(self, session_id: str) -> WsTicket:
        """Create a ticket for ``session_id``, valid ``WS_TICKET_TTL_S`` seconds."""
        ...

    async def consume(self, ticket: str, session_id: str) -> bool:
        """Atomically use up ``ticket``. True only if it existed, had not expired or
        been used, and was issued for ``session_id``."""
        ...

    async def ping(self) -> bool:
        """True if the backing store is reachable."""
        ...

    async def aclose(self) -> None: ...


class RedisTicketStore:
    def __init__(
        self,
        redis: Redis,
        *,
        prefix: str = DEFAULT_TICKET_PREFIX,
        ttl_s: int = WS_TICKET_TTL_S,
        owns_client: bool = False,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._ttl_s = ttl_s
        self._owns = owns_client

    @classmethod
    def from_url(
        cls, url: str, *, prefix: str = DEFAULT_TICKET_PREFIX, ttl_s: int = WS_TICKET_TTL_S
    ) -> RedisTicketStore:
        client: Redis = Redis.from_url(url, decode_responses=True)
        return cls(client, prefix=prefix, ttl_s=ttl_s, owns_client=True)

    def key(self, ticket: str) -> str:
        return f"{self._prefix}:{hash_ticket(ticket)}"

    async def issue(self, session_id: str) -> WsTicket:
        while True:
            ticket = new_ticket()
            if await self._redis.set(self.key(ticket), session_id, ex=self._ttl_s, nx=True):
                break
        return WsTicket(
            ticket=ticket,
            expires_at=utcnow() + timedelta(seconds=self._ttl_s),
            ttl_s=self._ttl_s,
        )

    async def consume(self, ticket: str, session_id: str) -> bool:
        if not _plausible(ticket):
            return False
        bound = await self._redis.getdel(self.key(ticket))
        if bound is None:
            return False
        value = bound if isinstance(bound, str) else bytes(bound).decode("utf-8", "replace")
        return hmac.compare_digest(value.encode(), session_id.encode())

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def aclose(self) -> None:
        if self._owns:
            await self._redis.aclose()


class InMemoryTicketStore:
    """Single-process ``TicketStore`` for tests and Redis-less development.

    ``clock`` returns monotonic seconds and can be replaced to test expiry.
    ``stored_keys`` exposes the stored hashes for assertions.
    """

    def __init__(
        self, *, ttl_s: int = WS_TICKET_TTL_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._tickets: dict[str, tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    @property
    def stored_keys(self) -> list[str]:
        return list(self._tickets)

    async def issue(self, session_id: str) -> WsTicket:
        ticket = new_ticket()
        async with self._lock:
            now = self._clock()
            self._tickets = {k: v for k, v in self._tickets.items() if v[1] > now}
            self._tickets[hash_ticket(ticket)] = (session_id, now + self._ttl_s)
        return WsTicket(
            ticket=ticket,
            expires_at=utcnow() + timedelta(seconds=self._ttl_s),
            ttl_s=self._ttl_s,
        )

    async def consume(self, ticket: str, session_id: str) -> bool:
        if not _plausible(ticket):
            return False
        async with self._lock:
            entry = self._tickets.pop(hash_ticket(ticket), None)
        if entry is None:
            return False
        bound, expires = entry
        if expires <= self._clock():
            return False
        return hmac.compare_digest(bound.encode(), session_id.encode())

    async def ping(self) -> bool:
        return True

    async def aclose(self) -> None:
        return None
