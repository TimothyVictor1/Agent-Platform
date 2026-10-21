"""Tool registry (SPEC §10.3).

Tools are registered in groups (one module per group: ``desktop``, ``control``,
``coordination``, ``artifacts``) with a *kind* that tells the loop how to schedule
them:

* ``desktop``: terminal GUI/shell action, exactly one per step, must be last;
* ``coordination``: non-terminal (board, messages, notebook, artifacts), max 2 per step;
* ``control``: terminal task control (submit_result, fail_task, request_approval, wait_for).

The registry is generic over the context object handed to handlers, so the agent
loop (T06) defines its own context type without changing this module. Groups can
be disabled per session mode (``for_mode``) and individual tools hidden per call
(phase gating, e.g. ``submit_result`` during the first 3 steps).

Handlers never crash the loop: expected errors (``AppError``) and unexpected
exceptions are turned into ``ERROR: ...`` results (max 600 chars).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from jsonschema import Draft202012Validator

from agentplatform.contracts.common import SessionMode
from agentplatform.contracts.model_io import ToolCall, ToolSpec
from agentplatform.contracts.task import TaskResult
from agentplatform.errors import AppError

__all__ = [
    "MAX_RESULT_CHARS",
    "MODE_DISABLED_GROUPS",
    "RegisteredTool",
    "TaskGaveUp",
    "TaskSubmitted",
    "ToolHandler",
    "ToolKind",
    "ToolRegistry",
    "ToolResult",
    "error_result",
]

log = structlog.get_logger(__name__)

MAX_RESULT_CHARS = 600

ToolKind = Literal["desktop", "coordination", "control"]

MODE_DISABLED_GROUPS: Mapping[SessionMode, frozenset[str]] = {
    "coordinated": frozenset(),
    "parallel_isolated": frozenset({"coordination", "artifacts"}),
    "solo": frozenset(),
}
"""Tool groups disabled in each session mode (SPEC §10.3, §12.3)."""


@dataclass(frozen=True, slots=True)
class TaskSubmitted:
    """Outcome of a successful ``submit_result``: the loop ends the task."""

    result: TaskResult


@dataclass(frozen=True, slots=True)
class TaskGaveUp:
    """Outcome of ``fail_task``: the loop ends the task as failed."""

    reason: str
    what_was_tried: str = ""


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool returns to the loop.

    ``text`` goes back to the model as the tool result (truncated to 600 chars).
    ``outcome`` is set only by control tools that end the task. ``image`` (JPEG) is
    attached to the next observation (used by ``zoom``).
    """

    text: str
    is_error: bool = False
    outcome: TaskSubmitted | TaskGaveUp | None = None
    image: bytes | None = None


def error_result(message: str) -> ToolResult:
    """An ``ERROR: ...`` result, truncated to the result limit."""
    text = message if message.startswith("ERROR:") else f"ERROR: {message}"
    return ToolResult(text=_truncate(text), is_error=True)


def _truncate(text: str) -> str:
    if len(text) <= MAX_RESULT_CHARS:
        return text
    return text[: MAX_RESULT_CHARS - 1] + "…"


ToolHandler = Callable[[dict[str, Any], Any], Awaitable[ToolResult]]
"""``async handler(args, ctx) -> ToolResult``. ``args`` already passed schema validation."""


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    group: str
    kind: ToolKind
    spec: ToolSpec
    handler: ToolHandler
    validator: Draft202012Validator = field(repr=False)


class ToolRegistry:
    """Holds tools and executes tool calls. A registry is cheap to copy with a
    different set of disabled groups (``with_disabled_groups``/``for_mode``); copies
    share registrations made before the copy."""

    def __init__(self, disabled_groups: Collection[str] = ()) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._disabled = frozenset(disabled_groups)

    # -- registration ---------------------------------------------------------------

    def register(self, group: str, spec: ToolSpec, handler: ToolHandler, *, kind: ToolKind) -> None:
        """Add a tool. Raises ``ValueError`` if the name is taken or the parameters
        schema is not a valid JSON Schema object schema."""
        if spec.name in self._tools:
            raise ValueError(f"tool {spec.name!r} is already registered")
        Draft202012Validator.check_schema(spec.parameters)
        if spec.parameters.get("type") != "object":
            raise ValueError(f"tool {spec.name!r}: parameters must be an object schema")
        self._tools[spec.name] = RegisteredTool(
            group=group,
            kind=kind,
            spec=spec,
            handler=handler,
            validator=Draft202012Validator(spec.parameters),
        )

    def with_disabled_groups(self, groups: Collection[str]) -> ToolRegistry:
        """A copy of this registry with ``groups`` (additionally) disabled."""
        copy = ToolRegistry(self._disabled | frozenset(groups))
        copy._tools = dict(self._tools)
        return copy

    def for_mode(self, mode: SessionMode) -> ToolRegistry:
        """A copy with the groups disabled that ``mode`` does not allow."""
        return self.with_disabled_groups(MODE_DISABLED_GROUPS[mode])

    # -- queries --------------------------------------------------------------------

    @property
    def disabled_groups(self) -> frozenset[str]:
        return self._disabled

    def _available(self, name: str, hidden: Collection[str]) -> RegisteredTool | None:
        tool = self._tools.get(name)
        if tool is None or tool.group in self._disabled or name in hidden:
            return None
        return tool

    def names(self, *, hidden: Collection[str] = ()) -> list[str]:
        """Names of the tools currently offered, in registration order."""
        return [n for n in self._tools if self._available(n, hidden) is not None]

    def specs(self, *, hidden: Collection[str] = ()) -> list[ToolSpec]:
        """Tool specs to send to the model (enabled groups, minus ``hidden``)."""
        return [self._tools[n].spec for n in self.names(hidden=hidden)]

    def kind_of(self, name: str) -> ToolKind | None:
        """Kind of a registered tool (even if disabled), ``None`` if unknown."""
        tool = self._tools.get(name)
        return tool.kind if tool else None

    def get(self, name: str) -> RegisteredTool | None:
        return self._tools.get(name)

    # -- execution ------------------------------------------------------------------

    async def execute(
        self, call: ToolCall, ctx: Any, *, hidden: Collection[str] = ()
    ) -> ToolResult:
        """Validate and run one tool call. Never raises (except cancellation)."""
        tool = self._available(call.name, hidden)
        if tool is None:
            valid = ", ".join(self.names(hidden=hidden))
            return error_result(f"unknown or unavailable tool {call.name!r}. Valid tools: {valid}")
        if "_raw" in call.arguments and len(call.arguments) == 1:
            return error_result(f"arguments for {call.name} were not valid JSON")
        problems = sorted(tool.validator.iter_errors(call.arguments), key=lambda e: list(e.path))
        if problems:
            first = problems[0]
            where = ".".join(str(p) for p in first.path) or "arguments"
            return error_result(f"invalid arguments for {call.name}: {where}: {first.message}")
        try:
            result = await tool.handler(dict(call.arguments), ctx)
        except AppError as exc:
            return error_result(exc.detail)
        except Exception as exc:
            log.exception("tool_failed", tool=call.name)
            return error_result(f"{call.name} failed unexpectedly ({type(exc).__name__})")
        if len(result.text) > MAX_RESULT_CHARS:
            return ToolResult(
                text=_truncate(result.text),
                is_error=result.is_error,
                outcome=result.outcome,
                image=result.image,
            )
        return result
