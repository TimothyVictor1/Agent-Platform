"""Scenario suite (SPEC §21) fixtures.

The suite reuses the runtime "world" harness of ``tests/integration/runtime``
(``runtime.support``). ``tests/integration`` is not a package, so its folder is put on
``sys.path`` here; mypy resolves ``runtime.support`` the same way.
"""

from __future__ import annotations

import sys
from pathlib import Path

_INTEGRATION = str(Path(__file__).resolve().parents[1])
if _INTEGRATION not in sys.path:
    sys.path.insert(0, _INTEGRATION)


from collections.abc import AsyncIterator  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture
async def world() -> AsyncIterator[object]:
    from scenarios.harness import ScenarioWorld

    w = ScenarioWorld()
    yield w
    await w.close()


# Postgres fixtures of the runtime tests (throwaway migrated database per run; only
# created when an ``infra`` test asks for them).
from runtime.conftest import database_url, engine, sessionmaker  # noqa: E402, F401
