"""WebSocket gateway: ticket auth, replay-then-live ordering, reconnects, thumbnails."""

from __future__ import annotations

import asyncio
from typing import Any

from agentplatform.api.app import create_app
from agentplatform.api.tickets import InMemoryTicketStore
from agentplatform.api.ws import (
    WS_CLOSE_BAD_REQUEST,
    WS_CLOSE_ORIGIN_REJECTED,
    WS_CLOSE_SESSION_NOT_FOUND,
)
from agentplatform.contracts.api import WS_CLOSE_TICKET_REJECTED
from agentplatform.contracts.common import Actor, utcnow
from agentplatform.contracts.events import Event, EventDraft, Heartbeat, SessionPaused, Thumbnail
from agentplatform.eventing import EventRecorder
from agentplatform.testing import InMemoryBus, InMemoryEventStore

from .conftest import API_TOKEN, AsgiWebSocket, build_deps

SID = "s-ws"


class RecordingStore(InMemoryEventStore):
    """Remembers the ``limit`` of every read so tests can check batching."""

    def __init__(self) -> None:
        super().__init__()
        self.read_limits: list[int] = []

    async def read(self, session_id: str, after_seq: int = 0, limit: int = 500) -> list[Event]:
        self.read_limits.append(limit)
        return await super().read(session_id, after_seq, limit)


class World:
    def __init__(self, *, token: str | None = None, check_s: float = 20.0) -> None:
        self.store = RecordingStore()
        self.bus = InMemoryBus()
        self.recorder = EventRecorder(self.store, self.bus)
        self.tickets = InMemoryTicketStore()
        self.sessions = {SID}
        self.deps = build_deps(
            token=token,
            events=self.store,
            bus=self.bus,
            tickets=self.tickets,
            session_exists=self._exists,
            recorder=self.recorder,
            ws_check_interval_s=check_s,
        )
        self.app = create_app(self.deps)

    async def _exists(self, session_id: str) -> bool:
        return session_id in self.sessions

    async def emit(self, n: int = 1) -> list[Event]:
        return [
            await self.recorder.record(
                SID, EventDraft(payload=SessionPaused(reason=f"e{i}"), actor=Actor.system())
            )
            for i in range(n)
        ]

    async def store_only(self, n: int = 1) -> None:
        """Persist without publishing (as if the bus lost the message)."""
        for i in range(n):
            await self.store.append(
                SID, EventDraft(payload=SessionPaused(reason=f"lost{i}"), actor=Actor.system())
            )

    def socket(
        self, session_id: str = SID, *, headers: dict[str, str] | None = None, **query: object
    ) -> AsgiWebSocket:
        return AsgiWebSocket(
            self.app, f"/api/v1/ws/sessions/{session_id}", headers=headers, **query
        )


async def receive_seqs(ws: AsgiWebSocket, n: int) -> list[int]:
    seqs: list[int] = []
    while len(seqs) < n:
        msg = await ws.receive_json()
        assert msg["type"] == "event"
        seqs.append(msg["event"]["seq"])
    return seqs


async def assert_quiet(ws: AsgiWebSocket, wait_s: float = 0.1) -> None:
    try:
        msg = await ws.receive(wait_s=wait_s)
    except TimeoutError:
        return
    raise AssertionError(f"unexpected message {msg}")


# --- ticket auth ------------------------------------------------------------------------


async def test_token_set_requires_a_ticket() -> None:
    w = World(token=API_TOKEN)
    async with w.socket() as ws:
        assert ws.accepted, "accept first so the browser sees the 4401 code"
        assert await ws.expect_close() == WS_CLOSE_TICKET_REJECTED


async def test_valid_ticket_connects_once_then_is_rejected() -> None:
    w = World(token=API_TOKEN)
    await w.emit(2)
    ticket = (await w.tickets.issue(SID)).ticket
    async with w.socket(ticket=ticket) as ws:
        assert await receive_seqs(ws, 2) == [1, 2]
    async with w.socket(ticket=ticket) as ws:
        assert await ws.expect_close() == WS_CLOSE_TICKET_REJECTED


async def test_expired_and_wrong_session_tickets_are_rejected() -> None:
    now = [1000.0]
    w = World(token=API_TOKEN)
    w.tickets = InMemoryTicketStore(clock=lambda: now[0])
    w.deps.tickets = w.tickets
    expired = (await w.tickets.issue(SID)).ticket
    now[0] += 61
    async with w.socket(ticket=expired) as ws:
        assert await ws.expect_close() == WS_CLOSE_TICKET_REJECTED

    w.sessions.add("other")
    foreign = (await w.tickets.issue("other")).ticket
    async with w.socket(ticket=foreign) as ws:
        assert await ws.expect_close() == WS_CLOSE_TICKET_REJECTED
    # the wrong-session attempt consumed it
    assert not await w.tickets.consume(foreign, "other")


async def test_garbage_ticket_is_rejected() -> None:
    w = World(token=API_TOKEN)
    async with w.socket(ticket="x" * 500) as ws:
        assert await ws.expect_close() == WS_CLOSE_TICKET_REJECTED


