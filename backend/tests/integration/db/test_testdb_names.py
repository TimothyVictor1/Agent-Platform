"""The throwaway test database can never be the source database (T04 review #2).

Pure tests: no database connection, no SQL executed.
"""

from __future__ import annotations

import pytest

from .testdb import (
    MAX_IDENTIFIER_BYTES,
    create_database_sql,
    drop_database_sql,
    itest_database_name,
    quote_identifier,
)

RUN = "0123456789ab"


@pytest.mark.parametrize(
    "source",
    [
        "a" * 63,
        "a" * 57 + "_itest",  # old scheme: f"{source}_itest"[:63] == source
        "agentplatform_" + "x" * 49,
        "agentplatform",
        "ä" * 31,  # multi-byte: 62 bytes
        "",
    ],
)
def test_name_is_length_safe_distinct_and_keeps_suffix(source: str) -> None:
    name = itest_database_name(source, RUN)
    assert name != source
    assert len(name.encode()) <= MAX_IDENTIFIER_BYTES
    assert name.endswith(f"_itest_{RUN}")


def test_63_char_source_is_never_the_drop_target() -> None:
    source = "a" * 57 + "_itest"
    assert len(source) == 63
    name = itest_database_name(source, RUN)
    sql = drop_database_sql(name, source=source, created=name)
    assert quote_identifier(source) not in sql
    with pytest.raises(ValueError, match="not a test database"):
        drop_database_sql(source, source=source, created=source)


def test_runs_get_distinct_names() -> None:
    assert itest_database_name("db", "aaaa") != itest_database_name("db", "bbbb")


def test_only_the_created_database_is_dropped() -> None:
    name = itest_database_name("agentplatform", RUN)
    with pytest.raises(ValueError, match="did not create it"):
        drop_database_sql(name, source="agentplatform", created=None)
    with pytest.raises(ValueError, match="did not create it"):
        drop_database_sql("agentplatform", source="agentplatform", created=name)
    with pytest.raises(ValueError, match="not a test database"):
        drop_database_sql("other", source="agentplatform", created="other")
    assert drop_database_sql(name, source="agentplatform", created=name) == (
        f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'
    )


def test_identifiers_are_quoted() -> None:
    assert quote_identifier('we"ird') == '"we""ird"'
    assert create_database_sql('x"y') == 'CREATE DATABASE "x""y"'


@pytest.mark.parametrize("run_id", ["", "a-b", "x" * 60])
def test_bad_run_ids_are_rejected(run_id: str) -> None:
    with pytest.raises(ValueError, match="run_id"):
        itest_database_name("db", run_id)
