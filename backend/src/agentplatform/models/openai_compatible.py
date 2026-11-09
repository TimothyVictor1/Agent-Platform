"""``openai_compatible`` provider: any server speaking the chat.completions API.

Covers llama.cpp server, Ollama, vLLM, LM Studio, OpenRouter and Gemini's
OpenAI-compatible endpoint (see docs/models.md). Uses the official ``AsyncOpenAI``
client with SDK retries disabled (``RetryingClient`` owns retries).

Mapping (SPEC §8.1):

* ``ImagePart`` -> ``{"type": "image_url", "image_url": {"url": "data:image/jpeg;
  base64,...", "detail": ...}}`` inside **user** messages only; images anywhere else
  are rejected with ``ModelError`` before any network call.
* ``tool`` messages -> ``{"role": "tool", "tool_call_id", "content"}``.
* ``tool_choice="required"``: many local servers reject it. On a 400/422 the request
  is retried once with ``"auto"``; if that works the base URL is remembered in
  ``SharedModelState.no_required_tool_choice`` and later requests go straight to
  ``"auto"``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx2
import openai
import structlog

from agentplatform.contracts.common import HealthStatus
from agentplatform.contracts.model_io import (
    Message,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    TextPart,
    ToolCall,
)
from agentplatform.errors import ModelError
from agentplatform.models._common import (
    b64,
    check_images_only_in_user,
    count_images,
    dump_arguments,
    elapsed_ms,
    health_failure,
    host_label,
    parse_arguments,
)
from agentplatform.models.errors import map_sdk_error
from agentplatform.models.shared import SharedModelState

__all__ = ["OpenAICompatibleClient", "to_openai_messages", "to_openai_tools"]

log = structlog.get_logger(__name__)

PROVIDER = "openai_compatible"
NO_KEY_PLACEHOLDER = "not-needed"
"""Sent when the role has no ``api_key_env``. Passing it explicitly also stops the SDK
from picking up ``OPENAI_API_KEY`` and sending it to an unrelated (local) server."""


def _user_content(msg: Message) -> str | list[dict[str, Any]]:
    if not msg.images():
        return msg.text_content()
    parts: list[dict[str, Any]] = []
    for part in msg.content:
        if isinstance(part, TextPart):
            parts.append({"type": "text", "text": part.text})
        else:
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/jpeg;base64,{b64(part)}",
                        "detail": part.detail,
                    },
                }
            )
    return parts


def _tool_call_out(call: ToolCall) -> dict[str, Any]:
    return {
        "id": call.id,
        "type": "function",
        "function": {"name": call.name, "arguments": dump_arguments(call)},
    }


def to_openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        check_images_only_in_user(msg)
        if msg.role == "user":
            out.append({"role": "user", "content": _user_content(msg)})
        elif msg.role == "system":
            out.append({"role": "system", "content": msg.text_content()})
        elif msg.role == "assistant":
            item: dict[str, Any] = {"role": "assistant", "content": msg.text_content() or None}
            if msg.tool_calls:
                item["tool_calls"] = [_tool_call_out(c) for c in msg.tool_calls]
            out.append(item)
        else:
            if not msg.tool_call_id:
                raise ModelError("tool messages need tool_call_id")
            out.append(
                {"role": "tool", "tool_call_id": msg.tool_call_id, "content": msg.text_content()}
            )
    return out


def to_openai_tools(req: ModelRequest) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
        }
        for t in req.tools or []
    ]


def _parse_response(data: dict[str, Any], req: ModelRequest, latency_ms: int) -> ModelResponse:
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise ModelError(f"{PROVIDER}: response has no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some servers return content parts
        content = "".join(
            p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
        )
    text = content if isinstance(content, str) and content != "" else None

    calls: list[ToolCall] = []
    for i, raw in enumerate(message.get("tool_calls") or []):
        fn = raw.get("function") or {}
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        call_id = raw.get("id") or f"call_{i}_{time.monotonic_ns()}"
        calls.append(
            ToolCall(id=str(call_id), name=name, arguments=parse_arguments(fn.get("arguments")))
        )

    usage = data.get("usage") or {}
    return ModelResponse(
        text=text,
        tool_calls=calls,
        usage=ModelUsage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            images=count_images(req),
        ),
        raw=data,
        latency_ms=latency_ms,
    )


class OpenAICompatibleClient:
    """``ModelClient`` for chat.completions servers. Safe for concurrent use."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        shared: SharedModelState | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._shared = shared or SharedModelState()
        self._client = openai.AsyncOpenAI(
            base_url=self._base_url,
            api_key=api_key or NO_KEY_PLACEHOLDER,
            max_retries=0,
            http_client=http_client,
        )
        self._name = f"{PROVIDER}:{model}@{host_label(self._base_url)}"
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    def _body(self, req: ModelRequest, tool_choice: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": to_openai_messages(req.messages),
            "max_tokens": req.max_output_tokens,
            "temperature": req.temperature,
        }
        if req.tools:
            body["tools"] = to_openai_tools(req)
            body["tool_choice"] = tool_choice
        if req.response_format == "json":
            body["response_format"] = {"type": "json_object"}
        return body

    async def _create(self, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        async with asyncio.timeout(timeout_s):
            resp = await self._client.chat.completions.create(**body, timeout=timeout_s)
        data: dict[str, Any] = resp.model_dump(mode="json", exclude_none=True)
        return data

    async def complete(self, req: ModelRequest) -> ModelResponse:
        choice: str = req.tool_choice
        downgraded = False
        if choice == "required" and self._base_url in self._shared.no_required_tool_choice:
            choice, downgraded = "auto", True
        body = self._body(req, choice)  # raises ModelError on misplaced images
        deadline = time.monotonic() + req.timeout_s
        started = time.perf_counter()
        try:
            try:
                data = await self._create(body, req.timeout_s)
            except openai.APIStatusError as err:
                if not (
                    err.status_code in (400, 422)
                    and "tool_choice" in body
                    and body["tool_choice"] == "required"
                    and not downgraded
                ):
                    raise
                log.info(
                    "model.tool_choice_required_rejected",
                    client=self._name,
                    status=err.status_code,
                )
                body["tool_choice"] = "auto"
                started = time.perf_counter()
                data = await self._create(body, max(0.001, deadline - time.monotonic()))
                self._shared.no_required_tool_choice.add(self._base_url)
        except (openai.OpenAIError, TimeoutError, ValueError) as err:
            # ValueError: malformed JSON or an invalid shape from a compatible server.
            raise map_sdk_error(self._name, err) from None
        try:
            return _parse_response(data, req, elapsed_ms(started))
        except (TypeError, ValueError, AttributeError) as err:
            raise ModelError(f"{self._name}: unexpected response shape") from err

    async def health(self) -> HealthStatus:
        started = time.perf_counter()
        try:
            await self._client.models.list()
        except Exception as err:
            return health_failure(started, map_sdk_error(self._name, err))
        return HealthStatus(ok=True, latency_ms=elapsed_ms(started))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.close()
