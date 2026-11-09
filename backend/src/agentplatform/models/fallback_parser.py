"""Tool-call fallback parser (SPEC §8.1, "Cross-cutting").

Many open GUI models answer with text actions instead of structured tool calls.
``parse_text_tool_calls`` turns such text into ``ToolCall`` objects. Its signature
matches ``agentplatform.runtime.agent.actions.FallbackParser``, so the runtime wiring
(T13) passes it to ``AgentRuntime(fallback_parser=...)`` unchanged.

Recognised forms, in text order:

* JSON objects ``{"tool": .., "args": {..}}``, ``{"name": .., "arguments": {..}}``
  (arguments may be a JSON string) and ``{"function": {"name": .., "arguments": ..}}``,
  bare or inside ```json fences, one or several, also inside a JSON list.
* Python-style calls: ``click(512, 300)``, ``click(x=512, y=300)``,
  ``type_text("hello")``, ``key("ctrl+l")`` and the pyautogui vocabulary
  (``pyautogui.click/doubleClick/rightClick/moveTo/dragTo/write/typewrite/press/
  hotkey/scroll/hscroll``).

The text is untrusted model output: Python calls are parsed with ``ast`` and only
literal arguments are accepted (``ast.literal_eval``); nothing is ever executed.
Calls whose mapped name is not in ``valid_tool_names`` are dropped. The registry
validates arguments, so this parser only maps names and argument positions.

Scrolling without a position (``pyautogui.scroll(-5)``) targets the screen centre,
written as ``x=-1, y=-1``; the action layer maps ``(-1, -1)`` to the centre (T03).
"""

from __future__ import annotations

import ast
import json
import re
import uuid
from collections.abc import Callable, Collection
from typing import Any, TypeGuard

from agentplatform.contracts.model_io import ToolCall

__all__ = [
    "MAX_CALLS",
    "MAX_JSON_DEPTH",
    "MAX_TEXT_CHARS",
    "SCREEN_CENTRE",
    "parse_text_tool_calls",
]

MAX_TEXT_CHARS = 20_000
"""Longer text is truncated before parsing (bounds the work on hostile input)."""
MAX_CALLS = 16
SCREEN_CENTRE = -1
MAX_SCROLL_AMOUNT = 50
MAX_CALL_CHARS = 4_000
"""A Python-style call longer than this is not an action (type_text allows 2000)."""
MAX_CANDIDATES = 256
"""At most this many ``{`` / ``name(`` positions are tried, bounding hostile input."""
MAX_JSON_DEPTH = 32
"""Deeper ``function`` / ``tool_calls`` nesting, or deeper argument values, is not an
action; the call is dropped (bounds recursion on hostile input)."""

_NAME_KEYS = ("tool", "name", "action", "tool_name", "function")
_ARG_KEYS = ("args", "arguments", "parameters", "input", "params")

# Positional parameter order of the platform's own tools (runtime/agent/tools/).
_POSITIONAL: dict[str, tuple[str, ...]] = {
    "click": ("x", "y", "button", "count"),
    "move": ("x", "y"),
    "drag": ("x1", "y1", "x2", "y2"),
    "scroll": ("x", "y", "direction", "amount"),
    "type_text": ("text",),
    "key": ("combo",),
    "run_command": ("cmd", "timeout_s"),
    "wait": ("seconds",),
    "zoom": ("x", "y", "w", "h"),
}

# Keyword aliases models commonly use, per target tool.
_KW_ALIASES: dict[str, dict[str, str]] = {
    "key": {"key": "combo", "keys": "combo", "hotkey": "combo"},
    "type_text": {"message": "text", "content": "text", "string": "text"},
    "click": {"clicks": "count"},
}

_CALL_START = re.compile(r"(?<![\w.])(?:pyautogui\.)?([A-Za-z_]\w*)\(")


def _new_id() -> str:
    return f"call_fb_{uuid.uuid4().hex[:16]}"


# --- JSON objects ----------------------------------------------------------------------


def _json_spans(text: str) -> list[tuple[int, int, Any]]:
    """Every top-level JSON object in ``text`` as ``(start, end, value)``."""
    decoder = json.JSONDecoder()
    spans: list[tuple[int, int, Any]] = []
    tries = 0
    i = text.find("{")
    while i != -1 and tries < MAX_CANDIDATES:
        tries += 1
        try:
            value, end = decoder.raw_decode(text, i)
        except (ValueError, RecursionError):
            i = text.find("{", i + 1)
            continue
        spans.append((i, end, value))
        i = text.find("{", end)
    return spans


def _coerce_args(raw: Any) -> dict[str, Any] | None:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw) if raw.strip() else {}
        except ValueError:
            return {"_raw": raw}
        return {str(k): v for k, v in parsed.items()} if isinstance(parsed, dict) else None
    return None


