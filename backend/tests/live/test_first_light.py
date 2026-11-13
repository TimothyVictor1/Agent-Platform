"""First light (T13): one real agent completes one real task on ``docker_local``.

Runs the API (in process, through ASGI) and the runtime against a throwaway,
migrated database on the configured Postgres, the configured Redis, the local
desktop image and the model profile named by ``APP_LIVE_PROFILE``. It costs money
with a cloud profile; the session budget is capped by ``APP_LIVE_MAX_COST_USD``
(default 2 USD).

    APP_LIVE=1 APP_DOCKER=1 APP_LIVE_PROFILE=<profile> \\
        uv run pytest tests/live/test_first_light.py -s

Needs: ``config/models.yaml`` with that profile, the API key it names (in ``.env``),
``config/policies.yaml``, ``make sandbox-image`` and ``make db-up``. See
docs/getting-started.md.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from agentplatform.api.app import create_app
from agentplatform.api.deps import build_deps
from agentplatform.config import Settings, load_settings
from agentplatform.runtime.container import build_container
from agentplatform.runtime.main import Runtime

pytestmark = [pytest.mark.live, pytest.mark.docker]

BACKEND = Path(__file__).resolve().parents[2]
PROMPT = (
    "Open the browser, go to example.com and report the page title (the text shown in "
    "the browser tab or window title) as output `title`"
)
EXPECTED_TITLE = "Example Domain"
"""example.com has no ``<h1>`` any more (2026); its ``<title>`` is still this."""
PLAN_TIMEOUT_S = 300.0
RUN_TIMEOUT_S = 20 * 60.0


def _testdb() -> Any:
    path = BACKEND / "tests" / "integration" / "db" / "testdb.py"
    spec = importlib.util.spec_from_file_location("first_light_testdb", path)
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
def settings() -> Iterator[Settings]:
    """Settings pointing at a fresh database (dropped afterwards), so the runtime
    under test never adopts sessions of the developer's own database."""
    base_settings = load_settings()
    if not os.environ.get("APP_LIVE_PROFILE"):
        pytest.skip("set APP_LIVE_PROFILE to the model profile to use")
    testdb = _testdb()
    base = make_url(base_settings.effective_database_url)
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
        yield base_settings.model_copy(update={"database_url": url, "app_db_name": ""})
    finally:
        asyncio.run(_admin(admin, testdb.drop_database_sql(name, source=source, created=name)))


@pytest.fixture
async def stack(settings: Settings) -> AsyncIterator[tuple[httpx.AsyncClient, Runtime]]:
    deps = await build_deps(settings)
    container = await build_container(settings)
    container.orphan_cleaners.clear()  # never touch the developer's other desktops
    runtime = Runtime(container)
    await runtime.start()
    headers = {"Authorization": f"Bearer {deps.api_token}"} if deps.api_token else {}
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(deps)),
        base_url="http://first-light",
        headers=headers,
        timeout=60.0,
    )
    try:
        yield client, runtime
    finally:
        for host in list(runtime.hosts.values()):
            await host.rt.pool.shutdown("first light finished")  # kill the desktops
        await runtime.shutdown()
        await container.aclose()
        await client.aclose()
        await deps.aclose()


async def _detail(client: httpx.AsyncClient, sid: str) -> dict[str, Any]:
    resp = await client.get(f"/api/v1/sessions/{sid}")
    resp.raise_for_status()
    data: dict[str, Any] = resp.json()
    return data


async def _events(client: httpx.AsyncClient, sid: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    after = 0
    while True:
        resp = await client.get(
            f"/api/v1/sessions/{sid}/events", params={"after_seq": after, "limit": 1000}
        )
        resp.raise_for_status()
        items = resp.json()["items"]
        if not items:
            return events
        events.extend(items)
        after = items[-1]["seq"]


async def _wait_status(
    client: httpx.AsyncClient, sid: str, wanted: set[str], timeout_s: float
) -> dict[str, Any]:
    async with asyncio.timeout(timeout_s):
        while True:
            detail = await _detail(client, sid)
            status = detail["session"]["status"]
            if status in wanted:
                return detail
            if status in ("failed", "stopped"):
                types = [e["type"] for e in await _events(client, sid)][-15:]
                pytest.fail(f"session ended {status}; last events: {types}")
            events = await _events(client, sid)
            failed = [e for e in events if e["type"] == "session.planning_failed"]
            if failed:
                pytest.fail(f"planning failed: {failed[-1]['payload']['reason']}")
            await asyncio.sleep(2.0)


async def test_first_light(stack: tuple[httpx.AsyncClient, Runtime]) -> None:
    client, _runtime = stack
    max_cost = float(os.environ.get("APP_LIVE_MAX_COST_USD", "2.0"))
    resp = await client.post(
        "/api/v1/sessions",
        json={
            "prompt": PROMPT,
            "agent_count": 1,
            "model_profile": os.environ["APP_LIVE_PROFILE"],
            "sandbox_provider": "docker_local",
            "budgets": {"max_cost_usd": max_cost, "max_wall_minutes": 20},
        },
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    await _wait_status(client, sid, {"awaiting_approval"}, PLAN_TIMEOUT_S)
    resp = await client.post(f"/api/v1/sessions/{sid}/approve")
    assert resp.status_code == 202, resp.text
    detail = await _wait_status(client, sid, {"completed"}, RUN_TIMEOUT_S)

    outputs = [t["result"]["outputs"] for t in detail["tasks"] if t.get("result")]
    titles = [str(o["title"]).strip() for o in outputs if "title" in o]
    assert titles, f"no task reported a `title` output: {outputs}"
    assert titles[-1].casefold() == EXPECTED_TITLE.casefold(), titles[-1]

    frames = (await client.get(f"/api/v1/sessions/{sid}/frames")).json()["items"]
    assert frames, "no frames were stored"
    frame = await client.get(f"/api/v1/frames/{frames[-1]['id']}")
    assert frame.status_code == 200
    assert frame.headers["content-type"].startswith("image/jpeg")

    seqs = [e["seq"] for e in await _events(client, sid)]
    assert seqs == list(range(1, len(seqs) + 1)), "event seqs are not contiguous"
    usage = detail["session"]["usage"]
    print(f"\nfirst light: {usage['steps']} steps, ${usage['cost_usd']:.4f}")
