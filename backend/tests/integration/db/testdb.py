"""Naming and SQL for the throwaway integration-test database (pure, no I/O).

Kept separate from ``conftest.py`` so the safety rules can be unit-tested without
a database: the test database name always differs from the source database,
fits Postgres' 63-byte identifier limit with its ``_itest_<run id>`` suffix
intact, and is quoted as an identifier in every statement.
"""

from __future__ import annotations

import uuid

from sqlalchemy.dialects import postgresql

__all__ = [
    "MAX_IDENTIFIER_BYTES",
    "TEST_DB_MARKER",
    "create_database_sql",
    "drop_database_sql",
    "itest_database_name",
    "new_run_id",
    "quote_identifier",
]

MAX_IDENTIFIER_BYTES = 63
"""Postgres truncates identifiers to NAMEDATALEN - 1 bytes."""
TEST_DB_MARKER = "_itest_"
_PREPARER = postgresql.dialect().identifier_preparer  # type: ignore[no-untyped-call]


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def itest_database_name(source: str, run_id: str) -> str:
    """``<source prefix>_itest_<run_id>``, at most 63 bytes, never equal to
    ``source``. The source part is shortened (on a character boundary), never the
    suffix."""
    if not run_id or not run_id.isalnum():
        raise ValueError("run_id must be a non-empty alphanumeric string")
    suffix = f"{TEST_DB_MARKER}{run_id}"
    room = MAX_IDENTIFIER_BYTES - len(suffix.encode())
    if room < 1:
        raise ValueError("run_id is too long for a Postgres identifier")
    prefix = (source or "agentplatform").encode()[:room].decode(errors="ignore")
    name = f"{prefix}{suffix}"
    if name == source or len(name.encode()) > MAX_IDENTIFIER_BYTES:
        raise ValueError("could not derive a safe test database name")
    return name


def quote_identifier(name: str) -> str:
    """Always-quoted Postgres identifier (embedded quotes doubled)."""
    return _PREPARER.quote_identifier(name)


def create_database_sql(name: str) -> str:
    return f"CREATE DATABASE {quote_identifier(name)}"


def drop_database_sql(name: str, *, source: str, created: str | None) -> str:
    """``DROP DATABASE`` for ``name``, refusing anything but the database this run
    created (``created``) and anything equal to the source database."""
    if created is None or name != created:
        raise ValueError(f"refusing to drop {name!r}: this run did not create it")
    if name == source or TEST_DB_MARKER not in name:
        raise ValueError(f"refusing to drop {name!r}: not a test database")
    return f"DROP DATABASE IF EXISTS {quote_identifier(name)} WITH (FORCE)"
