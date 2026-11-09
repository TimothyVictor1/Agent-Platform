"""``anthropic`` provider: the native Messages API via the official SDK.

Mapping (SPEC §8.1):

* ``system`` messages -> the separate ``system`` parameter (joined with blank lines).
* ``ImagePart`` -> ``{"type": "image", "source": {"type": "base64", "media_type":
  "image/jpeg", "data": ...}}`` in user turns only.
* assistant ``tool_calls`` -> ``tool_use`` blocks; ``tool`` messages ->
  ``tool_result`` blocks in a user turn. Consecutive messages of the same role are
  merged into one turn (the API requires alternating roles and all results for one
  assistant turn in the following user turn).
* ``tool_choice``: ``auto`` -> ``{"type": "auto"}``, ``required`` -> ``{"type":
  "any"}``, ``none`` -> ``{"type": "none"}``. Current models (Opus 5.5, Sonnet 5.5,
  Fable 5.1) reject forced tool use with a 400; the request is then retried once with
  ``auto`` and the model is remembered in ``SharedModelState`` (key
  ``anthropic:<model>``), as ``models/base.py`` allows.
* ``req.temperature`` is not sent: the Messages API rejects sampling parameters
  (``temperature``/``top_p``/``top_k``) on every model and SDK 1.x removed them.
* ``response_format="json"`` is a hint: one line is appended to the system prompt.
* Thinking blocks in responses are ignored (the provider-agnostic ``Message`` has no
  place to carry them back; see docs/models.md).

SDK retries are disabled (``RetryingClient`` owns retries). Native computer-use
tools are not used; the generic tool path is the default (SPEC §8.1).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import anthropic
import httpx2
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
    elapsed_ms,
    health_failure,
    host_label,
    parse_arguments,
)
from agentplatform.models.errors import map_sdk_error
from agentplatform.models.shared import SharedModelState

__all__ = ["AnthropicClient", "to_anthropic_messages"]

log = structlog.get_logger(__name__)

PROVIDER = "anthropic"
JSON_HINT = "Respond with a single JSON object and nothing else."
_TOOL_CHOICE = {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}


def _blocks(msg: Message) -> list[dict[str, Any]]:
    if msg.role == "tool":
        if not msg.tool_call_id:
            raise ModelError("tool messages need tool_call_id")
        return [
            {"type": "tool_result", "tool_use_id": msg.tool_call_id, "content": msg.text_content()}
        ]
    blocks: list[dict[str, Any]] = []
    for part in msg.content:
        if isinstance(part, TextPart):
            if part.text:
                blocks.append({"type": "text", "text": part.text})
        else:
            blocks.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64(part)},
                }
            )
    for call in msg.tool_calls or []:
        blocks.append(
            {"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
        )
    return blocks


def to_anthropic_messages(messages: list[Message]) -> tuple[str | None, list[dict[str, Any]]]:
    """``(system, messages)`` in Messages API format."""
    system: list[str] = []
    out: list[dict[str, Any]] = []
    for msg in messages:
        check_images_only_in_user(msg)
        if msg.role == "system":
            if text := msg.text_content():
                system.append(text)
            continue
        role = "assistant" if msg.role == "assistant" else "user"
        blocks = _blocks(msg)
        if not blocks:
            continue  # the API rejects empty content
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})
    return ("\n\n".join(system) or None), out


def _parse_response(data: dict[str, Any], req: ModelRequest, latency_ms: int) -> ModelResponse:
    content = data.get("content")
    if not isinstance(content, list):
        raise ModelError(f"{PROVIDER}: response has no content")
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in content:
        kind = block.get("type")
        if kind == "text" and block.get("text"):
            texts.append(block["text"])
        elif kind == "tool_use":
            calls.append(
                ToolCall(
                    id=str(block["id"]),
                    name=str(block["name"]),
                    arguments=parse_arguments(block.get("input")),
                )
            )
    usage = data.get("usage") or {}
    input_tokens = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    )
    return ModelResponse(
        text="\n".join(texts) or None,
        tool_calls=calls,
        usage=ModelUsage(
            input_tokens=input_tokens,
            output_tokens=int(usage.get("output_tokens") or 0),
            images=count_images(req),
        ),
        raw=data,
        latency_ms=latency_ms,
    )


class AnthropicClient:
    """``ModelClient`` for Anthropic's Messages API. Safe for concurrent use."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        shared: SharedModelState | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._model = model
        self._shared = shared or SharedModelState()
        self._choice_key = f"{PROVIDER}:{model}"
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, max_retries=0, http_client=http_client
        )
        self._name = f"{PROVIDER}:{model}@{host_label(base_url)}"
        self._closed = False

    @property
    def name(self) -> str:
        return self._name

    def _body(self, req: ModelRequest) -> dict[str, Any]:
        system, messages = to_anthropic_messages(req.messages)
        if req.response_format == "json":
            system = f"{system}\n\n{JSON_HINT}" if system else JSON_HINT
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "max_tokens": req.max_output_tokens,
        }
        if system:
            body["system"] = system
        if req.tools:
            body["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in req.tools
            ]
            choice = req.tool_choice
            if choice == "required" and self._choice_key in self._shared.no_required_tool_choice:
                choice = "auto"
            body["tool_choice"] = _TOOL_CHOICE[choice]
        return body

    async def _create(self, body: dict[str, Any], timeout_s: float) -> dict[str, Any]:
        async with asyncio.timeout(timeout_s):
            resp = await self._client.messages.create(**body, timeout=timeout_s)
        data: dict[str, Any] = resp.model_dump(mode="json", exclude_none=True)
        return data

    async def complete(self, req: ModelRequest) -> ModelResponse:
        body = self._body(req)  # raises ModelError on misplaced images
        deadline = time.monotonic() + req.timeout_s
        started = time.perf_counter()
        try:
            try:
                data = await self._create(body, req.timeout_s)
            except anthropic.APIStatusError as err:
                if not (err.status_code == 400 and body.get("tool_choice") == {"type": "any"}):
                    raise
                log.info("model.tool_choice_required_rejected", client=self._name)
                body["tool_choice"] = _TOOL_CHOICE["auto"]
                started = time.perf_counter()
                data = await self._create(body, max(0.001, deadline - time.monotonic()))
                self._shared.no_required_tool_choice.add(self._choice_key)
        except (anthropic.AnthropicError, TimeoutError, ValueError) as err:
            raise map_sdk_error(self._name, err) from None
        try:
            return _parse_response(data, req, elapsed_ms(started))
        except (TypeError, ValueError, KeyError, AttributeError) as err:
            raise ModelError(f"{self._name}: unexpected response shape") from err

    async def health(self) -> HealthStatus:
        """``GET /v1/models/{model}``: checks key, reachability and the model id, free."""
        started = time.perf_counter()
        try:
            await self._client.models.retrieve(self._model)
        except Exception as err:
            return health_failure(started, map_sdk_error(self._name, err))
        return HealthStatus(ok=True, latency_ms=elapsed_ms(started))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.close()
