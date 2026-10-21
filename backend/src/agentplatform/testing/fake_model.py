"""FakeModel: a scripted ``ModelClient`` for tests (no network).

Responses are scripted per *agent* and per *step*:

* The agent of a request is found by ``agent_of(request)``. The default looks for
  ``"You are <Name>"`` in the first system message (the agent system prompt starts
  that way, SPEC §10.2); requests without it (planner, supervisor, summarizer) use
  the key ``"default"``. Pass ``agent_of`` to key requests differently.
* The step is the 1-based number of requests seen so far for that agent.
* A script for an agent is either a list (index ``step - 1``) or a dict
  ``{step: item}``. An item is a ``ModelResponse``, an exception instance (raised),
  or a callable ``(request) -> ModelResponse``.
* When no item exists for a step: ``default`` is returned if set, otherwise
  ``ModelError("FakeModel: no scripted response ...")`` is raised.

Every request is recorded (``calls``) for assertions.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agentplatform.contracts.common import HealthStatus, new_id
from agentplatform.contracts.model_io import (
    ImagePart,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    TextPart,
    ToolCall,
)
from agentplatform.errors import ModelError

__all__ = [
    "FakeModel",
    "RecordedCall",
    "ScriptItem",
    "assert_has_image",
    "image_count",
    "multi_tool_response",
    "text_response",
    "tool_response",
]

ScriptItem = ModelResponse | BaseException | Callable[[ModelRequest], ModelResponse]
Script = Sequence[ScriptItem] | Mapping[int, ScriptItem]

_AGENT_RE = re.compile(r"\bYou are ([A-Z][A-Za-z0-9_-]*)")


def default_agent_of(req: ModelRequest) -> str:
    for msg in req.messages:
        if msg.role == "system":
            match = _AGENT_RE.search(msg.text_content())
            return match.group(1) if match else "default"
    return "default"


def tool_response(name: str, text: str | None = None, **arguments: Any) -> ModelResponse:
    """A response with one tool call ``name(**arguments)``."""
    return ModelResponse(
        text=text,
        tool_calls=[ToolCall(id=f"call_{new_id()[-12:]}", name=name, arguments=arguments)],
        usage=ModelUsage(input_tokens=100, output_tokens=20),
    )


def multi_tool_response(
    *calls: tuple[str, dict[str, Any]], text: str | None = None
) -> ModelResponse:
    """A response with several tool calls, in order."""
    return ModelResponse(
        text=text,
        tool_calls=[
            ToolCall(id=f"call_{new_id()[-12:]}", name=name, arguments=args) for name, args in calls
        ],
        usage=ModelUsage(input_tokens=100, output_tokens=20 * len(calls)),
    )


def text_response(text: str) -> ModelResponse:
    """A plain text response (no tool calls)."""
    return ModelResponse(text=text, usage=ModelUsage(input_tokens=100, output_tokens=20))


def image_count(req: ModelRequest) -> int:
    """Number of image parts across all messages of a request."""
    return sum(len(m.images()) for m in req.messages)


def assert_has_image(req: ModelRequest, *, in_last_user_message: bool = True) -> ImagePart:
    """Assert the request carries a screenshot (by default in its last user message)
    and that no image appears outside user messages. Returns the (last) image."""
    for msg in req.messages:
        if msg.role != "user":
            assert not msg.images(), f"image found in a {msg.role} message"
    users = [m for m in req.messages if m.role == "user"]
    assert users, "request has no user message"
    candidates = (
        users[-1].images() if in_last_user_message else [img for m in users for img in m.images()]
    )
    assert candidates, "no image part found in the expected user message(s)"
    return candidates[-1]


@dataclass(frozen=True, slots=True)
class RecordedCall:
    agent: str
    step: int
    request: ModelRequest

    def last_user_text(self) -> str:
        users = [m for m in self.request.messages if m.role == "user"]
        return users[-1].text_content() if users else ""

    def all_text(self) -> str:
        return "\n".join(
            p.text for m in self.request.messages for p in m.content if isinstance(p, TextPart)
        )


class FakeModel:
    """Scripted ``ModelClient``. See the module docstring."""

    def __init__(
        self,
        script: Mapping[str, Script] | None = None,
        *,
        default: ModelResponse | None = None,
        agent_of: Callable[[ModelRequest], str] = default_agent_of,
        latency_s: float = 0.0,
        name: str = "fake",
    ) -> None:
        self._scripts: dict[str, Script] = dict(script or {})
        self._default = default
        self._agent_of = agent_of
        self._latency_s = latency_s
        self._name = name
        self._steps: dict[str, int] = {}
        self.calls: list[RecordedCall] = []
        self.closed = False

    @property
    def name(self) -> str:
        return self._name

    def set_script(self, agent: str, script: Script) -> None:
        self._scripts[agent] = script

    def calls_for(self, agent: str) -> list[RecordedCall]:
        return [c for c in self.calls if c.agent == agent]

    async def complete(self, req: ModelRequest) -> ModelResponse:
        agent = self._agent_of(req)
        step = self._steps.get(agent, 0) + 1
        self._steps[agent] = step
        self.calls.append(RecordedCall(agent=agent, step=step, request=req))
        if self._latency_s:
            await asyncio.sleep(self._latency_s)
        item = self._lookup(agent, step)
        if item is None:
            if self._default is not None:
                return self._default.model_copy(deep=True)
            raise ModelError(f"FakeModel: no scripted response for agent {agent!r} step {step}")
        if isinstance(item, BaseException):
            raise item
        resp = item.model_copy(deep=True) if isinstance(item, ModelResponse) else item(req)
        if not resp.usage.images:
            resp.usage = resp.usage.model_copy(update={"images": image_count(req)})
        return resp

    def _lookup(self, agent: str, step: int) -> ScriptItem | None:
        script = self._scripts.get(agent)
        if script is None:
            return None
        if isinstance(script, Mapping):
            return script.get(step)
        return script[step - 1] if step <= len(script) else None

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=0)

    async def aclose(self) -> None:
        self.closed = True
