"""WebSocket tickets: issue endpoint, single use, expiry, session binding, hash-only
storage, atomic consumption (in-memory always; Redis under the ``infra`` marker)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from datetime import timedelta

import pytest
from redis.asyncio import Redis

from agentplatform.api.app import create_app
from agentplatform.api.tickets import InMemoryTicketStore, RedisTicketStore, hash_ticket
from agentplatform.config import load_settings
from agentplatform.contracts.api import WS_TICKET_TTL_S
from agentplatform.contracts.common import new_id, utcnow

from .conftest import API_TOKEN, build_deps, client_for

TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")


async def _known(session_id: str) -> bool:
    return session_id == "s-1"


@pytest.mark.parametrize("token", [None, API_TOKEN])
async def test_ticket_endpoint_issues_a_fresh_ticket_with_or_without_api_token(
    token: str | None,
) -> None:
    tickets = InMemoryTicketStore()
    app = create_app(build_deps(token=token, tickets=tickets, session_exists=_known))
    async with client_for(app, token=token) as client:
        first = await client.post("/api/v1/sessions/s-1/ws-ticket")
        second = await client.post("/api/v1/sessions/s-1/ws-ticket", json={})
    assert first.status_code == second.status_code == 201
    assert first.headers["cache-control"] == "no-store"
    body = first.json()
    assert TICKET_RE.match(body["ticket"])
    assert body["ttl_s"] == WS_TICKET_TTL_S
    assert body["ticket"] != second.json()["ticket"]
    assert await tickets.consume(body["ticket"], "s-1")


async def test_ticket_endpoint_needs_bearer_and_a_known_session() -> None:
    app = create_app(build_deps(token=API_TOKEN, session_exists=_known))
    async with client_for(app) as anonymous:
        assert (await anonymous.post("/api/v1/sessions/s-1/ws-ticket")).status_code == 401
    async with client_for(app, token=API_TOKEN) as client:
        missing = await client.post("/api/v1/sessions/nope/ws-ticket")
        extra = await client.post("/api/v1/sessions/s-1/ws-ticket", json={"x": 1})
    assert missing.status_code == 404
    assert extra.status_code == 422


async def test_in_memory_ticket_is_single_use_bound_and_expiring() -> None:
    now = [0.0]
    store = InMemoryTicketStore(clock=lambda: now[0])
    issued = await store.issue("s-1")
    assert abs(issued.expires_at - (utcnow() + timedelta(seconds=60))) < timedelta(seconds=5)
    assert store.stored_keys == [hash_ticket(issued.ticket)]
    assert issued.ticket not in store.stored_keys
    assert await store.consume(issued.ticket, "s-1")
    assert not await store.consume(issued.ticket, "s-1")

    wrong = await store.issue("s-1")
    assert not await store.consume(wrong.ticket, "s-2")
    assert not await store.consume(wrong.ticket, "s-1")  # burned by the wrong session

    late = await store.issue("s-1")
    now[0] += WS_TICKET_TTL_S
    assert not await store.consume(late.ticket, "s-1")
    assert not await store.consume("", "s-1")


async def test_in_memory_concurrent_consumes_have_one_winner() -> None:
    store = InMemoryTicketStore()
    ticket = (await store.issue("s-1")).ticket
    results = await asyncio.gather(*(store.consume(ticket, "s-1") for _ in range(10)))
    assert results.count(True) == 1


# --- Redis --------------------------------------------------------------------------------


@pytest.fixture
async def redis_tickets() -> AsyncIterator[tuple[Redis, RedisTicketStore, str]]:
    client: Redis = Redis.from_url(load_settings().effective_redis_url, decode_responses=True)
    prefix = f"test-{new_id()}:ws-ticket"
    yield client, RedisTicketStore(client, prefix=prefix), prefix
    keys = [k async for k in client.scan_iter(match=f"{prefix}:*")]
    if keys:
        await client.delete(*keys)
    await client.aclose()


@pytest.mark.infra
async def test_redis_stores_only_the_hash_with_a_60s_ttl(
    redis_tickets: tuple[Redis, RedisTicketStore, str],
) -> None:
    client, store, prefix = redis_tickets
    issued = await store.issue("s-1")
    keys = [k async for k in client.scan_iter(match=f"{prefix}:*")]
    assert keys == [f"{prefix}:{hash_ticket(issued.ticket)}"]
    assert issued.ticket not in keys[0]
    assert await client.get(keys[0]) == "s-1"
    assert 0 < await client.ttl(keys[0]) <= WS_TICKET_TTL_S
    assert await store.ping()


@pytest.mark.infra
async def test_redis_consume_is_atomic_single_use_and_session_bound(
    redis_tickets: tuple[Redis, RedisTicketStore, str],
) -> None:
    client, store, prefix = redis_tickets
    ticket = (await store.issue("s-1")).ticket
    results = await asyncio.gather(*(store.consume(ticket, "s-1") for _ in range(10)))
    assert results.count(True) == 1
    assert not [k async for k in client.scan_iter(match=f"{prefix}:*")]

    wrong = (await store.issue("s-1")).ticket
    assert not await store.consume(wrong, "s-2")
    assert not await store.consume(wrong, "s-1")
    assert not await store.consume("never-issued-0000000000000000000000000", "s-1")


@pytest.mark.infra
async def test_redis_ticket_expires(
    redis_tickets: tuple[Redis, RedisTicketStore, str],
) -> None:
    client, _, prefix = redis_tickets
    short = RedisTicketStore(client, prefix=prefix, ttl_s=1)
    ticket = (await short.issue("s-1")).ticket
    await asyncio.sleep(1.2)
    assert not await short.consume(ticket, "s-1")
