"""AnthropicClient against a mocked HTTP transport (no network)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from agentplatform.contracts.model_io import (
    ImagePart,
    Message,
    ModelRequest,
    TextPart,
    ToolCall,
    ToolSpec,
)
from agentplatform.errors import ModelError
from agentplatform.models.anthropic_client import JSON_HINT, AnthropicClient
from agentplatform.models.errors import RetryableModelError
from agentplatform.models.shared import SharedModelState

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "models"
JPEG = b"\xff\xd8\xff\xe0anthropic-jpeg"
KEY = "sk-ant-test-key"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class Server:
    def __init__(self, *responses: httpx2.Response) -> None:
        self.queue = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.queue.pop(0)

    def body(self, i: int = -1) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.requests[i].content)
        return data


def make_client(server: Server, shared: SharedModelState | None = None) -> AnthropicClient:
    return AnthropicClient(
        model="claude-sonnet-5-5",
        api_key=KEY,
        shared=shared,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(server)),
    )


CLICK = ToolSpec(
    name="click",
    description="Click.",
    parameters={"type": "object", "properties": {"x": {"type": "number"}}},
)


async def test_text_reply_and_system_is_separate() -> None:
    server = Server(httpx2.Response(200, json=fixture("anthropic_text.json")))
    client = make_client(server)
    resp = await client.complete(
        ModelRequest(
            messages=[
                Message.text("system", "You are the planner."),
                Message.text("system", "Be brief."),
                Message.text("user", "Plan it."),
            ],
            max_output_tokens=200,
        )
    )
    assert resp.text == "Plan: open the browser first."
    assert resp.tool_calls == []
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (800, 12)

    req = server.requests[0]
    assert req.url.path == "/v1/messages"
    assert req.headers["x-api-key"] == KEY
    body = server.body()
    assert body["system"] == "You are the planner.\n\nBe brief."
    assert body["messages"] == [{"role": "user", "content": [{"type": "text", "text": "Plan it."}]}]
    assert body["max_tokens"] == 200
    assert "temperature" not in body  # the Messages API rejects sampling params
    assert "tools" not in body
    await client.aclose()


async def test_image_is_a_base64_block_in_the_user_turn() -> None:
    server = Server(httpx2.Response(200, json=fixture("anthropic_text.json")))
    client = make_client(server)
    await client.complete(
        ModelRequest(
            messages=[
                Message(
                    role="user",
                    content=[
                        TextPart(text="Screen:"),
                        ImagePart(jpeg_bytes=JPEG, width=1280, height=800),
                    ],
                )
            ]
        )
    )
    content = server.body()["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "Screen:"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(content[1]["source"]["data"]) == JPEG
    await client.aclose()


async def test_tool_use_mapping_and_tool_results() -> None:
    server = Server(httpx2.Response(200, json=fixture("anthropic_tool_use.json")))
    client = make_client(server)
    history = [
        Message.text("user", "Search the weather."),
        Message(
            role="assistant",
            content=[TextPart(text="Two steps.")],
            tool_calls=[
                ToolCall(id="toolu_00A", name="key", arguments={"combo": "ctrl+l"}),
                ToolCall(id="toolu_00B", name="wait", arguments={"seconds": 1}),
            ],
        ),
        Message.tool_result("toolu_00A", "pressed ctrl+l"),
        Message.tool_result("toolu_00B", "waited 1 s"),
        Message(
            role="user",
            content=[TextPart(text="New screen:"), ImagePart(jpeg_bytes=JPEG, width=8, height=8)],
        ),
    ]
    resp = await client.complete(
        ModelRequest(messages=history, tools=[CLICK], tool_choice="required")
    )

    assert resp.text == "I'll click the search box, then type."
    assert resp.tool_calls == [
        ToolCall(id="toolu_01A", name="click", arguments={"x": 640, "y": 120}),
        ToolCall(id="toolu_01B", name="type_text", arguments={"text": "weather"}),
    ]
    assert resp.usage.input_tokens == 1500 + 100 + 400
    assert resp.usage.output_tokens == 60
    assert resp.usage.images == 1

    body = server.body()
    assert body["tool_choice"] == {"type": "any"}
    assert body["tools"] == [
        {"name": "click", "description": "Click.", "input_schema": CLICK.parameters}
    ]
    msgs = body["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]
    assert msgs[1]["content"] == [
        {"type": "text", "text": "Two steps."},
        {"type": "tool_use", "id": "toolu_00A", "name": "key", "input": {"combo": "ctrl+l"}},
        {"type": "tool_use", "id": "toolu_00B", "name": "wait", "input": {"seconds": 1}},
    ]
    # Both results, then the new observation, in ONE user turn; results first.
    user_turn = msgs[2]["content"]
    assert user_turn[0] == {
        "type": "tool_result",
        "tool_use_id": "toolu_00A",
        "content": "pressed ctrl+l",
    }
    assert user_turn[1]["tool_use_id"] == "toolu_00B"
    assert user_turn[2] == {"type": "text", "text": "New screen:"}
    assert user_turn[3]["type"] == "image"
    await client.aclose()


@pytest.mark.parametrize(
    ("choice", "expected"),
    [("auto", {"type": "auto"}), ("required", {"type": "any"}), ("none", {"type": "none"})],
)
async def test_tool_choice_mapping(choice: Any, expected: dict[str, str]) -> None:
    server = Server(httpx2.Response(200, json=fixture("anthropic_text.json")))
    client = make_client(server)
    await client.complete(
        ModelRequest(messages=[Message.text("user", "x")], tools=[CLICK], tool_choice=choice)
    )
    assert server.body()["tool_choice"] == expected
    await client.aclose()


FORCED_REJECTED = {
    "type": "error",
    "error": {
        "type": "invalid_request_error",
        "message": 'tool_choice: type "tool" and "any" are not supported for this model.',
    },
}


async def test_forced_tool_choice_rejected_falls_back_to_auto_and_is_cached() -> None:
    shared = SharedModelState()
    server = Server(
        httpx2.Response(400, json=FORCED_REJECTED),
        httpx2.Response(200, json=fixture("anthropic_tool_use.json")),
        httpx2.Response(200, json=fixture("anthropic_tool_use.json")),
    )
    client = make_client(server, shared=shared)
    req = ModelRequest(messages=[Message.text("user", "x")], tools=[CLICK], tool_choice="required")

    resp = await client.complete(req)
    assert [c.name for c in resp.tool_calls] == ["click", "type_text"]
    assert [server.body(i)["tool_choice"] for i in range(2)] == [{"type": "any"}, {"type": "auto"}]
    assert shared.no_required_tool_choice == {"anthropic:claude-sonnet-5-5"}

    await client.complete(req)
    assert len(server.requests) == 3
    assert server.body()["tool_choice"] == {"type": "auto"}
    await client.aclose()


async def test_other_400_with_auto_choice_is_not_retried() -> None:
    shared = SharedModelState()
    server = Server(httpx2.Response(400, json=FORCED_REJECTED))
    client = make_client(server, shared=shared)
    with pytest.raises(ModelError, match="HTTP 400"):
        await client.complete(
            ModelRequest(messages=[Message.text("user", "x")], tools=[CLICK], tool_choice="auto")
        )
    assert len(server.requests) == 1
    assert shared.no_required_tool_choice == set()
    await client.aclose()


async def test_json_hint_is_appended_to_system() -> None:
    server = Server(httpx2.Response(200, json=fixture("anthropic_text.json")))
    client = make_client(server)
    await client.complete(
        ModelRequest(
            messages=[Message.text("system", "Plan."), Message.text("user", "x")],
            response_format="json",
        )
    )
    assert server.body()["system"] == f"Plan.\n\n{JSON_HINT}"
    await client.aclose()


async def test_image_in_assistant_message_is_rejected() -> None:
    server = Server()
    client = make_client(server)
    bad = Message(role="assistant", content=[ImagePart(jpeg_bytes=JPEG, width=1, height=1)])
    with pytest.raises(ModelError, match="only allowed in user messages"):
        await client.complete(ModelRequest(messages=[Message.text("user", "x"), bad]))
    assert server.requests == []
    await client.aclose()


async def test_overloaded_is_retryable_and_key_not_in_message() -> None:
    server = Server(
        httpx2.Response(
            529, json=fixture("anthropic_overloaded.json"), headers={"retry-after": "3"}
        )
    )
    client = make_client(server)
    with pytest.raises(RetryableModelError) as info:
        await client.complete(ModelRequest(messages=[Message.text("user", "x")]))
    assert info.value.http_status == 529
    assert info.value.retry_after == 3.0
    assert KEY not in str(info.value)
    assert len(server.requests) == 1
    await client.aclose()


async def test_bad_request_is_not_retryable() -> None:
    server = Server(
        httpx2.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}},
        )
    )
    client = make_client(server)
    with pytest.raises(ModelError) as info:
        await client.complete(ModelRequest(messages=[Message.text("user", "x")]))
    assert not isinstance(info.value, RetryableModelError)
    await client.aclose()


async def test_health() -> None:
    server = Server(
        httpx2.Response(
            200,
            json={
                "type": "model",
                "id": "claude-sonnet-5-5",
                "display_name": "Claude",
                "created_at": "2026-01-01T00:00:00Z",
            },
        ),
        httpx2.Response(
            404,
            json={"type": "error", "error": {"type": "not_found_error", "message": "no model"}},
        ),
    )
    client = make_client(server)
    assert (await client.health()).ok is True
    assert server.requests[0].url.path == "/v1/models/claude-sonnet-5-5"
    bad = await client.health()
    assert bad.ok is False
    assert bad.error is not None
    assert "404" in bad.error
    await client.aclose()
    await client.aclose()
