"""WebSocket gateway ``/api/v1/ws/sessions/{id}?ticket=&since=`` (SPEC §7.2, §16).

Handshake (the socket is accepted first so the client sees the 4xxx close code; a
close before accept becomes an HTTP 403 and the browser only reports 1006):

0. ``Origin``: a browser connection from anywhere but ``FRONTEND_ORIGIN`` closes with
   ``4403`` (terminal). Clients without ``Origin`` are not affected.
1. Ticket: a presented ticket must be consumed successfully (single use, 60 s, bound
   to this session), otherwise close ``4401``. Without a ticket the connection is
   allowed only when ``APP_API_TOKEN`` is unset (local development).
2. ``since``: optional integer ≥ 0, otherwise close ``4400`` (terminal).
3. The session must exist, otherwise close ``4404`` (terminal).

Streaming, gap-free and duplicate-free by ``seq``:

* A pump task iterates the bus subscription into a bounded queue. It is started
  before the replay, so events published during the replay are buffered.
* Replay: persisted events with ``seq > since`` in batches of ``ws_replay_batch``
  (500) until a short batch.
* Live: an event with ``seq <= last_sent`` is a duplicate and skipped; ``seq ==
  last_sent + 1`` is sent; a larger ``seq`` means something was missed (bus
  hiccup, queue overflow, or an event committed between the replay and the
  subscription becoming active) and triggers a catch-up read from the store, which
  is the source of truth (``bus.base``). A catch-up also runs ``settle_s`` after
  connecting and on every periodic tick, so a missed event is delivered even when
  no later event arrives.
* Thumbnails (bus-only) are forwarded as ``WsThumbnailMessage``; heartbeats are not
  part of ``WsServerMessage`` and are dropped.
* Every ``ws_check_interval_s`` (20 s) the gateway checks that the session still
  exists and closes with ``4404`` once it is gone. Transport-level ping/pong every
  20 s is done by uvicorn (``ws_ping_interval``/``ws_ping_timeout`` in ``main.py``).
* Unexpected server errors close with ``1011`` (the client reconnects).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Final

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from agentplatform.api.deps import ApiDeps
from agentplatform.api.middleware import origin_allowed
from agentplatform.bus.base import BusRecord
from agentplatform.contracts.api import (
    WS_CLOSE_TICKET_REJECTED,
    WsEventMessage,
    WsThumbnailMessage,
)
from agentplatform.contracts.events import EventEnvelope, Thumbnail

__all__ = [
    "WS_CLOSE_BAD_REQUEST",
    "WS_CLOSE_ORIGIN_REJECTED",
    "WS_CLOSE_SESSION_NOT_FOUND",
    "WS_CLOSE_TICKET_REJECTED",
    "SessionStream",
    "router",
]

log = structlog.get_logger(__name__)

WS_CLOSE_BAD_REQUEST: Final = 4400
WS_CLOSE_ORIGIN_REJECTED: Final = 4403
WS_CLOSE_SESSION_NOT_FOUND: Final = 4404
WS_CLOSE_INTERNAL: Final = 1011
LIVE_QUEUE_MAX: Final = 10_000

router = APIRouter()


class _ClientGone(Exception):
    pass


class SessionStream:
    """Streams one session's events to one connected socket."""

    def __init__(
        self,
        deps: ApiDeps,
        websocket: WebSocket,
        session_id: str,
        since: int,
        *,
        settle_s: float = 1.0,
    ) -> None:
        self._deps = deps
        self._ws = websocket
        self._session_id = session_id
        self.last_sent = since
        self._settle_s = min(settle_s, deps.ws_check_interval_s)
        self._queue: asyncio.Queue[BusRecord] = asyncio.Queue(maxsize=LIVE_QUEUE_MAX)

    async def _send_text(self, text: str) -> None:
        try:
            await self._ws.send_text(text)
        except (WebSocketDisconnect, RuntimeError, OSError) as exc:
            raise _ClientGone from exc

    async def _send_event(self, event: EventEnvelope) -> None:
        message = WsEventMessage.model_validate({"type": "event", "event": event})
        await self._send_text(message.model_dump_json())
        self.last_sent = event.seq

    async def _pump(self, subscription: AsyncIterator[BusRecord]) -> None:
        try:
            async for record in subscription:
                try:
                    self._queue.put_nowait(record)
                except asyncio.QueueFull:
                    # Dropped events are recovered from the store via gap detection.
                    self._deps.metrics.inc("ws.live_dropped")
        finally:
            aclose = getattr(subscription, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

    async def catch_up(self) -> None:
        """Send every persisted event after ``last_sent``, in batches."""
        batch = max(1, self._deps.ws_replay_batch)
        while True:
            events = await self._deps.events.read(self._session_id, self.last_sent, limit=batch)
            for event in events:
                if event.seq > self.last_sent:
                    await self._send_event(event)
            if len(events) < batch:
                return

    async def _handle(self, record: BusRecord) -> None:
        item = record.item
        if isinstance(item, EventEnvelope):
            if item.session_id != self._session_id or item.seq <= self.last_sent:
                return
            if item.seq == self.last_sent + 1:
                await self._send_event(item)
            else:
                self._deps.metrics.inc("ws.gap_catch_up")
                await self.catch_up()
        elif isinstance(item, Thumbnail):
            message = WsThumbnailMessage(
                agent_id=item.agent_id, step=item.step, jpeg_b64=item.jpeg_b64
            )
            await self._send_text(message.model_dump_json())

    async def _tick(self) -> bool:
        """Periodic check. Returns False when the session no longer exists."""
        if not await self._deps.session_exists(self._session_id):
            return False
        await self.catch_up()
        return True

    async def run(self) -> int | None:
        """Stream until the client leaves (returns ``None``) or the session is gone
        (returns the close code to use)."""
        subscription = self._deps.bus.subscribe(self._session_id, include_ephemeral=True)
        pump = asyncio.create_task(self._pump(subscription), name=f"ws-pump-{self._session_id}")
        receiver = asyncio.create_task(self._drain_client(), name="ws-receive")
        await asyncio.sleep(0)  # let the pump register its subscription
        loop = asyncio.get_running_loop()
        next_tick = loop.time() + self._settle_s
        getter: asyncio.Task[BusRecord] | None = None
        try:
            await self.catch_up()
            while True:
                if receiver.done():
                    return None
                if pump.done():
                    pump.result()  # re-raise a bus failure
                    raise RuntimeError("bus subscription ended")
                if getter is None:
                    getter = asyncio.create_task(self._queue.get())
                timeout = max(0.0, next_tick - loop.time())
                done, _ = await asyncio.wait(
                    {getter, receiver, pump}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
                )
                if getter in done:
                    record = getter.result()
                    getter = None
                    await self._handle(record)
                if loop.time() >= next_tick:
                    next_tick = loop.time() + self._deps.ws_check_interval_s
                    if not await self._tick():
                        return WS_CLOSE_SESSION_NOT_FOUND
        except _ClientGone:
            return None
        finally:
            for task in (getter, receiver, pump):
                if task is not None:
                    task.cancel()
            for task in (getter, receiver, pump):
                if task is not None:
                    with contextlib.suppress(BaseException):
                        await task

    async def _drain_client(self) -> None:
        """Read (and ignore) client messages until it disconnects."""
        while True:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                return


def _parse_since(raw: str | None) -> int | None:
    if raw is None or raw == "":
        return 0
    if not raw.isdigit() or len(raw) > 18:
        return None
    return int(raw)


async def _close(websocket: WebSocket, code: int, reason: str = "") -> None:
    with contextlib.suppress(Exception):
        await websocket.close(code=code, reason=reason)


@router.websocket("/ws/sessions/{session_id}")
async def session_socket(websocket: WebSocket, session_id: str) -> None:
    deps: ApiDeps = websocket.app.state.deps
    ticket = websocket.query_params.get("ticket")
    since = _parse_since(websocket.query_params.get("since"))
    await websocket.accept()

    if not origin_allowed(websocket.headers.get("origin"), deps.allowed_origins):
        # Cross-site WebSocket hijacking: a foreign page must never read the stream.
        deps.metrics.inc("ws.rejected.origin")
        await _close(websocket, WS_CLOSE_ORIGIN_REJECTED, "origin not allowed")
        return
    if ticket is not None:
        allowed = await deps.tickets.consume(ticket, session_id)
    else:
        allowed = deps.api_token is None
    if not allowed:
        deps.metrics.inc("ws.rejected.ticket")
        await _close(websocket, WS_CLOSE_TICKET_REJECTED, "ticket rejected")
        return
    if since is None:
        deps.metrics.inc("ws.rejected.bad_request")
        await _close(websocket, WS_CLOSE_BAD_REQUEST, "since must be an integer >= 0")
        return
    if not await deps.session_exists(session_id):
        deps.metrics.inc("ws.rejected.not_found")
        await _close(websocket, WS_CLOSE_SESSION_NOT_FOUND, "session not found")
        return

    deps.metrics.inc("ws.connections")
    with structlog.contextvars.bound_contextvars(session_id=session_id):
        log.info("ws_connected", since=since)
        try:
            code = await SessionStream(deps, websocket, session_id, since).run()
        except Exception as exc:
            log.error("ws_failed", error_type=type(exc).__name__)
            await _close(websocket, WS_CLOSE_INTERNAL, "internal error")
            return
        if code is not None:
            await _close(websocket, code, "session not found")
        log.info("ws_closed", code=code)
