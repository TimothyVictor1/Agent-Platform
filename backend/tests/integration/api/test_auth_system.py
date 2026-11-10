"""Auth middleware, CORS, request ids, problem+json, health and metrics (no database)."""

from __future__ import annotations

import pytest

from agentplatform.api.app import create_app
from agentplatform.api.problems import PROBLEM_CONTENT_TYPE

from .conftest import API_TOKEN, FAKE_ANTHROPIC_KEY, build_deps, client_for


async def _false() -> bool:
    return False


async def _true() -> bool:
    return True


async def test_without_token_requests_need_no_auth() -> None:
    app = create_app(build_deps(token=None))
    async with client_for(app) as client:
        assert (await client.get("/api/v1/config/profiles")).status_code == 200


@pytest.mark.parametrize(
    "header",
    [None, "Bearer wrong-token", f"Basic {API_TOKEN}", f"Bearer {API_TOKEN}x", "Bearer"],
)
async def test_with_token_bad_or_missing_credentials_get_401(header: str | None) -> None:
    app = create_app(build_deps(token=API_TOKEN))
    headers = {"Authorization": header} if header else {}
    async with client_for(app) as client:
        response = await client.get("/api/v1/config/profiles", headers=headers)
    assert response.status_code == 401
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.headers["www-authenticate"] == "Bearer"
    body = response.json()
    assert body["status"] == 401
    assert body["type"].endswith(":unauthorized")
    assert API_TOKEN not in response.text


async def test_with_token_the_right_bearer_passes() -> None:
    app = create_app(build_deps(token=API_TOKEN))
    async with client_for(app, token=API_TOKEN) as client:
        assert (await client.get("/api/v1/config/profiles")).status_code == 200
        # case-insensitive scheme
        r = await client.get("/api/v1/metrics", headers={"Authorization": f"bearer {API_TOKEN}"})
        assert r.status_code == 200


async def test_health_and_docs_are_exempt_and_unknown_paths_still_need_auth() -> None:
    app = create_app(build_deps(token=API_TOKEN))
    async with client_for(app) as client:
        assert (await client.get("/api/v1/health")).status_code == 200
        assert (await client.get("/openapi.json")).status_code == 200
        assert (await client.get("/api/v1/nope")).status_code == 401


async def test_cors_preflight_and_401_carry_cors_headers_for_the_frontend_only() -> None:
    app = create_app(build_deps(token=API_TOKEN))
    origin = "http://localhost:3000"
    async with client_for(app) as client:
        pre = await client.options(
            "/api/v1/sessions",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        assert pre.status_code == 200
        assert pre.headers["access-control-allow-origin"] == origin
        denied = await client.get("/api/v1/sessions", headers={"Origin": origin})
        assert denied.status_code == 401
        assert denied.headers["access-control-allow-origin"] == origin
        evil = await client.options(
            "/api/v1/sessions",
            headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in evil.headers


async def test_request_id_is_echoed_or_generated() -> None:
    app = create_app(build_deps())
    async with client_for(app) as client:
        kept = await client.get("/api/v1/health", headers={"X-Request-ID": "abc-123"})
        assert kept.headers["x-request-id"] == "abc-123"
        replaced = await client.get("/api/v1/health", headers={"X-Request-ID": "bad id\n<x>"})
        assert replaced.headers["x-request-id"] not in ("bad id\n<x>", "")
        assert len(replaced.headers["x-request-id"]) == 36


async def test_health_reports_degraded_checks() -> None:
    app = create_app(build_deps(readiness={"database": _true, "redis": _false}))
    async with client_for(app) as client:
        body = (await client.get("/api/v1/health")).json()
    assert body["status"] == "degraded"
    assert body["checks"] == {"database": True, "redis": False}
    assert body["version"]


async def test_metrics_count_requests() -> None:
    app = create_app(build_deps())
    async with client_for(app) as client:
        await client.get("/api/v1/health")
        await client.get("/api/v1/does-not-exist")
        counters = (await client.get("/api/v1/metrics")).json()["counters"]
    assert counters["http.requests.2xx"] >= 1
    assert counters["http.requests.4xx"] == 1


async def test_unexpected_errors_are_generic_problem_json_without_details() -> None:
    app = create_app(build_deps())

    async def boom() -> None:
        raise RuntimeError(f"connection failed with key {FAKE_ANTHROPIC_KEY}")

    app.add_api_route("/api/v1/boom", boom)
    async with client_for(app, raise_app_exceptions=False) as client:
        response = await client.get("/api/v1/boom")
    assert response.status_code == 500
    assert response.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert response.json()["detail"] == "An unexpected error occurred."
    assert FAKE_ANTHROPIC_KEY not in response.text


async def test_unknown_route_and_wrong_method_are_problem_json() -> None:
    app = create_app(build_deps())
    async with client_for(app) as client:
        missing = await client.get("/api/v1/nothing-here")
        wrong = await client.delete("/api/v1/health")
    assert missing.status_code == 404
    assert missing.headers["content-type"] == PROBLEM_CONTENT_TYPE
    assert wrong.status_code == 405
    assert wrong.json()["status"] == 405
