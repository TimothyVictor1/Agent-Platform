"""OpenAICompatibleClient against a mocked HTTP transport (no network)."""

from __future__ import annotations

import base64
import json
import math
from collections.abc import Callable
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
from agentplatform.errors import ModelError, ModelTimeoutError
from agentplatform.models.errors import RetryableModelError
from agentplatform.models.openai_compatible import OpenAICompatibleClient
from agentplatform.models.shared import SharedModelState

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "models"
BASE_URL = "http://z2.local:8081/v1"
JPEG = b"\xff\xd8\xff\xe0fake-jpeg"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class Server:
    """Replays queued responses and records every request."""

    def __init__(self, *responses: httpx2.Response | Callable[[httpx2.Request], httpx2.Response]):
        self.queue = list(responses)
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        nxt = self.queue.pop(0)
        return nxt(request) if callable(nxt) else nxt

    def body(self, i: int = -1) -> dict[str, Any]:
        data: dict[str, Any] = json.loads(self.requests[i].content)
        return data


def make_client(
    server: Server, *, shared: SharedModelState | None = None, api_key: str | None = "sk-test"
) -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        base_url=BASE_URL,
        model="qwen2.5-vl-7b",
        api_key=api_key,
        shared=shared,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(server)),
    )


CLICK = ToolSpec(
    name="click",
    description="Click.",
    parameters={"type": "object", "properties": {"x": {"type": "number"}}},
)


def screenshot_request(**kw: Any) -> ModelRequest:
    return ModelRequest(
        messages=[
            Message.text("system", "You operate a desktop."),
            Message(
                role="user",
                content=[
                    TextPart(text="Here is the screen."),
                    ImagePart(jpeg_bytes=JPEG, width=1280, height=800, detail="high"),
                ],
            ),
        ],
        **kw,
    )


async def test_text_reply() -> None:
    server = Server(httpx2.Response(200, json=fixture("openai_text.json")))
    client = make_client(server)
    resp = await client.complete(screenshot_request(max_output_tokens=64, temperature=0.2))

    assert resp.text == "The page shows a login form."
    assert resp.tool_calls == []
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.images) == (1234, 9, 1)
    assert resp.usage.cost_usd == 0
    assert resp.raw["id"] == "chatcmpl-text-1"

    req = server.requests[0]
    assert req.url == f"{BASE_URL}/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = server.body()
    assert body["model"] == "qwen2.5-vl-7b"
    assert body["max_tokens"] == 64
    assert body["temperature"] == 0.2
    assert "tools" not in body
    assert "tool_choice" not in body
    await client.aclose()


async def test_image_goes_into_user_message_as_data_url() -> None:
    server = Server(httpx2.Response(200, json=fixture("openai_text.json")))
    client = make_client(server)
    await client.complete(screenshot_request())

    messages = server.body()["messages"]
    assert messages[0] == {"role": "system", "content": "You operate a desktop."}
    user = messages[1]
    assert user["role"] == "user"
    assert user["content"][0] == {"type": "text", "text": "Here is the screen."}
    image = user["content"][1]
    assert image["type"] == "image_url"
    assert image["image_url"]["detail"] == "high"
    url = image["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == JPEG
    await client.aclose()


async def test_image_outside_user_message_is_rejected_without_a_request() -> None:
    server = Server()
    client = make_client(server)
    bad = Message.tool_result("call_1", "ok").model_copy(
        update={"content": [ImagePart(jpeg_bytes=JPEG, width=1, height=1)]}
    )
    req = ModelRequest(messages=[Message.text("user", "hi"), bad])
    with pytest.raises(ModelError, match="only allowed in user messages"):
        await client.complete(req)
    assert server.requests == []
    await client.aclose()


async def test_tool_call_reply_and_history_formatting() -> None:
    server = Server(httpx2.Response(200, json=fixture("openai_tool_call.json")))
    client = make_client(server)
    history = [
        Message.text("user", "Open the site."),
        Message(
            role="assistant",
            content=[TextPart(text="Clicking.")],
            tool_calls=[ToolCall(id="call_0", name="click", arguments={"x": 1, "y": 2})],
        ),
        Message.tool_result("call_0", "clicked left at (1, 2)"),
    ]
    resp = await client.complete(
        ModelRequest(messages=history, tools=[CLICK], tool_choice="required")
    )

    assert resp.text is None
    assert resp.tool_calls == [
        ToolCall(id="call_abc123", name="click", arguments={"x": 512, "y": 300})
    ]
    body = server.body()
    assert body["tool_choice"] == "required"
    assert body["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "click",
                "description": "Click.",
                "parameters": CLICK.parameters,
            },
        }
    ]
    assistant, tool = body["messages"][1], body["messages"][2]
    assert assistant["content"] == "Clicking."
    assert assistant["tool_calls"][0]["function"] == {
        "name": "click",
        "arguments": json.dumps({"x": 1, "y": 2}),
    }
    assert tool == {"role": "tool", "tool_call_id": "call_0", "content": "clicked left at (1, 2)"}
    await client.aclose()