def _from_json_object(obj: dict[str, Any], depth: int = 0) -> tuple[str, dict[str, Any]] | None:
    if depth >= MAX_JSON_DEPTH:
        return None
    fn = obj.get("function")
    if isinstance(fn, dict):  # OpenAI style {"type":"function","function":{...}}
        return _from_json_object(fn, depth + 1)
    name = next((obj[k] for k in _NAME_KEYS if isinstance(obj.get(k), str)), None)
    if not name:
        return None
    raw_args = next((obj[k] for k in _ARG_KEYS if k in obj), None)
    args = _coerce_args(raw_args)
    if args is None:
        return None
    return name.strip(), args


def _json_calls(value: Any, depth: int = 0) -> list[tuple[str, dict[str, Any]]]:
    if depth >= MAX_JSON_DEPTH:
        return []
    if isinstance(value, dict):
        if isinstance(value.get("tool_calls"), list):
            return [c for item in value["tool_calls"] for c in _json_calls(item, depth + 1)]
        found = _from_json_object(value, depth)
        return [found] if found else []
    if isinstance(value, list):
        return [c for item in value for c in _json_calls(item, depth + 1)]
    return []


# --- Python-style calls ----------------------------------------------------------------


def _call_end(text: str, open_paren: int) -> int | None:
    """Index just past the ``)`` matching ``text[open_paren]``, honouring quotes."""
    depth = 0
    quote: str | None = None
    i = open_paren
    stop = min(len(text), open_paren + MAX_CALL_CHARS)
    while i < stop:
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i + 1 if ch == ")" else None
        elif ch == "\n" and depth == 1:
            return None  # a call spanning lines is prose, not an action
        i += 1
    return None


def _literal(node: ast.expr) -> Any:
    return ast.literal_eval(node)


