"""Fixtures for the runtime wiring tests.

Postgres fixtures (marker ``infra``; needs ``make db-up`` and ``DATABASE_URL``): one
throwaway migrated database per run, the same scheme as ``tests/integration/db``.
They are only created when a test asks for them; the fake-based tests never do.
"""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from agentplatform.config import load_settings
from agentplatform.db.session import SessionMaker, create_engine, create_sessionmaker

BACKEND = Path(__file__).resolve().parents[3]


def _load_testdb() -> object:
    path = Path(__file__).resolve().parents[1] / "db" / "testdb.py"
    spec = importlib.util.spec_from_file_location("runtime_testdb", path)
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
