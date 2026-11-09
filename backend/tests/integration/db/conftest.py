"""Fixtures for Postgres-backed tests (marker ``infra``; needs ``make db-up``).

A throwaway database ``<APP_DB_NAME>_itest`` is recreated and migrated with Alembic
once per test run, so tests never touch a worktree's real data. Tests isolate
themselves by using fresh session ids, not by truncating tables.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
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

BACKEND = Path(__file__).resolve().parents[3]


async def _recreate_database(admin_url: str, name: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL of a freshly migrated test database (created once per run)."""
    base = make_url(load_settings().effective_database_url)
    name = f"{base.database or 'agentplatform'}_itest"[:63]
    admin = base.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_recreate_database(admin, name))
    url = base.set(database=name).render_as_string(hide_password=False)
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    return url


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
