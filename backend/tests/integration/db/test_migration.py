"""The Alembic migrations create exactly the schema of ``db/models.py``.

Needs the compose Postgres (``make db-up``); marked ``infra``. Uses a throwaway
database so it never touches a worktree's real data.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplatform.config import load_settings
from agentplatform.db.models import Base

pytestmark = pytest.mark.infra

BACKEND = Path(__file__).resolve().parents[3]
TEST_DB = "agentplatform_migration_test"


async def _recreate_database(admin_url: str) -> None:
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{TEST_DB}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{TEST_DB}"'))
    finally:
        await engine.dispose()


def _diff(conn: Connection) -> list[object]:
    ctx = MigrationContext.configure(conn)
    return list(compare_metadata(ctx, Base.metadata))


async def _schema_diff(url: str) -> list[object]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_diff)
    finally:
        await engine.dispose()


def test_migrations_match_models() -> None:
    base_url = make_url(load_settings().effective_database_url)
    try:
        asyncio.run(_recreate_database(base_url.set(database="postgres").render_as_string(False)))
    except OSError as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Postgres not reachable ({exc}); run `make db-up`")
    url = base_url.set(database=TEST_DB).render_as_string(hide_password=False)

    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    command.upgrade(cfg, "head")
    assert asyncio.run(_schema_diff(url)) == []
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
