"""Key-combo normalisation (no Docker needed): named keys must become
correctly cased X keysyms, or xdotool silently ignores the press."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control.input import normalize_combo


@pytest.mark.parametrize(
    ("combo", "expected"),
    [
        ("enter", "Return"),
        ("Return", "Return"),
        ("esc", "Escape"),
        ("backspace", "BackSpace"),
        ("Tab", "Tab"),
        ("delete", "Delete"),
        ("home", "Home"),
        ("end", "End"),
        ("up", "Up"),
        ("ArrowLeft", "Left"),
        ("pgup", "Prior"),
        ("page_down", "Next"),
        ("insert", "Insert"),
        ("F5", "F5"),
        ("f12", "F12"),
        ("space", "space"),
        ("ctrl+plus", "ctrl+plus"),
        ("ctrl+minus", "ctrl+minus"),
        ("ctrl+,", "ctrl+comma"),
        ("ctrl+.", "ctrl+period"),
        ("ctrl+/", "ctrl+slash"),
        ("Ctrl + Shift + T", "ctrl+shift+t"),
        ("cmd+l", "super+l"),
        ("alt+tab", "alt+Tab"),
        ("ctrl+l", "ctrl+l"),
    ],
)
def test_normalize_combo(combo: str, expected: str) -> None:
    assert normalize_combo(combo) == expected


@pytest.mark.parametrize("combo", ["", "ctrl+", "ctrl+shift", "ctrl+FOO_BAR", "a+b", "ctrl+é"])
def test_normalize_combo_rejects(combo: str) -> None:
    with pytest.raises(ValueError, match="combo"):
        normalize_combo(combo)
