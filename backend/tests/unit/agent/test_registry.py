"""ToolRegistry: registration, mode/phase gating, validation, error handling."""

from __future__ import annotations

from typing import Any

import pytest

from agentplatform.contracts.model_io import ToolCall, ToolSpec
from agentplatform.contracts.task import TaskResult
from agentplatform.errors import ActionError
from agentplatform.runtime.agent.tools.registry import (
    MAX_RESULT_CHARS,
    TaskSubmitted,
    ToolRegistry,
    ToolResult,
)


def spec(name: str, **props: Any) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"{name} tool",
        parameters={
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    )


async def echo(args: dict[str, Any], ctx: Any) -> ToolResult:
    return ToolResult(text=f"echo {args} ctx={ctx}")


async def boom(args: dict[str, Any], ctx: Any) -> ToolResult:
    raise RuntimeError("secret internals")


async def action_error(args: dict[str, Any], ctx: Any) -> ToolResult:
    raise ActionError("coordinates are outside the screen")


async def long_text(args: dict[str, Any], ctx: Any) -> ToolResult:
    return ToolResult(text="x" * 5000)


async def submit(args: dict[str, Any], ctx: Any) -> ToolResult:
    return ToolResult(text="submitted", outcome=TaskSubmitted(TaskResult(summary="ok")))


@pytest.fixture
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register("desktop", spec("click", x={"type": "integer"}, y={"type": "integer"}), echo,
                 kind="desktop")  # fmt: skip
    reg.register("coordination", spec("post", title={"type": "string"}), echo, kind="coordination")
    reg.register("artifacts", spec("publish_artifact"), echo, kind="coordination")
    reg.register("control", spec("submit_result"), submit, kind="control")
    reg.register("desktop", spec("boom"), boom, kind="desktop")
    reg.register("desktop", spec("bad_action"), action_error, kind="desktop")
    reg.register("desktop", spec("long"), long_text, kind="desktop")
    return reg


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id="c1", name=name, arguments=args)


def test_duplicate_registration_fails(registry: ToolRegistry) -> None:
    with pytest.raises(ValueError, match="already registered"):
        registry.register("desktop", spec("click"), echo, kind="desktop")


def test_parameters_must_be_object_schema() -> None:
    reg = ToolRegistry()
    with pytest.raises(ValueError, match="object schema"):
        reg.register("x", ToolSpec(name="t", description="", parameters={"type": "string"}),
                     echo, kind="desktop")  # fmt: skip


def test_specs_follow_registration_order(registry: ToolRegistry) -> None:
    assert [s.name for s in registry.specs()] == [
        "click", "post", "publish_artifact", "submit_result", "boom", "bad_action", "long",
    ]  # fmt: skip
    assert registry.kind_of("post") == "coordination"
    assert registry.kind_of("nope") is None


def test_parallel_isolated_disables_coordination_and_artifacts(registry: ToolRegistry) -> None:
    isolated = registry.for_mode("parallel_isolated")
    names = isolated.names()
    assert "post" not in names
    assert "publish_artifact" not in names
    assert "click" in names
    # the original registry is unchanged
    assert "post" in registry.names()
    assert registry.for_mode("coordinated").names() == registry.names()
    assert registry.for_mode("solo").names() == registry.names()


async def test_execute_happy_path(registry: ToolRegistry) -> None:
    result = await registry.execute(call("click", x=1, y=2), ctx="CTX")
    assert not result.is_error
    assert result.text == "echo {'x': 1, 'y': 2} ctx=CTX"


async def test_unknown_tool_lists_valid_tools(registry: ToolRegistry) -> None:
    result = await registry.execute(call("teleport"), ctx=None)
    assert result.is_error
    assert result.text.startswith("ERROR: unknown or unavailable tool 'teleport'")
    assert "click" in result.text


async def test_disabled_group_tool_is_unavailable(registry: ToolRegistry) -> None:
    result = await registry.for_mode("parallel_isolated").execute(call("post", title="x"), None)
    assert result.is_error
    assert "post" in result.text
    assert "click" in result.text


async def test_hidden_tool_is_unavailable(registry: ToolRegistry) -> None:
    assert "submit_result" not in registry.names(hidden={"submit_result"})
    result = await registry.execute(call("submit_result"), None, hidden={"submit_result"})
    assert result.is_error
    assert result.outcome is None


async def test_invalid_arguments(registry: ToolRegistry) -> None:
    result = await registry.execute(call("click", x="left", y=2), None)
    assert result.is_error
    assert "invalid arguments for click: x" in result.text
    missing = await registry.execute(call("click", x=1), None)
    assert missing.is_error
    assert "'y' is a required property" in missing.text
    extra = await registry.execute(call("click", x=1, y=2, z=3), None)
    assert extra.is_error


async def test_raw_unparsed_arguments(registry: ToolRegistry) -> None:
    result = await registry.execute(call("click", _raw="{x: 1"), None)
    assert result.is_error
    assert "not valid JSON" in result.text


async def test_app_error_becomes_error_text(registry: ToolRegistry) -> None:
    result = await registry.execute(call("bad_action"), None)
    assert result == ToolResult(text="ERROR: coordinates are outside the screen", is_error=True)


async def test_unexpected_exception_does_not_leak_details(registry: ToolRegistry) -> None:
    result = await registry.execute(call("boom"), None)
    assert result.is_error
    assert "secret internals" not in result.text
    assert "RuntimeError" in result.text


async def test_results_are_truncated(registry: ToolRegistry) -> None:
    result = await registry.execute(call("long"), None)
    assert len(result.text) == MAX_RESULT_CHARS
    assert result.text.endswith("…")


async def test_control_outcome_passes_through(registry: ToolRegistry) -> None:
    result = await registry.execute(call("submit_result"), None)
    assert isinstance(result.outcome, TaskSubmitted)
    assert result.outcome.result.summary == "ok"
