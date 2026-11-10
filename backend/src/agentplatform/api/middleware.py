"""Pure-ASGI middleware: request id + access log, and optional bearer auth (SPEC §16).

Pure ASGI (not ``BaseHTTPMiddleware``) so streaming downloads and WebSockets pass
through untouched.

Access logs contain the path only, never the query string: WebSocket URLs carry
``?ticket=``. uvicorn's own access log is disabled in ``main.py`` for the same reason.
"""

from __future__ import annotations

import hmac
import re
import time
from collections.abc import Callable
from typing import Final

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentplatform.api.metrics import Metrics
from agentplatform.api.problems import problem_response
from agentplatform.contracts.common import new_id

__all__ = [
    "AUTH_EXEMPT_PATHS",
    "BearerAuthMiddleware",
    "BodySizeLimitMiddleware",
    "RequestContextMiddleware",
]

log = structlog.get_logger("agentplatform.api.access")

REQUEST_ID_HEADER: Final = "x-request-id"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

AUTH_EXEMPT_PATHS: Final = frozenset(
    {"/api/v1/health", "/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"}
)
"""Liveness and the (non-secret) API docs stay reachable without the token."""


class RequestContextMiddleware:
    """Assigns a request id (honouring a safe incoming ``X-Request-ID``), binds it to
    structlog context, echoes it in the response and logs one access line."""

    def __init__(self, app: ASGIApp, *, metrics: Callable[[], Metrics | None]) -> None:
        self.app = app
        self._metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER, "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else new_id()
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.perf_counter()
        status: list[int] = []

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status.append(int(message["status"]))
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                code = status[0] if status else (500 if scope["type"] == "http" else 0)
                if scope["type"] == "http":
                    metrics = self._metrics()
                    if metrics is not None:
                        metrics.inc(f"http.requests.{code // 100}xx")
                    log.info(
                        "request",
                        method=scope.get("method"),
                        path=scope.get("path"),
                        status=code,
                        duration_ms=round((time.perf_counter() - started) * 1000, 1),
                    )


class BearerAuthMiddleware:
    """When a token is configured, every HTTP request except ``AUTH_EXEMPT_PATHS`` and
    CORS preflights needs ``Authorization: Bearer <token>``. WebSockets authenticate
    with tickets (``api/ws.py``) instead."""

    def __init__(self, app: ASGIApp, *, token: Callable[[], str | None]) -> None:
        self.app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        expected = self._token()
        if expected is None or scope.get("path") in AUTH_EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if scope.get("method") == "OPTIONS" and "access-control-request-method" in headers:
            await self.app(scope, receive, send)  # CORS preflight carries no credentials
            return
        scheme, _, presented = headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(
            presented.strip().encode(), expected.encode()
        ):
            await self.app(scope, receive, send)
            return
        response = problem_response(
            401,
            code="unauthorized",
            detail="A valid bearer token is required.",
            instance=scope.get("path"),
            headers={"WWW-Authenticate": "Bearer"},
        )
        await response(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects request bodies above ``limit(method, path)`` bytes with 413, both via
    ``Content-Length`` and while a (chunked) body streams in, so an oversized upload
    is never fully buffered or spooled to disk. ``limit`` returns ``None`` for no limit.
    """

    def __init__(self, app: ASGIApp, *, limit: Callable[[str, str], int | None]) -> None:
        self.app = app
        self._limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit(str(scope.get("method", "")), str(scope.get("path", "")))
        if limit is None:
            await self.app(scope, receive, send)
            return
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return
        seen = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = problem_response(
            413,
            code="payload_too_large",
            detail=f"The request body exceeds {limit} bytes.",
            instance=scope.get("path"),
            headers={"Connection": "close"},
        )
        await response(scope, receive, send)