async def test_multiple_tool_calls_keep_order_and_bad_json_is_raw() -> None:
    server = Server(httpx2.Response(200, json=fixture("openai_multi_tool_calls.json")))
    client = make_client(server)
    resp = await client.complete(ModelRequest(messages=[Message.text("user", "go")], tools=[CLICK]))

    assert resp.text == "Opening the address bar, then typing."
    assert [(c.id, c.name, c.arguments) for c in resp.tool_calls] == [
        ("call_1", "key", {"combo": "ctrl+l"}),
        ("call_2", "type_text", {"text": "example.com\n"}),
        ("call_3", "wait", {"_raw": '{"seconds": 2'}),
    ]
    assert resp.usage.images == 0
    await client.aclose()


async def test_required_tool_choice_falls_back_to_auto_and_is_cached() -> None:
    shared = SharedModelState()
    server = Server(
        httpx2.Response(400, json=fixture("openai_tool_choice_rejected.json")),
        httpx2.Response(200, json=fixture("openai_tool_call.json")),
        httpx2.Response(200, json=fixture("openai_tool_call.json")),
    )
    client = make_client(server, shared=shared)
    req = ModelRequest(messages=[Message.text("user", "go")], tools=[CLICK], tool_choice="required")

    first = await client.complete(req)
    assert first.tool_calls[0].name == "click"
    assert [server.body(i)["tool_choice"] for i in range(2)] == ["required", "auto"]
    assert shared.no_required_tool_choice == {BASE_URL}

    # A second client for the same server (another role) skips the failing attempt.
    other = make_client(server, shared=shared)
    await other.complete(req)
    assert len(server.requests) == 3
    assert server.body()["tool_choice"] == "auto"
    await client.aclose()
    await other.aclose()


async def test_tool_choice_not_cached_when_auto_also_fails() -> None:
    shared = SharedModelState()
    server = Server(
        httpx2.Response(400, json=fixture("openai_tool_choice_rejected.json")),
        httpx2.Response(400, json=fixture("openai_tool_choice_rejected.json")),
    )
    client = make_client(server, shared=shared)
    req = ModelRequest(messages=[Message.text("user", "go")], tools=[CLICK], tool_choice="required")
    with pytest.raises(ModelError, match="HTTP 400") as info:
        await client.complete(req)
    assert not isinstance(info.value, RetryableModelError)
    assert shared.no_required_tool_choice == set()
    await client.aclose()


async def test_json_response_format() -> None:
    server = Server(httpx2.Response(200, json=fixture("openai_text.json")))
    client = make_client(server)
    await client.complete(
        ModelRequest(messages=[Message.text("user", "plan")], response_format="json")
    )
    assert server.body()["response_format"] == {"type": "json_object"}
    await client.aclose()


async def test_429_maps_to_retryable_with_retry_after() -> None:
    server = Server(
        httpx2.Response(429, json=fixture("openai_rate_limited.json"), headers={"retry-after": "2"})
    )
    client = make_client(server)
    with pytest.raises(RetryableModelError) as info:
        await client.complete(ModelRequest(messages=[Message.text("user", "x")]))
    assert info.value.http_status == 429
    assert info.value.retry_after == 2.0
    assert len(server.requests) == 1  # SDK retries are disabled
    await client.aclose()