async def test_without_token_no_ticket_is_needed_but_a_bad_one_is_rejected() -> None:
    w = World(token=None)
    await w.emit(1)
    async with w.socket() as ws:
        assert await receive_seqs(ws, 1) == [1]
    async with w.socket(ticket="not-a-real-ticket-0000000000000000000") as ws:
        assert await ws.expect_close() == WS_CLOSE_TICKET_REJECTED
    ticket = (await w.tickets.issue(SID)).ticket
    async with w.socket(ticket=ticket) as ws:
        assert await receive_seqs(ws, 1) == [1]


async def test_bad_since_and_unknown_session_are_terminal_codes() -> None:
    w = World()
    async with w.socket(since="-1") as ws:
        assert await ws.expect_close() == WS_CLOSE_BAD_REQUEST
    async with w.socket(since="abc") as ws:
        assert await ws.expect_close() == WS_CLOSE_BAD_REQUEST
    async with w.socket("missing") as ws:
        assert await ws.expect_close() == WS_CLOSE_SESSION_NOT_FOUND


async def test_foreign_origin_is_rejected_even_without_token() -> None:
    w = World(token=None)
    await w.emit(1)
    async with w.socket(headers={"Origin": "http://evil.example"}) as ws:
        assert await ws.expect_close() == WS_CLOSE_ORIGIN_REJECTED
    async with w.socket(headers={"Origin": "http://localhost:3000"}) as ws:
        assert await receive_seqs(ws, 1) == [1]


# --- replay then live -------------------------------------------------------------------


async def test_replay_after_since_in_batches_then_live_in_order() -> None:
    w = World()
    await w.emit(1200)
    async with w.socket(since=100) as ws:
        replayed = await receive_seqs(ws, 1100)
        assert replayed == list(range(101, 1201))
        assert w.store.read_limits
        assert max(w.store.read_limits) == 500
        assert len(w.store.read_limits) >= 3  # 101..600, 601..1100, 1101..1200
        await w.emit(3)
        assert await receive_seqs(ws, 3) == [1201, 1202, 1203]
        await assert_quiet(ws)


async def test_events_published_during_replay_are_neither_lost_nor_duplicated() -> None:
    w = World()
    await w.emit(600)
    async with w.socket(since=0) as ws:
        first = await receive_seqs(ws, 10)
        await w.emit(50)  # published while the client is still draining the replay
        rest = await receive_seqs(ws, 640)
        assert first + rest == list(range(1, 651))
        await assert_quiet(ws)


async def test_reconnect_with_since_is_gap_free() -> None:
    w = World()
    await w.emit(5)
    async with w.socket(since=0) as ws:
        seen = await receive_seqs(ws, 5)
    await w.emit(4)  # while disconnected
    async with w.socket(since=seen[-1]) as ws:
        seen += await receive_seqs(ws, 4)
        await w.emit(1)
        seen += await receive_seqs(ws, 1)
    assert seen == list(range(1, 11))


async def test_gap_in_live_stream_is_filled_from_the_store() -> None:
    w = World()
    async with w.socket(since=0) as ws:
        await assert_quiet(ws, 0.05)
        await w.store_only(2)  # seq 1, 2 never reach the bus
        await w.emit(1)  # seq 3 arrives live: gap detected
        assert await receive_seqs(ws, 3) == [1, 2, 3]
        await assert_quiet(ws)


async def test_missed_event_is_delivered_by_the_periodic_catch_up() -> None:
    w = World(check_s=0.05)
    async with w.socket(since=0) as ws:
        await w.store_only(1)  # no later event will reveal the gap
        assert await receive_seqs(ws, 1) == [1]


async def test_duplicate_live_events_are_skipped() -> None:
    w = World()
    events = await w.emit(2)
    async with w.socket(since=0) as ws:
        assert await receive_seqs(ws, 2) == [1, 2]
        await w.bus.publish(events[0])  # redelivered duplicate
        await w.emit(1)
        assert await receive_seqs(ws, 1) == [3]


# --- ephemeral and lifecycle --------------------------------------------------------------


async def test_thumbnails_are_forwarded_and_heartbeats_dropped() -> None:
    w = World()
    async with w.socket() as ws:
        await asyncio.sleep(0.01)
        await w.bus.publish_ephemeral(
            Heartbeat(session_id=SID, agent_id="a-1", step=3, ts=utcnow())
        )
        await w.bus.publish_ephemeral(
            Thumbnail(session_id=SID, agent_id="a-1", step=4, jpeg_b64="AAAA", ts=utcnow())
        )
        msg: dict[str, Any] = await ws.receive_json()
        assert msg == {"type": "thumbnail", "agent_id": "a-1", "step": 4, "jpeg_b64": "AAAA"}
        await assert_quiet(ws)


async def test_socket_closes_when_the_session_is_deleted() -> None:
    w = World(check_s=0.05)
    async with w.socket() as ws:
        await assert_quiet(ws, 0.08)
        w.sessions.discard(SID)
        assert await ws.expect_close(wait_s=2) == WS_CLOSE_SESSION_NOT_FOUND


async def test_client_disconnect_releases_the_subscription() -> None:
    w = World()
    async with w.socket():
        await asyncio.sleep(0.01)
        assert w.bus._subs.get(SID)
    await asyncio.sleep(0.01)
    assert not w.bus._subs.get(SID)
    assert w.deps.metrics.get("ws.connections") == 1
