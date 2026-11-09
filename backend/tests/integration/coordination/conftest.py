"""Postgres fixtures for the coordination tests (marker ``infra``; needs ``make db-up``).

Same throwaway-database scheme as ``tests/integration/db/conftest.py`` (one migrated
database per run, dropped afterwards, only if this run created it). Tests isolate
themselves with a fresh session per test.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplatform.config import load_settings
from agentplatform.contracts.agent import Agent
from agentplatform.contracts.common import Actor
from agentplatform.contracts.events import EventDraft, SessionCreated
from agentplatform.contracts.session import Session
from agentplatform.db.event_store import PostgresEventStore
from agentplatform.db.repositories import Repositories
from agentplatform.db.session import SessionMaker, create_engine, create_sessionmaker, transaction
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.coordination.postgres_board import PostgresBoardService
from agentplatform.testing.builders import make_agent, make_session
from agentplatform.testing.memory_bus import InMemoryBus

BACKEND = Path(__file__).resolve().parents[3]


def _load_testdb() -> object:
    path = Path(__file__).resolve().parents[1] / "db" / "testdb.py"
    spec = importlib.util.spec_from_file_location("coordination_testdb", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


testdb = _load_testdb()


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    base = make_url(load_settings().effective_database_url)
    source = base.database or ""
    name = testdb.itest_database_name(source, testdb.new_run_id())  # type: ignore[attr-defined]
    assert name != source
    admin = base.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin_execute(admin, testdb.create_database_sql(name)))  # type: ignore[attr-defined]
    try:
        url = base.set(database=name).render_as_string(hide_password=False)
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield url
    finally:
        drop = testdb.drop_database_sql(name, source=source, created=name)  # type: ignore[attr-defined]
        asyncio.run(_admin_execute(admin, drop))


@pytest.fixture
async def engine(database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(url=database_url, pool_size=10, max_overflow=20)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> SessionMaker:
    return create_sessionmaker(engine)


@pytest.fixture
def store(sessionmaker: SessionMaker) -> PostgresEventStore:
    return PostgresEventStore(sessionmaker)


@pytest.fixture
def bus() -> InMemoryBus:
    return InMemoryBus()


@pytest.fixture
def recorder(store: PostgresEventStore, bus: InMemoryBus) -> EventRecorder:
    return EventRecorder(store, bus)


@pytest.fixture
def board(sessionmaker: SessionMaker, recorder: EventRecorder) -> PostgresBoardService:
    return PostgresBoardService(sessionmaker, recorder, poll_interval_s=0.05)


@dataclass(frozen=True)
class World:
    """A session with three agents (Ada, Brook, Cleo)."""

    session: Session
    ada: Agent
    brook: Agent
    cleo: Agent

    @property
    def id(self) -> str:
        return self.session.id

    def actor(self, agent: Agent) -> Actor:
        return Actor.agent(agent.id, agent.name)


@pytest.fixture
async def world(store: PostgresEventStore, sessionmaker: SessionMaker) -> World:
    session = make_session()
    await store.append(
        session.id, EventDraft(payload=SessionCreated(session=session), actor=Actor.user("local"))
    )
    agents = [make_agent(i, session_id=session.id) for i in range(3)]
    async with transaction(sessionmaker) as tx:
        repos = Repositories(tx)
        await repos.sessions.insert(session, if_missing=True)
        for agent in agents:
            await repos.agents.upsert(agent)
    return World(session, *agents)
