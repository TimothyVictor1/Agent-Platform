"""Async SQLAlchemy engine, sessionmaker and transaction helpers (SPEC §17).

Create one engine and one sessionmaker per process (in ``main.py``) and inject them;
there is no module-level engine. The URL is ``Settings.effective_database_url``:
``DATABASE_URL`` with the database name replaced by ``APP_DB_NAME`` when set.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agentplatform.config import Settings

__all__ = [
    "SessionMaker",
    "as_session",
    "create_engine",
    "create_sessionmaker",
    "session_dependency",
    "transaction",
]

SessionMaker = async_sessionmaker[AsyncSession]


def create_engine(
    settings: Settings | None = None,
    *,
    url: str | None = None,
    **engine_kwargs: Any,
) -> AsyncEngine:
    """Async engine for ``url`` or, by default, ``settings.effective_database_url``.

    ``pool_pre_ping`` is on so connections dropped by a Postgres restart are
    replaced transparently. Extra keyword arguments go to ``create_async_engine``.
    """
    if url is None:
        url = (settings or Settings()).effective_database_url
    engine_kwargs.setdefault("pool_pre_ping", True)
    return create_async_engine(url, **engine_kwargs)


def create_sessionmaker(engine: AsyncEngine) -> SessionMaker:
    """Sessionmaker whose objects stay readable after commit (``expire_on_commit=False``)."""
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@asynccontextmanager
async def transaction(sessionmaker: SessionMaker) -> AsyncIterator[AsyncSession]:
    """``async with transaction(sm) as tx:`` — commits on success, rolls back on error."""
    async with sessionmaker() as tx, tx.begin():
        yield tx


def session_dependency(
    sessionmaker: SessionMaker,
) -> Callable[[], AsyncIterator[AsyncSession]]:
    """A FastAPI dependency yielding one transactional ``AsyncSession`` per request."""

    async def dependency() -> AsyncIterator[AsyncSession]:
        async with transaction(sessionmaker) as tx:
            yield tx

    return dependency


def as_session(tx: object) -> AsyncSession:
    """Narrow the ``tx`` handle passed to a ``ReadModelUpdate`` to an ``AsyncSession``.

    Raises ``TypeError`` when called with the in-memory store's ``None`` handle, so a
    Postgres-only read model fails loudly instead of silently doing nothing.
    """
    if not isinstance(tx, AsyncSession):
        raise TypeError(f"expected an AsyncSession transaction handle, got {type(tx).__name__}")
    return tx
