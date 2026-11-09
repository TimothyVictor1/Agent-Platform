"""Input verbs (click/move/drag/scroll/type/key) on top of xdotool.

All calls are blocking and must be run via ``asyncio.to_thread`` from the
FastAPI handlers. A single asyncio lock in main.py serialises them so
x11vnc viewers never observe an interleaved mousedown/mouseup sequence.
"""
from __future__ import annotations

import subprocess

from .config import SCREEN_HEIGHT, SCREEN_WIDTH

BUTTONS: dict[str, int] = {"left": 1, "middle": 2, "right": 3}
SCROLL_BUTTONS: dict[str, int] = {"up": 4, "down": 5, "left": 6, "right": 7}
DRAG_STEPS = 12


class ToolError(RuntimeError):
    """An underlying tool failed (surfaced to the caller as HTTP 500)."""


def run_xdotool(*args: str) -> str:
    proc = subprocess.run(
        ["xdotool", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if proc.returncode != 0:
        raise ToolError(f"xdotool {' '.join(args)} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def check_coords(x: int, y: int) -> None:
    if not (0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT):
        raise ValueError(
            f"coordinates ({x}, {y}) are outside the "
            f"{SCREEN_WIDTH}x{SCREEN_HEIGHT} screen"
        )


def click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    check_coords(x, y)
    code = BUTTONS.get(button)
    if code is None:
        raise ValueError(f"unknown button {button!r}, expected one of {sorted(BUTTONS)}")
    if not 1 <= count <= 3:
        raise ValueError("click count must be 1, 2 or 3")
    run_xdotool("mousemove", "--sync", str(x), str(y))
    run_xdotool("click", "--repeat", str(count), "--delay", "60", str(code))


def move(x: int, y: int) -> None:
    check_coords(x, y)
    run_xdotool("mousemove", "--sync", str(x), str(y))


def drag(x1: int, y1: int, x2: int, y2: int) -> None:
    check_coords(x1, y1)
    check_coords(x2, y2)
    run_xdotool("mousedown", "1")
    try:
        for step in range(1, DRAG_STEPS + 1):
            frac = step / DRAG_STEPS
            xs = round(x1 + (x2 - x1) * frac)
            ys = round(y1 + (y2 - y1) * frac)
            run_xdotool("mousemove", "--sync", str(xs), str(ys))
    finally:
        run_xdotool("mouseup", "1")


def scroll(
    x: int, y: int, direction: str = "down", amount: int = 3
) -> None:
    check_coords(x, y)
    code = SCROLL_BUTTONS.get(direction)
    if code is None:
        raise ValueError(
            f"unknown scroll direction {direction!r}, "
            f"expected one of {sorted(SCROLL_BUTTONS)}"
        )
    if not 1 <= amount <= 50:
        raise ValueError("scroll amount must be in 1..50")
    run_xdotool("mousemove", "--sync", str(x), str(y))
    run_xdotool("click", "--repeat", str(amount), "--delay", "40", str(code))


# --- keyboard ----------------------------------------------------------------
# Canonical xdotool keysyms for the aliases the backend's normalize_key uses.
KEY_ALIASES: dict[str, str] = {
    "return": "return",
    "enter": "return",
    "esc": "escape",
    "escape": "escape",
    "del": "delete",
    "delete": "delete",
    "bs": "backspace",
    "backspace": "backspace",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "meta": "super",
    "super": "super",
    "option": "alt",
    "opt": "alt",
    "alt": "alt",
    "ctrl": "ctrl",
    "control": "ctrl",
    "shift": "shift",
    "pgup": "prior",
    "page_up": "prior",
    "pageup": "prior",
    "pgdn": "next",
    "pgdown": "next",
    "page_down": "next",
    "pagedown": "next",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "up": "up",
    "down": "down",
    "left": "left",
    "right": "right",
    "space": "space",
    "tab": "tab",
    "home": "home",
    "end": "end",
}
MODIFIERS = frozenset({"ctrl", "shift", "alt", "super"})
MAX_MODIFIER_COMBO = 5  # safety: reject absurd chains


def normalize_combo(combo: str) -> str:
    """Map a display-ish combo like ``Ctrl + Shift + T`` to ``ctrl+shift+t``."""
    text = combo.strip()
    if not text or len(text) > 64:
        raise ValueError("key combo must be 1..64 characters")
    tokens = [tok.strip().lower() for tok in text.split("+")]
    if any(not tok for tok in tokens):
        raise ValueError(f"empty part in key combo {combo!r}")
    normalized: list[str] = []
    for tok in tokens:
        alias = KEY_ALIASES.get(tok)
        if alias is not None:
            normalized.append(alias)
        elif len(tok) == 1 and (tok.isalnum() or tok.isascii()):
            normalized.append(tok)
        else:
            raise ValueError(f"unsupported key {tok!r} in combo {combo!r}")
    modifiers = [tok for tok in normalized if tok in MODIFIERS]
    keys = [tok for tok in normalized if tok not in MODIFIERS]
    if len(modifiers) > MAX_MODIFIER_COMBO or len(keys) != 1 or not keys:
        raise ValueError(
            f"combo must have exactly one non-modifier key: {combo!r}"
        )
    ordered = modifiers + [keys[0]]
    return "+".join(ordered)


def key(combo: str) -> None:
    run_xdotool("key", "--", normalize_combo(combo))


def type_text(text: str, interval_ms: int = 10) -> None:
    if not text:
        raise ValueError("text must not be empty")
    run_xdotool(
        "type", "--clearmodifiers", "--delay", str(interval_ms), "--", text
    )
