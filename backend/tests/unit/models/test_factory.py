"""build_client: provider selection, key handling and wrapper stack (no network)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from agentplatform.config import ModelPrice, RoleConfig
from agentplatform.contracts.model_io import Message, ModelRequest
from agentplatform.errors import ConfigError
from agentplatform.models import build_client, parse_text_tool_calls
from agentplatform.models.anthropic_client import AnthropicClient
from agentplatform.models.base import ModelClient
from agentplatform.models.factory import concurrency_key
from agentplatform.models.openai_compatible import OpenAICompatibleClient
from agentplatform.models.shared import SharedModelState
from agentplatform.models.wrappers import (
    AccountingClient,
    ConcurrencyLimitedClient,
    DebugLoggingClient,
    RetryingClient,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "models"

LOCAL = RoleConfig(
    provider="openai_compatible",
    base_url="http://z2.local:8081/v1",
    model="qwen2.5-vl-7b",
    vision=True,
    concurrency=2,
)
CLOUD = RoleConfig(provider="anthropic", model="claude-sonnet-5-5", concurrency=6)


def chain(client: Any) -> list[type]:
    types: list[type] = []
    while True:
        types.append(type(client))
        inner = getattr(client, "inner", None)
        if inner is None:
            return types
        client = inner


def test_openai_compatible_stack_without_key() -> None:
    client = build_client(LOCAL, {}, env={})
    assert isinstance(client, ModelClient)
    assert chain(client) == [
        AccountingClient,
        RetryingClient,
        ConcurrencyLimitedClient,
        OpenAICompatibleClient,
    ]
    assert client.name == "openai_compatible:qwen2.5-vl-7b@z2.local:8081"


def test_debug_dir_adds_logging_outermost(tmp_path: Path) -> None:
    client = build_client(LOCAL, {}, env={}, debug_dir=tmp_path)
    assert chain(client)[0] is DebugLoggingClient


def test_anthropic_uses_default_key_env() -> None:
    client = build_client(CLOUD, {}, env={"ANTHROPIC_API_KEY": "sk-ant-x"})
    assert chain(client)[-1] is AnthropicClient


def test_missing_key_is_a_config_error_naming_only_the_variable() -> None:
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY"):
        build_client(CLOUD, {}, env={})
    role = LOCAL.model_copy(update={"api_key_env": "LOCAL_LLM_KEY"})
    with pytest.raises(ConfigError, match="LOCAL_LLM_KEY"):
        build_client(role, {}, env={"LOCAL_LLM_KEY": "  "})


def test_semaphores_are_shared_per_server() -> None:
    shared = SharedModelState()
    planner = LOCAL.model_copy(
        update={"model": "other-model", "base_url": "http://z2.local:8081/v1/"}
    )
    build_client(LOCAL, {}, env={}, shared=shared)
    build_client(planner, {}, env={}, shared=shared)
    assert concurrency_key(LOCAL) == concurrency_key(planner)
    assert concurrency_key(CLOUD) == "anthropic:default:claude-sonnet-5-5"
    assert list(shared._semaphores) == [concurrency_key(LOCAL)]


async def test_end_to_end_cost_and_key_header() -> None:
    data = json.loads((FIXTURES / "openai_tool_call.json").read_text())
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(200, json=data)

    role = LOCAL.model_copy(update={"api_key_env": "OPENROUTER_API_KEY"})
    client = build_client(
        role,
        {"qwen2.5-vl-7b": ModelPrice(input=1.0, output=2.0)},
        env={"OPENROUTER_API_KEY": "sk-or-123"},
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    resp = await client.complete(ModelRequest(messages=[Message.text("user", "go")]))
    assert seen[0].headers["authorization"] == "Bearer sk-or-123"
    assert resp.usage.cost_usd == pytest.approx(2000 * 1.0 / 1e6 + 20 * 2.0 / 1e6)
    await client.aclose()


def test_package_exports() -> None:
    assert callable(parse_text_tool_calls)
