"""Secrets echoed by providers never reach errors, health status or debug logs (T02 review)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx2
import pytest

from agentplatform.contracts.common import HealthStatus
from agentplatform.contracts.model_io import Message, ModelRequest, ModelResponse
from agentplatform.errors import ModelError
from agentplatform.models.anthropic_client import AnthropicClient
from agentplatform.models.errors import redact_secrets
from agentplatform.models.openai_compatible import OpenAICompatibleClient
from agentplatform.models.wrappers import DebugLoggingClient

SECRET = "sk-secret-value-123"
ECHO_401 = {"error": {"message": f"Incorrect API key provided: {SECRET}. Check your key."}}
ANTHROPIC_401 = {
    "type": "error",
    "error": {"type": "authentication_error", "message": f"invalid x-api-key: {SECRET}"},
}


def echo_server(body: dict[str, Any]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda r: httpx2.Response(401, json=body))
    )


def openai_client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url="https://llm.example/v1",
        model="m",
        api_key=SECRET,
        http_client=echo_server(ECHO_401),
    )


def anthropic_client() -> AnthropicClient:
    return AnthropicClient(
        model="claude-sonnet-5-5", api_key=SECRET, http_client=echo_server(ANTHROPIC_401)
    )


MakeClient = Callable[[], OpenAICompatibleClient | AnthropicClient]
REQ = ModelRequest(messages=[Message.text("user", "hi")])


@pytest.mark.parametrize("make", [openai_client, anthropic_client], ids=["openai", "anthropic"])
async def test_401_echoing_the_key_is_redacted_in_the_error(
    make: MakeClient,
) -> None:
    client = make()
    with pytest.raises(ModelError) as info:
        await client.complete(REQ)
    assert "401" in info.value.detail
    assert SECRET not in info.value.detail
    assert SECRET not in str(info.value)
    await client.aclose()


@pytest.mark.parametrize("make", [openai_client, anthropic_client], ids=["openai", "anthropic"])
async def test_401_echoing_the_key_is_redacted_in_health(
    make: MakeClient,
) -> None:
    client = make()
    status = await client.health()
    assert status.ok is False
    assert status.error is not None
    assert "401" in status.error
    assert SECRET not in status.error
    await client.aclose()


async def test_401_echoing_the_key_is_redacted_in_the_debug_log(tmp_path: Path) -> None:
    client = DebugLoggingClient(openai_client(), tmp_path)
    with pytest.raises(ModelError):
        await client.complete(REQ)
    text = "".join(p.read_text() for p in sorted(tmp_path.iterdir()))  # noqa: ASYNC240
    assert SECRET not in text
    assert "401" in json.loads(text)["error"]["message"]
    await client.aclose()


class RaisesRaw:
    """An inner client whose error text was not built by ``map_sdk_error``."""

    name = "raw"

    async def complete(self, req: ModelRequest) -> ModelResponse:
        raise RuntimeError(f"upstream said Authorization: Bearer {SECRET} api_key={SECRET}")

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True)

    async def aclose(self) -> None:
        return None


async def test_debug_log_redacts_any_error_message(tmp_path: Path) -> None:
    client = DebugLoggingClient(RaisesRaw(), tmp_path)
    with pytest.raises(RuntimeError):
        await client.complete(REQ)
    text = "".join(p.read_text() for p in sorted(tmp_path.iterdir()))  # noqa: ASYNC240
    assert SECRET not in text
    assert "secret-value" not in text


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        ("Incorrect API key provided: sk-proj-AbC123_xyz-789", "AbC123_xyz-789"),
        ("bad key sk-ant-api03-ZZZZZZZZZZ", "api03-ZZZZZZZZZZ"),
        ("Authorization: Bearer eyJhbGciOi.payload.sig", "eyJhbGciOi"),
        ("authorization=Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("header bearer abc.def~ghi", "abc.def~ghi"),
        ("GET /v1/models?key=AIzaSyA-1234567890abcdefghijklmnop", "AIzaSyA-1234567890"),
        ("api_key=hunter2&model=x", "hunter2"),
        ('{"api_key": "hunter2"}', "hunter2"),
        ("x-api-key: hunter2", "hunter2"),
        ("apiKey='hunter2'", "hunter2"),
        ("access_token=tok123 refused", "tok123"),
        ("password: hunter2", "hunter2"),
    ],
)
def test_redact_secrets_patterns(text: str, leak: str) -> None:
    out = redact_secrets(text)
    assert leak not in out
    assert "[redacted]" in out


@pytest.mark.parametrize(
    "text",
    ["HTTP 429: rate limited, max_tokens is 4096", "task-12345678 failed", "model not found"],
)
def test_redact_secrets_leaves_ordinary_text_alone(text: str) -> None:
    assert redact_secrets(text) == text