def _parse_call(src: str) -> tuple[str, list[Any], dict[str, Any]] | None:
    try:
        tree = ast.parse(src, mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    call = tree.body
    if not isinstance(call, ast.Call):
        return None
    func = call.func
    if isinstance(func, ast.Name):
        name = func.id
    elif (
        isinstance(func, ast.Attribute)
        and isinstance(func.value, ast.Name)
        and func.value.id == "pyautogui"
    ):
        name = func.attr
    else:
        return None
    try:
        args = [_literal(a) for a in call.args]
        kwargs = {kw.arg: _literal(kw.value) for kw in call.keywords if kw.arg is not None}
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None
    if any(kw.arg is None for kw in call.keywords):  # **kwargs
        return None
    return name, args, kwargs


def _is_num(v: object) -> TypeGuard[int | float]:
    return isinstance(v, int | float) and not isinstance(v, bool)


def _xy(args: list[Any], kwargs: dict[str, Any]) -> tuple[Any, Any] | None:
    x = kwargs.get("x", args[0] if args else None)
    y = kwargs.get("y", args[1] if len(args) > 1 else None)
    if isinstance(x, list | tuple) and len(x) == 2 and y is None:  # click((x, y))
        x, y = x
    if _is_num(x) and _is_num(y):
        return x, y
    return None


def _pg_click(
    button: str | None = None, count: int | None = None
) -> Callable[[list[Any], dict[str, Any]], tuple[str, dict[str, Any]] | None]:
    def build(args: list[Any], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        xy = _xy(args, kwargs)
        if xy is None:
            return None
        out: dict[str, Any] = {"x": xy[0], "y": xy[1]}
        btn = button or kwargs.get("button")
        if isinstance(btn, str) and btn != "left":
            out["button"] = btn
        n = count or kwargs.get("clicks") or kwargs.get("count")
        if _is_num(n) and int(n) > 1:
            out["count"] = int(n)
        return "click", out

    return build


def _pg_move(args: list[Any], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    xy = _xy(args, kwargs)
    return ("move", {"x": xy[0], "y": xy[1]}) if xy else None


def _pg_write(args: list[Any], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    text = kwargs.get("message", kwargs.get("text", args[0] if args else None))
    return ("type_text", {"text": text}) if isinstance(text, str) and text else None


def _pg_press(args: list[Any], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    keys = kwargs.get("keys", args[0] if args else None)
    if isinstance(keys, str) and keys:
        return "key", {"combo": keys}
    return None


def _pg_hotkey(args: list[Any], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    keys = list(args[0]) if len(args) == 1 and isinstance(args[0], list | tuple) else args
    if keys and all(isinstance(k, str) and k for k in keys):
        return "key", {"combo": "+".join(keys)}
    return None


def _pg_scroll(horizontal: bool) -> Callable[..., tuple[str, dict[str, Any]] | None]:
    def build(args: list[Any], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
        clicks = kwargs.get("clicks", args[0] if args else None)
        if not _is_num(clicks) or int(clicks) == 0:
            return None
        n = int(clicks)
        positive, negative = ("right", "left") if horizontal else ("up", "down")
        direction = positive if n > 0 else negative
        x = kwargs.get("x", args[1] if len(args) > 1 else None)
        y = kwargs.get("y", args[2] if len(args) > 2 else None)
        if not (_is_num(x) and _is_num(y)):
            x = y = SCREEN_CENTRE
        amount = min(abs(n), MAX_SCROLL_AMOUNT)
        return "scroll", {"x": x, "y": y, "direction": direction, "amount": amount}

    return build


def _pg_drag(args: list[Any], kwargs: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    # drag(x1, y1, x2, y2) is the platform tool; pyautogui.dragTo(x, y) has no start.
    if len(args) == 4 and all(_is_num(a) for a in args):
        return "drag", dict(zip(("x1", "y1", "x2", "y2"), args, strict=True))
    return None


_PYAUTOGUI: dict[str, Callable[[list[Any], dict[str, Any]], tuple[str, dict[str, Any]] | None]] = {
    "click": _pg_click(),
    "leftClick": _pg_click("left"),
    "rightClick": _pg_click("right"),
    "middleClick": _pg_click("middle"),
    "doubleClick": _pg_click(count=2),
    "tripleClick": _pg_click(count=3),
    "moveTo": _pg_move,
    "write": _pg_write,
    "typewrite": _pg_write,
    "press": _pg_press,
    "hotkey": _pg_hotkey,
    "scroll": _pg_scroll(horizontal=False),
    "vscroll": _pg_scroll(horizontal=False),
    "hscroll": _pg_scroll(horizontal=True),
}


def _map_direct(
    name: str, args: list[Any], kwargs: dict[str, Any]
) -> tuple[str, dict[str, Any]] | None:
    """A call of a platform tool by its own name, e.g. ``click(x=1, y=2)``."""
    order = _POSITIONAL.get(name)
    if args and (order is None or len(args) > len(order)):
        return None
    out: dict[str, Any] = dict(zip(order or (), args, strict=False))
    aliases = _KW_ALIASES.get(name, {})
    for k, v in kwargs.items():
        out.setdefault(aliases.get(k, k), v)
    return name, out


def _map_python(
    name: str, args: list[Any], kwargs: dict[str, Any], prefixed: bool, valid: Collection[str]
) -> tuple[str, dict[str, Any]] | None:
    # The platform's own scroll(x, y, direction, amount) wins over pyautogui.scroll(clicks)
    # unless the call is written pyautogui.scroll(...) or has a single numeric argument.
    if name in valid and not prefixed and not (name == "scroll" and len(args) == 1):
        return _map_direct(name, args, kwargs)
    builder = _PYAUTOGUI.get(name)
    if builder is not None:
        return builder(args, kwargs)
    if name in {"type", "type_text"}:
        return _pg_write(args, kwargs)
    if name == "drag":
        return _pg_drag(args, kwargs)
    return None


def _python_calls(
    text: str, skip: list[tuple[int, int]], valid: Collection[str]
) -> list[tuple[int, str, dict[str, Any]]]:
    found: list[tuple[int, str, dict[str, Any]]] = []
    pos = 0
    tries = 0
    while tries < MAX_CANDIDATES and (m := _CALL_START.search(text, pos)) is not None:
        tries += 1
        start = m.start()
        if any(a <= start < b for a, b in skip):
            pos = m.end()
            continue
        end = _call_end(text, m.end() - 1)
        if end is None:
            pos = m.end()
            continue
        parsed = _parse_call(text[start:end])
        pos = end if parsed else m.end()
        if parsed is None:
            continue
        name, args, kwargs = parsed
        prefixed = text.startswith("pyautogui.", start)
        mapped = _map_python(name, args, kwargs, prefixed, valid)
        if mapped is not None:
            found.append((start, mapped[0], mapped[1]))
    return found


def parse_text_tool_calls(text: str, valid_tool_names: Collection[str]) -> list[ToolCall]:
    """Parse text actions in ``text`` into tool calls (see the module docstring).

    Returns calls in text order, at most ``MAX_CALLS``; unknown tool names are
    ignored. Never raises on any input.
    """
    if not text:
        return []
    text = text[:MAX_TEXT_CHARS]
    valid = set(valid_tool_names)

    found: list[tuple[int, str, dict[str, Any]]] = []
    spans = _json_spans(text)
    for start, _end, value in spans:
        found.extend((start, name, args) for name, args in _json_calls(value))
    found.extend(_python_calls(text, [(a, b) for a, b, _ in spans], valid))
    found.sort(key=lambda item: item[0])

    calls = [
        ToolCall(id=_new_id(), name=name, arguments=args)
        for _pos, name, args in found
        if name in valid
    ]
    return calls[:MAX_CALLS]
