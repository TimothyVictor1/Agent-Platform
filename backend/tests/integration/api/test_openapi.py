"""The OpenAPI schema lists exactly the endpoints of SPEC §16 with contract models."""

from __future__ import annotations

from typing import Any

from agentplatform.api.app import create_app

from .conftest import build_deps, client_for

SPEC_ENDPOINTS = {
    ("post", "/api/v1/sessions"),
    ("get", "/api/v1/sessions"),
    ("get", "/api/v1/sessions/{session_id}"),
    ("post", "/api/v1/sessions/{session_id}/refine"),
    ("put", "/api/v1/sessions/{session_id}/plan"),
    ("post", "/api/v1/sessions/{session_id}/approve"),
    ("post", "/api/v1/sessions/{session_id}/followup"),
    ("post", "/api/v1/sessions/{session_id}/steer"),
    ("post", "/api/v1/sessions/{session_id}/stop"),
    ("post", "/api/v1/sessions/{session_id}/pause"),
    ("post", "/api/v1/sessions/{session_id}/resume"),
    ("post", "/api/v1/agents/{agent_id}/takeover"),
    ("post", "/api/v1/agents/{agent_id}/release"),
    ("get", "/api/v1/sessions/{session_id}/board"),
    ("post", "/api/v1/sessions/{session_id}/board"),
    ("get", "/api/v1/sessions/{session_id}/approvals"),
    ("post", "/api/v1/approvals/{approval_id}/decide"),
    ("get", "/api/v1/sessions/{session_id}/artifacts"),
    ("post", "/api/v1/sessions/{session_id}/artifacts"),
    ("get", "/api/v1/artifacts/{artifact_id}/download"),
    ("get", "/api/v1/sessions/{session_id}/events"),
    ("get", "/api/v1/sessions/{session_id}/frames"),
    ("get", "/api/v1/frames/{frame_id}"),
    ("get", "/api/v1/config/profiles"),
    ("get", "/api/v1/config/providers/health"),
    ("post", "/api/v1/sessions/{session_id}/ws-ticket"),
    ("get", "/api/v1/health"),
    ("get", "/api/v1/metrics"),
}

RESPONSE_MODELS = {
    ("post", "/api/v1/sessions"): ("201", "Session"),
    ("get", "/api/v1/sessions"): ("200", "SessionPage"),
    ("get", "/api/v1/sessions/{session_id}"): ("200", "SessionDetail"),
    ("put", "/api/v1/sessions/{session_id}/plan"): ("202", "CommandAccepted"),
    ("post", "/api/v1/sessions/{session_id}/steer"): ("202", "CommandAccepted"),
    ("post", "/api/v1/sessions/{session_id}/ws-ticket"): ("201", "WsTicket"),
    ("get", "/api/v1/sessions/{session_id}/board"): ("200", "BoardPage"),
    ("get", "/api/v1/config/profiles"): ("200", "ProfileList"),
}


async def _schema() -> dict[str, Any]:
    app = create_app(build_deps())
    async with client_for(app) as client:
        response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema: dict[str, Any] = response.json()
    return schema


async def test_openapi_paths_match_spec_section_16() -> None:
    schema = await _schema()
    found = {(m, p) for p, ops in schema["paths"].items() for m in ops}
    assert found == SPEC_ENDPOINTS


async def test_openapi_uses_contract_models() -> None:
    schema = await _schema()
    for (method, path), (status, model) in RESPONSE_MODELS.items():
        content = schema["paths"][path][method]["responses"][status]["content"]
        ref = content["application/json"]["schema"]["$ref"]
        assert ref.endswith(f"/{model}"), (path, ref)
    upload = schema["paths"]["/api/v1/sessions/{session_id}/artifacts"]["post"]
    assert "multipart/form-data" in upload["requestBody"]["content"]
