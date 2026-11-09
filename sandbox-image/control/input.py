"""Input verbs (click/move/drag/scroll/type/key) on top of xdotool.

All calls are blocking and must be run via ``asyncio.to_thread`` from the
FastAPI handlers. A single asyncio lock in main.py serialises them so
x11vnc viewers never observe an interleaved mousedown/mouseup sequence.
"""

from __future__ import annotations

import subprocess

from .config import (
    MAX_CLICK_COUNT,
    MAX_SCROLL_AMOUNT,
    MIN_SCROLL_AMOUNT,
    SCREEN_HEIGHT,
    SCREEN_WIDTH,
)

BUTTONS: dict[str, int] = {"left": 1, "middle": 2, "right": 3}
SCROLL_BUTTONS: dict[str, int] = {"up": 4, "down": 5, "left": 6, "right": 7}
DRAG_STEPS = 12


class ToolError(RuntimeError):
    """An underlying tool failed (surfaced to the caller as HTTP 500)."""


def run_xdotool(*args: str) -> str:
    proc = subprocess.run(  # noqa: S603 - args come from validated request models
        ["/usr/bin/xdotool", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )
    stderr = proc.stderr.strip()
    # xdotool can exit 0 even when a keysym is unknown, so check the message
    # regardless of the return code: a silent no-op key press would corrupt
    # an agent's view of the screen.
    if "No such key name" in stderr:
        raise ToolError(f"xdotool rejected an unknown key name: {stderr[:200]}")
    if proc.returncode != 0:
        raise ToolError(f"xdotool {' '.join(args)} failed: {stderr[:200]}")
    return proc.stdout


def check_coords(x: int, y: int) -> None:
    if not (0 <= x < SCREEN_WIDTH and 0 <= y < SCREEN_HEIGHT):
        raise ValueError(
            f"coordinates ({x}, {y}) are outside the {SCREEN_WIDTH}x{SCREEN_HEIGHT} screen"
        )


def click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    check_coords(x, y)
    code = BUTTONS.get(button)
    if code is None:
        raise ValueError(f"unknown button {button!r}, expected one of {sorted(BUTTONS)}")
    if not 1 <= count <= MAX_CLICK_COUNT:
        raise ValueError(f"click count must be 1..{MAX_CLICK_COUNT}")
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


def scroll(x: int, y: int, direction: str = "down", amount: int = 3) -> None:
    check_coords(x, y)
    code = SCROLL_BUTTONS.get(direction)
    if code is None:
        raise ValueError(
            f"unknown scroll direction {direction!r}, expected one of {sorted(SCROLL_BUTTONS)}"
        )
    if not MIN_SCROLL_AMOUNT <= amount <= MAX_SCROLL_AMOUNT:
        raise ValueError(f"scroll amount must be in {MIN_SCROLL_AMOUNT}..{MAX_SCROLL_AMOUNT}")
    run_xdotool("mousemove", "--sync", str(x), str(y))
    run_xdotool("click", "--repeat", str(amount), "--delay", "40", str(code))


# --- keyboard ----------------------------------------------------------------
# X keysym names for the aliases the backend's normalize_key produces.
# Keysym names are case-sensitive in xdotool: the named keys must be mapped
# to their correctly cased forms ("Return", not "return" — a lowercase name
# is not a keysym and the press would silently do nothing). Modifier names
# (ctrl/shift/alt/super) and the punctuation keysyms (plus, minus, ...)
# are lowercase by definition; "space" is too.
KEY_ALIASES: dict[str, str] = {
    "return": "Return",
    "enter": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "del": "Delete",
    "delete": "Delete",
    "bs": "BackSpace",
    "backspace": "BackSpace",
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
    "pgup": "Prior",
    "page_up": "Prior",
    "pageup": "Prior",
    "pgdn": "Next",
    "pgdown": "Next",
    "page_down": "Next",
    "pagedown": "Next",
    "insert": "Insert",
    "ins": "Insert",
    "arrowup": "Up",
    "arrowdown": "Down",
    "arrowleft": "Left",
    "arrowright": "Right",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "space": "space",
    "tab": "Tab",
    "plus": "plus",
    "minus": "minus",
    "comma": "comma",
    "period": "period",
    "slash": "slash",
}
KEY_ALIASES.update({f"f{i}": f"F{i}" for i in range(1, 13)})
# Single punctuation characters are not keysym names either ("," is
# "comma"); map them so "ctrl+," reaches the app instead of being dropped.
# "+" itself is the combo separator: callers spell it "plus".
KEY_ALIASES.update(
    {
        ",": "comma",
        ".": "period",
        "/": "slash",
        "-": "minus",
        "=": "equal",
        ";": "semicolon",
        "'": "apostrophe",
        "`": "grave",
        "[": "bracketleft",
        "]": "bracketright",
        "\\": "backslash",
        "!": "exclam",
        "@": "at",
        "#": "numbersign",
        "$": "dollar",
        "%": "percent",
        "^": "asciicircum",
        "&": "ampersand",
        "*": "asterisk",
        "(": "parenleft",
        ")": "parenright",
        "_": "underscore",
        ":": "colon",
        '"': "quotedbl",
        "<": "less",
        ">": "greater",
        "?": "question",
        "{": "braceleft",
        "}": "braceright",
        "|": "bar",
        "~": "asciitilde",
        "equal": "equal",
        "semicolon": "semicolon",
        "backslash": "backslash",
    }
)
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
        elif len(tok) == 1 and tok.isascii() and tok.isalnum():
            normalized.append(tok)
        else:
            raise ValueError(f"unsupported key {tok!r} in combo {combo!r}")
    modifiers = [tok for tok in normalized if tok in MODIFIERS]
    keys = [tok for tok in normalized if tok not in MODIFIERS]
    if len(modifiers) > MAX_MODIFIER_COMBO or len(keys) != 1 or not keys:
        raise ValueError(f"combo must have exactly one non-modifier key: {combo!r}")
    ordered = [*modifiers, keys[0]]
    return "+".join(ordered)


def key(combo: str) -> None:
    run_xdotool("key", "--", normalize_combo(combo))


def type_text(text: str, interval_ms: int = 10) -> None:
    if not text:
        raise ValueError("text must not be empty")
    run_xdotool("type", "--clearmodifiers", "--delay", str(interval_ms), "--", text)
