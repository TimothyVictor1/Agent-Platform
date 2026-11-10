"""Fixtures for the API tests.

* Every test builds its app with ``build_deps(...)`` from in-memory fakes
  (``InMemoryEventStore``, ``InMemoryBus``, ``InMemoryArtifactStore``,
  ``InMemoryTicketStore``) and talks to it with ``httpx.AsyncClient`` over ASGI, or
  with ``AsgiWebSocket`` (an in-loop ASGI WebSocket client) for the gateway.
* Tests marked ``infra`` add a throwaway, migrated Postgres database (same scheme as
  ``tests/integration/db/conftest.py``) so routes read real read models. Run them
  with ``uv run pytest -m infra tests/integration/api``.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplatform.api.deps import ApiDeps
from agentplatform.api.tickets import InMemoryTicketStore, TicketStore
from agentplatform.config import ModelsConfig, PoliciesConfig, RoleConfig, Settings, load_settings
from agentplatform.contracts.common import Actor, HealthStatus
from agentplatform.contracts.events import EventDraft, SessionCreated
from agentplatform.contracts.session import Session
from agentplatform.db.event_store import EventStore, PostgresEventStore
from agentplatform.db.session import SessionMaker, create_engine, create_sessionmaker
from agentplatform.models.base import ModelClient
from agentplatform.sandboxes.base import SandboxProvider
from agentplatform.testing import (
    FakeModel,
    FakeSandboxProvider,
    InMemoryArtifactStore,
    InMemoryBus,
    InMemoryEventStore,
    make_session,
)

BACKEND = Path(__file__).resolve().parents[3]

API_TOKEN = "test-api-token-0123456789abcdef"  # gitleaks:allow
FAKE_ANTHROPIC_KEY = "sk-ant-test-0000000000000000000000"  # gitleaks:allow
FAKE_URL_SECRET = "urlsecret-1234567890"  # gitleaks:allow


# --- configuration -----------------------------------------------------------------------


def models_config() -> ModelsConfig:
    """One profile ``test`` whose base URL carries credentials that must never leak."""
    local = {
        "provider": "openai_compatible",
        "model": "local-model",
        "base_url": f"http://user:{FAKE_URL_SECRET}@localhost:1234/v1?key={FAKE_URL_SECRET}",
    }
    return ModelsConfig.model_validate(
        {
            "default_profile": "test",
            "profiles": {
                "test": {
                    "planner": local,
                    "supervisor": local,
                    "summarizer": local,
                    "agent": {
                        "provider": "anthropic",
                        "model": "claude-test",
                        "api_key_env": "ANTHROPIC_API_KEY",
                        "vision": True,
                        "coordinate_mode": "normalized_1000",
                    },
                }
            },
        }
    )


def make_settings(*, token: str | None = None) -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_api_token=SecretStr(token) if token else None,
        anthropic_api_key=SecretStr(FAKE_ANTHROPIC_KEY),
        frontend_origin="http://localhost:3000",
    )


class StubModelClient:
    """A ``ModelClient`` whose ``health()`` is scripted by the test."""

    def __init__(self, role: RoleConfig, behaviour: Callable[[], Awaitable[HealthStatus]]):
        self.role = role
        self._behaviour = behaviour
        self.calls = 0
        self.closed = False

    @property
    def name(self) -> str:
        return self.role.model

    async def complete(self, req: object) -> object:  # pragma: no cover - unused
        raise NotImplementedError

    async def health(self) -> HealthStatus:
        self.calls += 1
        return await self._behaviour()

    async def aclose(self) -> None:
        self.closed = True


def _healthy_client(role: RoleConfig) -> ModelClient:
    return FakeModel(name=role.model)


def _unreachable_sessionmaker() -> SessionMaker:
    # Never connected: non-infra tests override every DB-backed hook.
    return create_sessionmaker(create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:9/none"))


def build_deps(
    *,
    token: str | None = None,
    events: EventStore | None = None,
    bus: InMemoryBus | None = None,
    sessionmaker: SessionMaker | None = None,
    tickets: TicketStore | None = None,
    session_exists: Callable[[str], Awaitable[bool]] | None = None,
    sandbox_providers: dict[str, SandboxProvider] | None = None,
    model_client_factory: Callable[[RoleConfig], ModelClient] = _healthy_client,
    max_upload_bytes: int = 50 * 1024 * 1024,
    **kwargs: Any,
) -> ApiDeps:
    readiness = kwargs.pop("readiness", None)
    if sessionmaker is None and readiness is None:
        readiness = {"database": _true, "redis": _true}
    return ApiDeps(
        settings=make_settings(token=token),
        models=models_config(),
        policies=PoliciesConfig(),
        sessionmaker=sessionmaker or _unreachable_sessionmaker(),
        events=events or InMemoryEventStore(),
        bus=bus or InMemoryBus(),
        artifacts=InMemoryArtifactStore(),
        tickets=tickets or InMemoryTicketStore(),
        sandbox_providers=(
            sandbox_providers
            if sandbox_providers is not None
            else {"docker_local": FakeSandboxProvider()}
        ),
        model_client_factory=model_client_factory,
        session_exists=session_exists,
        readiness=readiness,
        max_upload_bytes=max_upload_bytes,
        **kwargs,
    )


async def _true() -> bool:
    return True


def client_for(
    app: FastAPI, *, token: str | None = None, raise_app_exceptions: bool = True
) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions),
        base_url="http://test",
        headers=headers,
    )


# --- in-loop ASGI WebSocket client ---------------------------------------------------------


class WsClosed(Exception):
    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"closed with {code} {reason}")
        self.code = code
        self.reason = reason


class AsgiWebSocket:
    """Drive an ASGI app's WebSocket endpoint in the current event loop.

    ``async with AsgiWebSocket(app, path, ticket=..., since=...) as ws`` performs the
    handshake; ``await ws.receive_json()`` returns the next message or raises
    ``WsClosed(code)``; leaving the block disconnects the client.
    """

    def __init__(self, app: FastAPI, path: str, **query: object) -> None:
        self._app = app
        self._path = path
        self._query = urlencode({k: v for k, v in query.items() if v is not None})
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self.accepted = False
        self.close_code: int | None = None

    async def __aenter__(self) -> AsgiWebSocket:
        scope: dict[str, Any] = {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": self._query.encode(),
            "root_path": "",
            "headers": [(b"host", b"test")],
            "client": ("127.0.0.1", 50000),
            "server": ("test", 80),
            "subprotocols": [],
            "state": {},
        }
        self._task = asyncio.create_task(
            self._app(scope, self._to_app.get, self._from_app.put)  # type: ignore[arg-type]
        )
        await self._to_app.put({"type": "websocket.connect"})
        message = await asyncio.wait_for(self._from_app.get(), timeout=5)
        if message["type"] == "websocket.accept":
            self.accepted = True
        elif message["type"] == "websocket.close":
            self.close_code = int(message.get("code", 1000))
        return self

    async def receive(self, wait_s: float = 5.0) -> dict[str, Any]:
        if self.close_code is not None:
            raise WsClosed(self.close_code)
        message = await asyncio.wait_for(self._from_app.get(), timeout=wait_s)
        if message["type"] == "websocket.close":
            self.close_code = int(message.get("code", 1000))
            raise WsClosed(self.close_code, str(message.get("reason", "")))
        assert message["type"] == "websocket.send", message
        return message

    async def receive_json(self, wait_s: float = 5.0) -> dict[str, Any]:
        message = await self.receive(wait_s)
        data: dict[str, Any] = json.loads(message["text"])
        return data

    async def expect_close(self, wait_s: float = 5.0) -> int:
        try:
            message = await self.receive(wait_s)
        except WsClosed as closed:
            return closed.code
        raise AssertionError(f"expected a close, got {message}")

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except TimeoutError:  # pragma: no cover - a hang is a test failure
                self._task.cancel()
                raise


# --- Postgres (infra) ---------------------------------------------------------------------


def _load_testdb() -> Any:
    path = Path(__file__).resolve().parents[1] / "db" / "testdb.py"
    spec = importlib.util.spec_from_file_location("api_testdb", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    testdb = _load_testdb()
    base = make_url(load_settings().effective_database_url)
    source = base.database or ""
    name = testdb.itest_database_name(source, testdb.new_run_id())
    assert name != source
    admin = base.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin_execute(admin, testdb.create_database_sql(name)))
    try:
        url = base.set(database=name).render_as_string(hide_password=False)
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield url
    finally:
        asyncio.run(
            _admin_execute(admin, testdb.drop_database_sql(name, source=source, created=name))
        )


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(url=database_url, pool_size=5, max_overflow=10)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> SessionMaker:
    return create_sessionmaker(engine)


@pytest.fixture
def pg_store(sessionmaker: SessionMaker) -> PostgresEventStore:
    return PostgresEventStore(sessionmaker)


async def create_db_session(store: EventStore, **overrides: Any) -> Session:
    """Append ``session.created`` so the event store creates the sessions row."""
    session = make_session(**overrides)
    await store.append(
        session.id, EventDraft(payload=SessionCreated(session=session), actor=Actor.user("local"))
    )
    return session
