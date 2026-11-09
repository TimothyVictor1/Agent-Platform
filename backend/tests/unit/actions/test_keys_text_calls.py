"""normalize_key, validate_type_text and normalize_tool_calls (SPEC §8.1, §10.5, T03)."""

from __future__ import annotations

from collections.abc import Collection

import pytest

from agentplatform.contracts.model_io import ModelResponse, ToolCall
from agentplatform.errors import ActionError
from agentplatform.runtime.agent.actions import (
    MAX_TYPE_TEXT_CHARS,
    normalize_key,
    normalize_tool_calls,
    validate_type_text,
)

KEYS = [
    ("enter", "enter"),
    ("Return", "enter"),
    ("RETURN", "enter"),
    ("esc", "escape"),
    ("Escape", "escape"),
    ("del", "delete"),
    ("bs", "backspace"),
    ("BackSpace", "backspace"),
    ("cmd", "super"),
    ("command", "super"),
    ("win", "super"),
    ("meta", "super"),
    ("option", "alt"),
    ("control+c", "ctrl+c"),
    ("ArrowUp", "up"),
    ("arrowdown", "down"),
    ("ArrowLeft", "left"),
    ("arrowright", "right"),
    ("arrow up", "up"),
    ("pgup", "page_up"),
    ("PgDn", "page_down"),
    ("PageUp", "page_up"),
    ("Page Down", "page_down"),
    ("page_up", "page_up"),
    ("spacebar", "space"),
    ("ins", "insert"),
    ("Tab", "tab"),
    ("F5", "f5"),
    ("a", "a"),
    ("Ctrl + Shift + T", "ctrl+shift+t"),
    ("ctrl+L", "ctrl+l"),
    ("cmd+shift+esc", "super+shift+escape"),
    ("alt+Tab", "alt+tab"),
    ("Ctrl+Return", "ctrl+enter"),
    ("  ctrl  +  arrowleft ", "ctrl+left"),
    ("option+Del", "alt+delete"),
]


@pytest.mark.parametrize(("combo", "expected"), KEYS)
def test_normalize_key(combo: str, expected: str) -> None:
    assert normalize_key(combo) == expected


def test_normalize_key_is_idempotent() -> None:
    for _, expected in KEYS:
        assert normalize_key(expected) == expected


@pytest.mark.parametrize("combo", ["", "   ", "+", "ctrl+", "+a", "ctrl++t", "ctrl+ +t"])
def test_empty_combo_or_part_is_rejected(combo: str) -> None:
    with pytest.raises(ActionError, match="invalid key combo"):
        normalize_key(combo)


@pytest.mark.parametrize(
    "text",
    ["", "hello", "line one\nline two\n", "ä" * MAX_TYPE_TEXT_CHARS, "x" * MAX_TYPE_TEXT_CHARS],
)
def test_type_text_up_to_the_limit_is_accepted(text: str) -> None:
    validate_type_text(text)


def test_type_text_over_the_limit_is_rejected_with_advice() -> None:
    with pytest.raises(ActionError, match=r"2001 characters \(max 2000\).*run_command"):
        validate_type_text("x" * (MAX_TYPE_TEXT_CHARS + 1))
    with pytest.raises(ActionError):
        validate_type_text("\n" * (MAX_TYPE_TEXT_CHARS + 1))  # newlines count too


class Parser:
    def __init__(self, result: list[ToolCall]) -> None:
        self.result = result
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, text: str, names: Collection[str]) -> list[ToolCall]:
        self.calls.append((text, sorted(names)))
        return self.result


CLICK = ToolCall(id="p1", name="click", arguments={"x": 1, "y": 2})


def test_structured_calls_win_over_text() -> None:
    parser = Parser([CLICK])
    resp = ModelResponse(
        text="click(5, 5)", tool_calls=[ToolCall(id="s1", name="key", arguments={})]
    )
    calls, used = normalize_tool_calls(resp, ["click", "key"], parser)
    assert [c.id for c in calls] == ["s1"]
    assert used is False
    assert parser.calls == []


def test_fallback_parses_text() -> None:
    parser = Parser([CLICK])
    calls, used = normalize_tool_calls(ModelResponse(text="click(1, 2)"), {"click"}, parser)
    assert calls == [CLICK]
    assert used is True
    assert parser.calls == [("click(1, 2)", ["click"])]


def test_fallback_that_finds_nothing_is_not_reported_as_used() -> None:
    parser = Parser([])
    assert normalize_tool_calls(ModelResponse(text="thinking"), ["click"], parser) == ([], False)


def test_no_text_or_no_parser_gives_no_calls() -> None:
    parser = Parser([CLICK])
    assert normalize_tool_calls(ModelResponse(text=""), ["click"], parser) == ([], False)
    assert normalize_tool_calls(ModelResponse(), ["click"], parser) == ([], False)
    assert parser.calls == []
    assert normalize_tool_calls(ModelResponse(text="click(1, 2)"), ["click"]) == ([], False)


def test_unknown_tool_names_are_kept_for_the_registry() -> None:
    bogus = ToolCall(id="u1", name="teleport", arguments={})
    calls, _ = normalize_tool_calls(ModelResponse(tool_calls=[bogus]), ["click"])
    assert calls == [bogus]
