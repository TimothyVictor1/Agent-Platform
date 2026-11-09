"""Table-driven tests for the text tool-call fallback parser (T02)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from agentplatform.contracts.model_io import ModelResponse
from agentplatform.models.fallback_parser import (
    MAX_CALLS,
    MAX_TEXT_CHARS,
    parse_text_tool_calls,
)
from agentplatform.runtime.agent.actions import FallbackParser, normalize_tool_calls

TOOLS = [
    "click",
    "move",
    "drag",
    "scroll",
    "type_text",
    "key",
    "run_command",
    "wait",
    "zoom",
    "finish",
]

Call = tuple[str, dict[str, Any]]

CASES: list[tuple[str, str, list[Call]]] = [
    # --- JSON ----------------------------------------------------------------------------
    (
        "json tool/args",
        '{"tool": "click", "args": {"x": 512, "y": 300}}',
        [("click", {"x": 512, "y": 300})],
    ),
    (
        "json name/arguments",
        '{"name": "type_text", "arguments": {"text": "hello"}}',
        [("type_text", {"text": "hello"})],
    ),
    (
        "json arguments as string",
        '{"name": "key", "arguments": "{\\"combo\\": \\"ctrl+l\\"}"}',
        [("key", {"combo": "ctrl+l"})],
    ),
    (
        "json in fence with prose",
        'I will open the address bar.\n```json\n{"tool": "key", "args": {"combo": "ctrl+l"}}\n```',
        [("key", {"combo": "ctrl+l"})],
    ),
    (
        "two json objects",
        '{"tool": "click", "args": {"x": 1, "y": 2}}\n{"tool": "type_text", "args": {"text": "a"}}',
        [("click", {"x": 1, "y": 2}), ("type_text", {"text": "a"})],
    ),
    (
        "json list",
        '[{"name": "wait", "arguments": {"seconds": 2}},'
        ' {"name": "key", "arguments": {"combo": "enter"}}]',
        [("wait", {"seconds": 2}), ("key", {"combo": "enter"})],
    ),
    (
        "openai function shape",
        '{"type": "function", "function":'
        ' {"name": "move", "arguments": "{\\"x\\": 5, \\"y\\": 6}"}}',
        [("move", {"x": 5, "y": 6})],
    ),
    (
        "json without args",
        '{"tool": "finish"}',
        [("finish", {})],
    ),
    (
        "json with invalid argument string keeps raw",
        '{"name": "click", "arguments": "x=1"}',
        [("click", {"_raw": "x=1"})],
    ),
    (
        "json unknown tool ignored",
        '{"tool": "rm_rf", "args": {}}',
        [],
    ),
    (
        "json non-tool object ignored",
        '{"thought": "I see a login page", "confidence": 0.9}',
        [],
    ),
    (
        "call text inside json string is not parsed twice",
        '{"tool": "click", "args": {"x": 1, "y": 2}, "why": "click(9, 9) failed before"}',
        [("click", {"x": 1, "y": 2})],
    ),
    # --- pyautogui -----------------------------------------------------------------------
    ("pyautogui.click", "pyautogui.click(512, 300)", [("click", {"x": 512, "y": 300})]),
    (
        "pyautogui.click kwargs and button",
        "pyautogui.click(x=10, y=20, button='right')",
        [("click", {"x": 10, "y": 20, "button": "right"})],
    ),
    (
        "pyautogui.doubleClick",
        "pyautogui.doubleClick(3, 4)",
        [("click", {"x": 3, "y": 4, "count": 2})],
    ),
    (
        "pyautogui.rightClick",
        "pyautogui.rightClick(3, 4)",
        [("click", {"x": 3, "y": 4, "button": "right"})],
    ),
    ("pyautogui.moveTo", "pyautogui.moveTo(100, 200)", [("move", {"x": 100, "y": 200})]),
    ("pyautogui.write", "pyautogui.write('abc')", [("type_text", {"text": "abc"})]),
    (
        "pyautogui.typewrite",
        'pyautogui.typewrite("hi there")',
        [("type_text", {"text": "hi there"})],
    ),
    ("pyautogui.press", "pyautogui.press('enter')", [("key", {"combo": "enter"})]),
    ("pyautogui.hotkey", "pyautogui.hotkey('ctrl', 'l')", [("key", {"combo": "ctrl+l"})]),
    (
        "pyautogui.hotkey list",
        "pyautogui.hotkey(['ctrl', 'shift', 't'])",
        [("key", {"combo": "ctrl+shift+t"})],
    ),
    (
        "pyautogui.scroll down at centre",
        "pyautogui.scroll(-5)",
        [("scroll", {"x": -1, "y": -1, "direction": "down", "amount": 5})],
    ),
    (
        "pyautogui.scroll up at point",
        "pyautogui.scroll(3, x=100, y=200)",
        [("scroll", {"x": 100, "y": 200, "direction": "up", "amount": 3})],
    ),
    (
        "bare scroll(n) is pyautogui style",
        "scroll(-2)",
        [("scroll", {"x": -1, "y": -1, "direction": "down", "amount": 2})],
    ),
    (
        "huge scroll is capped",
        "pyautogui.scroll(-500)",
        [("scroll", {"x": -1, "y": -1, "direction": "down", "amount": 50})],
    ),
    (
        "pyautogui.hscroll",
        "pyautogui.hscroll(-4)",
        [("scroll", {"x": -1, "y": -1, "direction": "left", "amount": 4})],
    ),
    ("scroll(0) ignored", "pyautogui.scroll(0)", []),
    # --- platform tool names -------------------------------------------------------------
    ("click positional", "click(512, 300)", [("click", {"x": 512, "y": 300})]),
    ("click keywords", "click(x=512, y=300)", [("click", {"x": 512, "y": 300})]),
    ("type_text", 'type_text("hello")', [("type_text", {"text": "hello"})]),
    ("key", 'key("ctrl+l")', [("key", {"combo": "ctrl+l"})]),
    ("key alias kwarg", 'key(key="enter")', [("key", {"combo": "enter"})]),
    (
        "scroll with all args",
        'scroll(10, 20, "down", 3)',
        [("scroll", {"x": 10, "y": 20, "direction": "down", "amount": 3})],
    ),
    ("drag", "drag(1, 2, 3, 4)", [("drag", {"x1": 1, "y1": 2, "x2": 3, "y2": 4})]),
    ("floats kept", "click(0.25, 0.75)", [("click", {"x": 0.25, "y": 0.75})]),
    (
        "several calls in prose order",
        "First pyautogui.click(1, 2) then type_text('a') and finally key('enter').",
        [("click", {"x": 1, "y": 2}), ("type_text", {"text": "a"}), ("key", {"combo": "enter"})],
    ),
    (
        "code fence with python",
        "```python\npyautogui.click(5, 6)\npyautogui.write('x')\n```",
        [("click", {"x": 5, "y": 6}), ("type_text", {"text": "x"})],
    ),
    (
        "json and python mixed in order",
        'pyautogui.click(1, 1)\n{"tool": "key", "args": {"combo": "enter"}}',
        [("click", {"x": 1, "y": 1}), ("key", {"combo": "enter"})],
    ),
    # --- rejected input ------------------------------------------------------------------
    ("unknown function ignored", "os.system('rm -rf /')", []),
    ("unknown bare function ignored", "launch_missiles(1)", []),
    ("non-literal args ignored", "click(button_x, button_y)", []),
    ("prose with parentheses", "I will click (the blue button) now.", []),
    ("other module prefix ignored", "mouse.click(1, 2)", []),
    ("click without coordinates via pyautogui", "pyautogui.click()", []),
    ("calls are never executed", "click(__import__('os').getpid(), 1)", []),
    ("unterminated call", "click(1, 2", []),
    ("empty text", "", []),
    ("too many positional args", "key('a', 'b')", []),
]


@pytest.mark.parametrize(
    ("text", "expected"), [(t, e) for _, t, e in CASES], ids=[c[0] for c in CASES]
)
def test_parse_cases(text: str, expected: list[Call]) -> None:
    calls = parse_text_tool_calls(text, TOOLS)
    assert [(c.name, c.arguments) for c in calls] == expected


def test_has_at_least_twenty_cases() -> None:
    assert len(CASES) >= 20


def test_ids_are_unique_and_provider_safe() -> None:
    calls = parse_text_tool_calls("click(1, 2) click(3, 4)", TOOLS)
    ids = [c.id for c in calls]
    assert len(set(ids)) == 2
    assert all(i.startswith("call_fb_") and i.replace("_", "").isalnum() for i in ids)


def test_only_valid_names_are_returned() -> None:
    assert parse_text_tool_calls("pyautogui.click(1, 2)", ["key"]) == []
    assert [c.name for c in parse_text_tool_calls("pyautogui.press('a')", ["key"])] == ["key"]


def test_call_count_is_capped() -> None:
    text = " ".join(f"click({i}, {i})" for i in range(MAX_CALLS + 10))
    assert len(parse_text_tool_calls(text, TOOLS)) == MAX_CALLS


def test_hostile_input_is_bounded_and_does_not_raise() -> None:
    nasty = "{" * 50_000 + "click(" * 10_000 + "[" * 5000
    assert len(nasty) > MAX_TEXT_CHARS
    assert parse_text_tool_calls(nasty, TOOLS) == []
    assert parse_text_tool_calls("click(" + "(" * 3000 + "1" + ")" * 3001, TOOLS) == []


@pytest.mark.parametrize(
    "nasty",
    ["click(" * 4000, 'click("' * 3000, '{"a":' * 4000, "{" * 20_000, "f(x) " * 4000],
    ids=["open-calls", "open-strings", "open-json", "braces", "many-calls"],
)
def test_work_is_bounded_on_adversarial_text(nasty: str) -> None:
    started = time.perf_counter()
    parse_text_tool_calls(nasty, TOOLS)
    assert time.perf_counter() - started < 1.0  # was ~3 s before MAX_CANDIDATES/MAX_CALL_CHARS


def test_matches_the_agent_loop_fallback_type() -> None:
    # Typed assignment: mypy --strict fails the build if the signature drifts from
    # runtime/agent/actions.FallbackParser (what AgentRuntime(fallback_parser=...) takes).
    parser: FallbackParser = parse_text_tool_calls
    calls, used = normalize_tool_calls(
        ModelResponse(text="pyautogui.click(10, 20)"), frozenset(TOOLS), parser
    )
    assert used is True
    assert [(c.name, c.arguments) for c in calls] == [("click", {"x": 10, "y": 20})]
