"""The scheduler on the real ``PostgresEventStore`` (marker ``infra``).

Needs the compose Postgres. Run against the shared instance, e.g.::

    DATABASE_URL=postgresql+asyncpg://agentplatform:agentplatform@localhost:5434/agentplatform \
        uv run pytest -m infra tests/integration/scheduler

A throwaway ``<db>_itest_<run id>`` database is created, migrated and dropped, with
the same safety rules as ``tests/integration/db`` (helpers loaded from its
``testdb.py``).
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import ModuleType

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplatform.config import load_settings
from agentplatform.contracts.events import SessionCreated
from agentplatform.db.event_store import PostgresEventStore
from agentplatform.db.repositories import SessionRepository
from agentplatform.db.session import SessionMaker, create_engine, create_sessionmaker
from agentplatform.testing import make_session, make_tasks

from .harness import build_world, order_of

pytestmark = pytest.mark.infra

BACKEND = Path(__file__).resolve().parents[3]


def _testdb() -> ModuleType:
    path = BACKEND / "tests" / "integration" / "db" / "testdb.py"
    spec = importlib.util.spec_from_file_location("_scheduler_testdb", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _admin(url: str, sql: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(sql))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def database_url() -> Iterator[str]:
    testdb = _testdb()
    base = make_url(load_settings().effective_database_url)
    source = base.database or ""
    name = testdb.itest_database_name(source, testdb.new_run_id())
    admin = base.set(database="postgres").render_as_string(hide_password=False)
    asyncio.run(_admin(admin, testdb.create_database_sql(name)))
    try:
        url = base.set(database=name).render_as_string(hide_password=False)
        cfg = Config(str(BACKEND / "alembic.ini"))
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
        command.upgrade(cfg, "head")
        yield url
    finally:
        sql = testdb.drop_database_sql(name, source=source, created=name)
        asyncio.run(_admin(admin, sql))


@pytest.fixture
async def sessionmaker(database_url: str) -> AsyncIterator[SessionMaker]:
    engine = create_engine(url=database_url, pool_size=5, max_overflow=10)
    try:
        yield create_sessionmaker(engine)
    finally:
        await engine.dispose()


async def test_dag_runs_on_postgres_event_store(sessionmaker: SessionMaker) -> None:
    store = PostgresEventStore(sessionmaker)
    session = make_session(agent_count=2, status="awaiting_approval")
    w = build_world(session=session, store=store)
    # the event store creates the session row; the scheduler never inserts it
    await w.record(SessionCreated(session=session))
    await w.approve(make_tasks({"T1": [], "T2": [], "T3": ["T1", "T2"]}), 1)
    w.start()
    try:
        await w.wait_for("session.completed", limit_s=30)
        events = await w.events()
    finally:
        await w.stop()

    assert [e.seq for e in events] == list(range(1, len(events) + 1))
    assert sorted(order_of(events, "task.accepted")) == ["T1", "T2", "T3"]
    assert [e.type for e in events].count("session.created") == 1
    async with sessionmaker() as tx:
        stored = await SessionRepository(tx).get(session.id)
    assert stored.id == session.id
