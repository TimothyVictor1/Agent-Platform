"""Desktop tools (SPEC §10.3): click, move, drag, scroll, type_text, key, run_command,
wait, zoom.

Every desktop tool is *terminal*: exactly one per step, and it must be the last call.
Handlers convert model coordinates with ``actions.to_sandbox_coords`` (using the
observation the model saw this step) and call the ``SandboxHandle``. Expected
failures raise ``AppError`` subclasses (``ActionError``, ``CoordinateError``,
``SandboxError``); the registry turns them into ``ERROR: ...`` results (<= 600 chars).

The commands run by ``run_command`` execute inside the sandbox only, never on the
host (AGENTS.md §5).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from agentplatform.contracts.model_io import ToolSpec
from agentplatform.runtime.agent import actions
from agentplatform.runtime.agent.tools.registry import MAX_RESULT_CHARS, ToolRegistry, ToolResult

if TYPE_CHECKING:
    from agentplatform.runtime.agent.loop import ToolContext

__all__ = ["DESKTOP_GROUP", "DESKTOP_TOOL_NAMES", "register_desktop_tools"]

DESKTOP_GROUP = "desktop"
MAX_WAIT_S = 10.0
MAX_COMMAND_TIMEOUT_S = 300
MAX_COMMAND_CHARS = 4000

_NUM = {"type": "number"}


def _obj(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _xy(ctx: ToolContext, x: float, y: float) -> tuple[int, int]:
    return actions.to_sandbox_coords(
        x, y, ctx.coordinate_mode, ctx.observation, ctx.sandbox.screen_size
    )


def _fmt(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else f"{v:g}"


# -- handlers ---------------------------------------------------------------------------


async def _click(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    x, y = _xy(ctx, args["x"], args["y"])
    button = args.get("button", "left")
    count = int(args.get("count", 1))
    await ctx.sandbox.click(x, y, button=button, count=count)
    times = "" if count == 1 else f" x{count}"
    return ToolResult(text=f"clicked {button}{times} at ({_fmt(args['x'])}, {_fmt(args['y'])})")


async def _move(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    x, y = _xy(ctx, args["x"], args["y"])
    await ctx.sandbox.move(x, y)
    return ToolResult(text=f"moved pointer to ({_fmt(args['x'])}, {_fmt(args['y'])})")


async def _drag(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    x1, y1 = _xy(ctx, args["x1"], args["y1"])
    x2, y2 = _xy(ctx, args["x2"], args["y2"])
    await ctx.sandbox.drag(x1, y1, x2, y2)
    return ToolResult(
        text=f"dragged from ({_fmt(args['x1'])}, {_fmt(args['y1'])}) "
        f"to ({_fmt(args['x2'])}, {_fmt(args['y2'])})"
    )


async def _scroll(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    x, y = _xy(ctx, args["x"], args["y"])
    amount = int(args.get("amount", 3))
    await ctx.sandbox.scroll(x, y, args["direction"], amount)
    return ToolResult(text=f"scrolled {args['direction']} by {amount}")


async def _type_text(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    text: str = args["text"]
    actions.validate_type_text(text)
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            await ctx.sandbox.type_text(line)
        if i < len(lines) - 1:
            await ctx.sandbox.key("enter")
    shown = text if len(text) <= 60 else text[:57] + "..."
    return ToolResult(text=f"typed {len(text)} characters: {shown!r}")


async def _key(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    combo = actions.normalize_key(args["combo"])
    await ctx.sandbox.key(combo)
    return ToolResult(text=f"pressed {combo}")


async def _run_command(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    timeout_s = int(args.get("timeout_s", 60))
    res = await ctx.sandbox.run_command(args["cmd"], timeout_s=timeout_s)
    head = f"exit {res.exit_code}"
    if res.timed_out:
        head += f" (timed out after {timeout_s}s)"
    parts = [head]
    if res.stdout:
        parts.append("stdout:\n" + res.stdout)
    if res.stderr:
        parts.append("stderr:\n" + res.stderr)
    text = "\n".join(parts)
    if len(text) > MAX_RESULT_CHARS:
        marker = "\n…(output truncated; redirect to a file and read parts of it)"
        text = text[: MAX_RESULT_CHARS - len(marker)] + marker
    return ToolResult(text=text, is_error=res.timed_out)


async def _wait(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    seconds = float(args["seconds"])
    await ctx.hooks.sleep(seconds)
    return ToolResult(text=f"waited {_fmt(seconds)} s")


async def _zoom(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
    jpeg = await asyncio.to_thread(
        actions.crop_region,
        ctx.screenshot_png,
        args["x"],
        args["y"],
        args["w"],
        args["h"],
        ctx.coordinate_mode,
        ctx.observation,
        ctx.sandbox.screen_size,
    )
    return ToolResult(
        text="zoomed region; the enlarged image is attached to your next observation",
        image=jpeg,
    )


# -- specs ------------------------------------------------------------------------------

_SPECS: list[tuple[ToolSpec, Any]] = [
    (
        ToolSpec(
            name="click",
            description="Click at a point of the screenshot.",
            parameters=_obj(
                {
                    "x": _NUM,
                    "y": _NUM,
                    "button": {"type": "string", "enum": ["left", "right", "middle"]},
                    "count": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                ["x", "y"],
            ),
        ),
        _click,
    ),
    (
        ToolSpec(
            name="move",
            description="Move the mouse pointer without clicking (e.g. to open a hover menu).",
            parameters=_obj({"x": _NUM, "y": _NUM}, ["x", "y"]),
        ),
        _move,
    ),
    (
        ToolSpec(
            name="drag",
            description="Press the left button at (x1, y1), move to (x2, y2) and release.",
            parameters=_obj(
                {"x1": _NUM, "y1": _NUM, "x2": _NUM, "y2": _NUM}, ["x1", "y1", "x2", "y2"]
            ),
        ),
        _drag,
    ),
    (
        ToolSpec(
            name="scroll",
            description="Scroll the mouse wheel over a point.",
            parameters=_obj(
                {
                    "x": _NUM,
                    "y": _NUM,
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"]},
                    "amount": {"type": "integer", "minimum": 1, "maximum": 50},
                },
                ["x", "y", "direction"],
            ),
        ),
        _scroll,
    ),
    (
        ToolSpec(
            name="type_text",
            description=(
                "Type text into the focused window. A newline presses Enter. Max "
                f"{actions.MAX_TYPE_TEXT_CHARS} characters; write longer text to a file "
                "with run_command."
            ),
            parameters=_obj({"text": {"type": "string", "minLength": 1}}, ["text"]),
        ),
        _type_text,
    ),
    (
        ToolSpec(
            name="key",
            description="Press a key or key combination, e.g. 'enter', 'ctrl+l', 'alt+tab'.",
            parameters=_obj({"combo": {"type": "string", "minLength": 1}}, ["combo"]),
        ),
        _key,
    ),
    (
        ToolSpec(
            name="run_command",
            description=(
                "Run a shell command in your own Linux desktop (bash). Prefer this for "
                "file and data work. Output is truncated; redirect long output to a file."
            ),
            parameters=_obj(
                {
                    "cmd": {"type": "string", "minLength": 1, "maxLength": MAX_COMMAND_CHARS},
                    "timeout_s": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_COMMAND_TIMEOUT_S,
                    },
                },
                ["cmd"],
            ),
        ),
        _run_command,
    ),
    (
        ToolSpec(
            name="wait",
            description="Wait for the screen to settle (page loading), at most 10 seconds.",
            parameters=_obj(
                {"seconds": {"type": "number", "exclusiveMinimum": 0, "maximum": MAX_WAIT_S}},
                ["seconds"],
            ),
        ),
        _wait,
    ),
    (
        ToolSpec(
            name="zoom",
            description=(
                "Get an enlarged image of a region (x, y = top-left corner, w, h = size) "
                "in your next observation, to read small text."
            ),
            parameters=_obj(
                {
                    "x": _NUM,
                    "y": _NUM,
                    "w": {"type": "number", "exclusiveMinimum": 0},
                    "h": {"type": "number", "exclusiveMinimum": 0},
                },
                ["x", "y", "w", "h"],
            ),
        ),
        _zoom,
    ),
]

DESKTOP_TOOL_NAMES = frozenset(spec.name for spec, _ in _SPECS)


def register_desktop_tools(registry: ToolRegistry) -> None:
    """Register every desktop tool in ``registry`` (group ``desktop``, kind ``desktop``)."""
    for spec, handler in _SPECS:
        registry.register(DESKTOP_GROUP, spec, handler, kind="desktop")
