"""FastAPI application factory (SPEC §16).

``create_app(deps)`` serves the given dependencies (tests); ``create_app()`` builds the
production ones from ``Settings`` in the lifespan and closes them on shutdown.

Middleware, outermost first: request id + access log → CORS (``FRONTEND_ORIGIN``
only) → origin check for unsafe methods → bearer auth (when ``APP_API_TOKEN`` is
set) → upload size limit → routes.
CORS sits outside auth so preflights and 401s carry CORS headers.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agentplatform.api import ws
from agentplatform.api.deps import ApiDeps, build_deps
from agentplatform.api.metrics import Metrics
from agentplatform.api.middleware import (
    BearerAuthMiddleware,
    BodySizeLimitMiddleware,
    OriginCheckMiddleware,
    RequestContextMiddleware,
)
from agentplatform.api.problems import install_problem_handlers
from agentplatform.api.routes import agents, approvals, artifacts, board, replay, sessions, system
from agentplatform.config import Settings, load_settings

__all__ = ["API_PREFIX", "create_app"]

API_PREFIX = "/api/v1"
UPLOAD_OVERHEAD_BYTES = 1024 * 1024
"""Allowance for multipart framing and the description field on top of the file."""

_UPLOAD_PATH = re.compile(r"^/api/v1/sessions/[^/]+/artifacts$")


def _deps(app: FastAPI) -> ApiDeps | None:
    deps: ApiDeps | None = getattr(app.state, "deps", None)
    return deps


def create_app(deps: ApiDeps | None = None, *, settings: Settings | None = None) -> FastAPI:
    resolved = deps.settings if deps is not None else (settings or load_settings())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if deps is not None:
            yield  # the caller owns (and closes) injected dependencies
            return
        built = await build_deps(resolved)
        app.state.deps = built
        try:
            yield
        finally:
            await built.aclose()

    app = FastAPI(
        title="Agent Platform API",
        version=system.api_version(),
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
        redoc_url=None,
    )
    if deps is not None:
        app.state.deps = deps
    install_problem_handlers(app)

    for module in (sessions, agents, board, approvals, artifacts, replay, system):
        app.include_router(module.router, prefix=API_PREFIX)
    app.include_router(ws.router, prefix=API_PREFIX)

    def token() -> str | None:
        current = _deps(app)
        return current.api_token if current is not None else None

    def upload_limit(method: str, path: str) -> int | None:
        current = _deps(app)
        if current is None or method != "POST" or not _UPLOAD_PATH.match(path):
            return None
        return current.max_upload_bytes + UPLOAD_OVERHEAD_BYTES

    def metrics() -> Metrics | None:
        current = _deps(app)
        return current.metrics if current is not None else None

    # add_middleware: the last one added is the outermost.
    app.add_middleware(BodySizeLimitMiddleware, limit=upload_limit)
    app.add_middleware(BearerAuthMiddleware, token=token)
    origin = resolved.frontend_origin.strip().rstrip("/")
    app.add_middleware(
        OriginCheckMiddleware, allowed=frozenset({origin}) if origin else frozenset()
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[resolved.frontend_origin] if resolved.frontend_origin else [],
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "If-None-Match"],
        expose_headers=["X-Request-ID", "ETag", "Content-Disposition"],
        max_age=600,
    )
    app.add_middleware(RequestContextMiddleware, metrics=metrics)
    return app
