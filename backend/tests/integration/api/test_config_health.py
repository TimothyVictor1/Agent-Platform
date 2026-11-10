"""``/config/profiles`` and ``/config/providers/health``: summaries, caching, timeouts,
and that no API key (or URL credential) ever leaves the API."""

from __future__ import annotations

import asyncio

from agentplatform.api.app import create_app
from agentplatform.api.health import ProvidersHealthChecker, public_base_url, scrub
from agentplatform.config import RoleConfig
from agentplatform.contracts.common import HealthStatus
from agentplatform.errors import ConfigError
from agentplatform.models.base import ModelClient
from agentplatform.testing import FakeSandboxProvider

from .conftest import (
    API_TOKEN,
    FAKE_ANTHROPIC_KEY,
    FAKE_URL_SECRET,
    StubModelClient,
    build_deps,
    client_for,
    models_config,
)

SECRETS = (FAKE_ANTHROPIC_KEY, FAKE_URL_SECRET, API_TOKEN)


def assert_no_secrets(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text
    assert "api_key" not in text
    assert "ANTHROPIC_API_KEY" not in text


async def test_profiles_have_role_summaries_and_no_keys_or_url_credentials() -> None:
    app = create_app(build_deps(token=API_TOKEN))
    async with client_for(app, token=API_TOKEN) as client:
        response = await client.get("/api/v1/config/profiles")
    assert response.status_code == 200
    assert_no_secrets(response.text)
    body = response.json()
    assert body["default_profile"] == "test"
    (profile,) = body["items"]
    assert profile["name"] == "test"
    assert set(profile["roles"]) == {"planner", "supervisor", "agent", "summarizer"}
    assert profile["roles"]["planner"]["base_url"] == "http://localhost:1234/v1"
    agent = profile["roles"]["agent"]
    assert agent == {
        "provider": "anthropic",
        "model": "claude-test",
        "base_url": None,
        "vision": True,
        "coordinate_mode": "normalized_1000",
    }


def test_public_base_url_strips_userinfo_query_and_fragment() -> None:
    assert public_base_url("https://u:p@h.example:8443/v1?key=s#f") == "https://h.example:8443/v1"
    assert public_base_url("http://[::1]:11434/v1") == "http://[::1]:11434/v1"
    assert public_base_url(None) is None
    assert scrub(f"bad key {FAKE_ANTHROPIC_KEY}!", [FAKE_ANTHROPIC_KEY]) == "bad key [redacted]!"


async def test_providers_health_endpoint_lists_models_and_sandboxes_without_secrets() -> None:
    async def leaky() -> HealthStatus:
        return HealthStatus(ok=False, error=f"401 for key {FAKE_ANTHROPIC_KEY}")

    def factory(role: RoleConfig) -> ModelClient:
        return StubModelClient(role, leaky)

    app = create_app(build_deps(model_client_factory=factory))
    async with client_for(app) as client:
        response = await client.get("/api/v1/config/providers/health")
    assert response.status_code == 200
    assert_no_secrets(response.text)
    items = {(i["kind"], i["name"]): i for i in response.json()["items"]}
    # the three local roles share one endpoint, so 2 model checks + 1 sandbox
    assert len(items) == 3
    assert ("sandbox", "docker_local") in items
    assert items[("sandbox", "docker_local")]["ok"] is True
    model_errors = [i["error"] for (kind, _), i in items.items() if kind == "model"]
    assert model_errors
    assert all("[redacted]" in e for e in model_errors)


async def test_health_is_cached_and_shared_between_concurrent_callers() -> None:
    now = [0.0]
    calls = 0

    async def slow_ok() -> HealthStatus:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return HealthStatus(ok=True, latency_ms=7)

    checker = ProvidersHealthChecker(
        models_config(),
        {},
        lambda role: StubModelClient(role, slow_ok),
        clock=lambda: now[0],
    )
    first, second = await asyncio.gather(checker.check(), checker.check())
    assert first == second
    assert calls == 2  # two distinct endpoints, one check each
    now[0] += 29
    await checker.check()
    assert calls == 2
    now[0] += 2  # past the 30 s cache
    await checker.check()
    assert calls == 4
    await checker.aclose()


async def test_health_times_out_and_reports_unbuildable_clients() -> None:
    async def hang() -> HealthStatus:
        await asyncio.sleep(10)
        raise AssertionError("unreachable")

    def factory(role: RoleConfig) -> ModelClient:
        if role.provider == "anthropic":
            raise ConfigError("environment variable ANTHROPIC_API_KEY is not set")
        return StubModelClient(role, hang)

    sandbox = FakeSandboxProvider()
    checker = ProvidersHealthChecker(
        models_config(), {"docker_local": sandbox}, factory, timeout_s=0.05
    )
    result = await asyncio.wait_for(checker.check(), timeout=2)
    by_name = {i.name: i for i in result.items}
    local = next(i for n, i in by_name.items() if n.startswith("openai_compatible"))
    assert not local.ok
    assert local.error is not None
    assert "timed out" in local.error
    anthropic = next(i for n, i in by_name.items() if n.startswith("anthropic"))
    assert not anthropic.ok
    assert anthropic.error == "environment variable ANTHROPIC_API_KEY is not set"
    assert by_name["docker_local"].ok


async def test_health_failure_exceptions_do_not_leak_their_message() -> None:
    async def explode() -> HealthStatus:
        raise RuntimeError(f"token {FAKE_ANTHROPIC_KEY}")

    checker = ProvidersHealthChecker(
        models_config(), {}, lambda role: StubModelClient(role, explode)
    )
    result = await checker.check()
    assert all(not i.ok for i in result.items)
    assert all(FAKE_ANTHROPIC_KEY not in (i.error or "") for i in result.items)
