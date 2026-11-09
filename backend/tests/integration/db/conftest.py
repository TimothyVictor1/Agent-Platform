"""Fixtures for Postgres-backed tests (marker ``infra``; needs ``make db-up``).

A throwaway database ``<APP_DB_NAME prefix>_itest_<run id>`` is created and
migrated with Alembic once per test run and dropped at the end of the run, so
tests never touch a worktree's real data. Only the database this run created is
ever dropped (see ``testdb.py``). Tests isolate themselves by using fresh session
ids, not by truncating tables.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from agentplatform.config import load_settings
from agentplatform.contracts.common import Actor
from agentplatform.contracts.events import EventDraft, SessionCreated
from agentplatform.contracts.session import Session
from agentplatform.db.event_store import PostgresEventStore
from agentplatform.db.session import SessionMaker, create_engine, create_sessionmaker
from agentplatform.testing.builders import make_session

from .testdb import create_database_sql, drop_database_sql, itest_database_name, new_run_id

BACKEND = Path(__file__).resolve().parents[3]


async def _admin_execute(admin_url: str, sql: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    """URL of a freshly migrated test database (created once per run, dropped after)."""
    base = make_url(load_settings().effective_database_url)
    source = base.database or ""
    name = itest_database_name(source, new_run_id())
    assert name != source, "test database name must differ from the source database"
    admin = base.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin_execute(admin, create_database_sql(name)))
    created = name  # only now is this database ours to drop
    try:
        url = base.set(database=name).render_as_string(hide_password=False)
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield url
    finally:
        asyncio.run(_admin_execute(admin, drop_database_sql(name, source=source, created=created)))


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
async def tx(sessionmaker: SessionMaker) -> AsyncIterator[AsyncSession]:
    """A transaction that is rolled back after the test (repository tests)."""
    async with sessionmaker() as session, session.begin() as outer:
        yield session
        await outer.rollback()


def created_draft(session: Session) -> EventDraft:
    return EventDraft(payload=SessionCreated(session=session), actor=Actor.user("local"))


@pytest.fixture
async def created_session(store: PostgresEventStore) -> Session:
    """A session whose ``session.created`` event (seq 1) is committed."""
    session = make_session()
    await store.append(session.id, created_draft(session))
    return session