@pytest.mark.parametrize(
    ("status", "retryable"), [(500, True), (503, True), (401, False), (404, False)]
)
async def test_status_mapping(status: int, retryable: bool) -> None:
    server = Server(httpx2.Response(status, json={"error": {"message": "nope"}}))
    client = make_client(server)
    with pytest.raises(ModelError) as info:
        await client.complete(ModelRequest(messages=[Message.text("user", "x")]))
    assert isinstance(info.value, RetryableModelError) is retryable
    assert "sk-test" not in str(info.value)
    await client.aclose()


async def test_timeout_maps_to_model_timeout() -> None:
    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out", request=request)

    client = make_client(Server(slow))
    with pytest.raises(ModelTimeoutError):
        await client.complete(ModelRequest(messages=[Message.text("user", "x")], timeout_s=1))
    await client.aclose()


async def test_connection_error_is_retryable() -> None:
    def down(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused", request=request)

    client = make_client(Server(down))
    with pytest.raises(RetryableModelError):
        await client.complete(ModelRequest(messages=[Message.text("user", "x")]))
    await client.aclose()


async def test_garbage_body_is_model_error() -> None:
    client = make_client(Server(httpx2.Response(200, json={"unexpected": True})))
    with pytest.raises(ModelError, match="no choices"):
        await client.complete(ModelRequest(messages=[Message.text("user", "x")]))
    await client.aclose()


async def test_no_key_sends_placeholder_not_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-never-leave")
    server = Server(httpx2.Response(200, json=fixture("openai_text.json")))
    client = make_client(server, api_key=None)
    await client.complete(ModelRequest(messages=[Message.text("user", "x")]))
    assert "sk-should-never-leave" not in server.requests[0].headers["authorization"]
    await client.aclose()


async def test_health_ok_and_failure() -> None:
    server = Server(
        httpx2.Response(200, json=fixture("openai_models.json")),
        httpx2.Response(401, json={"error": {"message": "bad key"}}),
    )
    client = make_client(server)
    ok = await client.health()
    assert ok.ok is True
    assert ok.latency_ms is not None
    assert server.requests[0].url == f"{BASE_URL}/models"
    assert server.requests[0].method == "GET"

    bad = await client.health()
    assert bad.ok is False
    assert bad.error is not None
    assert "401" in bad.error
    await client.aclose()
    await client.aclose()  # idempotent


def test_name_label() -> None:
    client = make_client(Server())
    assert client.name == "openai_compatible:qwen2.5-vl-7b@z2.local:8081"


@pytest.mark.parametrize(
    "value", [b"raw", {1, 2}, math.inf, math.nan], ids=["bytes", "set", "inf", "nan"]
)
async def test_non_json_tool_arguments_are_a_model_error_without_a_request(value: Any) -> None:
    server = Server()
    client = make_client(server)
    call = ToolCall(id="call_1", name="click", arguments={"x": value})
    req = ModelRequest(
        messages=[
            Message.text("user", "hi"),
            Message(role="assistant", content=[], tool_calls=[call]),
            Message.tool_result("call_1", "ok"),
        ]
    )
    with pytest.raises(ModelError, match=r"tool call .* has non-JSON arguments"):
        await client.complete(req)
    assert server.requests == []
    await client.aclose()


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning"])
async def test_separate_reasoning_is_kept(field: str) -> None:
    """T13: Nemotron/GLM on Nebius answer with ``reasoning_content`` next to the tool
    call and no ``content``; the client used to drop it, so every step's excerpt was
    empty."""
    body = {
        "id": "chatcmpl-r-1",
        "object": "chat.completion",
        "created": 0,
        "model": "nemotron",
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    field: "  The Continue button is in the centre.  ",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "click", "arguments": '{"x": 499, "y": 470}'},
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 1332, "completion_tokens": 90, "total_tokens": 1422},
    }
    client = make_client(Server(httpx2.Response(200, json=body)))
    resp = await client.complete(screenshot_request(tools=[CLICK]))
    assert resp.text is None
    assert resp.reasoning == "The Continue button is in the centre."
    assert [c.name for c in resp.tool_calls] == ["click"]
    await client.aclose()


async def test_no_reasoning_field_means_none() -> None:
    client = make_client(Server(httpx2.Response(200, json=fixture("openai_text.json"))))
    resp = await client.complete(screenshot_request())
    assert resp.reasoning is None
    await client.aclose()
