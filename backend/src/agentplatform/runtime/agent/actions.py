"""Action layer: observations, coordinate mapping, key normalization (SPEC §10.5).

All functions here are pure (no I/O) so they can run anywhere and be unit-tested.

Coordinates: the model gives points in its profile's ``coordinate_mode`` (SPEC §8.2),
always relative to the observation it saw:

* ``absolute``: pixels of the (possibly downscaled) screenshot.
* ``normalized_1000``: 0..1000 on both axes.
* ``relative``: 0..1 floats.

Every mode is first turned into a fraction of the screen and then into pixels of the
target (the sandbox screen for actions, the full-size PNG for ``zoom``), so a resized
observation needs no special handling. ``(-1, -1)`` means "screen centre" (used by
the fallback parser for scroll calls without a position).
"""

from __future__ import annotations

import io
import math
from collections.abc import Callable, Collection
from dataclasses import dataclass

import imagehash
from PIL import Image

from agentplatform.contracts.common import CoordinateMode
from agentplatform.contracts.model_io import ModelResponse, ToolCall
from agentplatform.errors import ActionError, CoordinateError, SandboxError

__all__ = [
    "MAX_TYPE_TEXT_CHARS",
    "FallbackParser",
    "Observation",
    "crop_region",
    "normalize_key",
    "normalize_tool_calls",
    "prepare_observation",
    "to_sandbox_coords",
    "validate_type_text",
]

MAX_TYPE_TEXT_CHARS = 2000
ZOOM_MAX_WIDTH = 1280
ZOOM_MAX_HEIGHT = 1280
ZOOM_MAX_UPSCALE = 4.0
CENTRE = -1
"""``x == y == CENTRE`` means the centre of the screen."""

# Slightly negative values (a model aiming at the very edge) clamp to 0; anything
# further out, or beyond ``OUT_OF_RANGE_FACTOR`` x the axis span, is an error.
NEGATIVE_SLACK: dict[CoordinateMode, float] = {
    "absolute": 5.0,
    "normalized_1000": 5.0,
    "relative": 0.005,
}
OUT_OF_RANGE_FACTOR = 2.0

KEY_ALIASES: dict[str, str] = {
    "return": "enter",
    "esc": "escape",
    "del": "delete",
    "bs": "backspace",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "windows": "super",
    "meta": "super",
    "option": "alt",
    "control": "ctrl",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
    "arrow_up": "up",
    "arrow_down": "down",
    "arrow_left": "left",
    "arrow_right": "right",
    "pgup": "page_up",
    "pgdn": "page_down",
    "pageup": "page_up",
    "pagedown": "page_down",
    "spacebar": "space",
    "ins": "insert",
}


@dataclass(frozen=True, slots=True)
class Observation:
    """A screenshot prepared for the model.

    ``scale_x``/``scale_y`` are sandbox pixels per observation pixel (>= 1 because
    observations are never upscaled), so ``sandbox_x = obs_x * scale_x``.
    """

    jpeg: bytes
    width: int
    height: int
    scale_x: float
    scale_y: float
    phash: str
    source_width: int
    source_height: int


FallbackParser = Callable[[str, Collection[str]], list[ToolCall]]
"""``parse_text_tool_calls(text, valid_tool_names)`` from ``models/fallback_parser.py``."""


def _decode_rgb(png_bytes: bytes) -> Image.Image:
    """Decode a screenshot to RGB. A corrupt screenshot is a sandbox fault."""
    try:
        with Image.open(io.BytesIO(png_bytes)) as img:
            return img.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise SandboxError("screenshot is not a valid image") from exc


def prepare_observation(png_bytes: bytes, target_width: int, jpeg_quality: int = 70) -> Observation:
    """Decode a PNG screenshot, downscale to ``target_width`` keeping the aspect ratio
    (never upscale), JPEG-encode it and compute its perceptual hash (hex).

    Raises ``ActionError`` for ``target_width < 1`` and ``SandboxError`` when the
    bytes are not a decodable image."""
    if target_width < 1:
        raise ActionError(f"screenshot target width must be >= 1, got {target_width}")
    rgb = _decode_rgb(png_bytes)
    src_w, src_h = rgb.size
    if target_width < src_w:
        new_h = max(1, round(src_h * target_width / src_w))
        resized = rgb.resize((target_width, new_h), Image.Resampling.LANCZOS)
    else:
        resized = rgb
    out = io.BytesIO()
    resized.save(out, format="JPEG", quality=jpeg_quality)
    w, h = resized.size
    return Observation(
        jpeg=out.getvalue(),
        width=w,
        height=h,
        scale_x=src_w / w,
        scale_y=src_h / h,
        phash=str(imagehash.phash(resized)),
        source_width=src_w,
        source_height=src_h,
    )


# -- coordinates --------------------------------------------------------------------------


def _range_help(mode: CoordinateMode, obs: Observation) -> str:
    if mode == "normalized_1000":
        return "use integers 0..1000 (0,0 = top-left, 1000,1000 = bottom-right)"
    if mode == "relative":
        return "use decimals 0..1 (0,0 = top-left, 1,1 = bottom-right)"
    return f"use pixels inside the {obs.width}x{obs.height} screenshot (0,0 = top-left)"


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CoordinateError(f"{name} must be a number, got {value!r}")
    if not math.isfinite(value):
        raise CoordinateError(f"{name} must be a finite number, got {value!r}")
    return float(value)


def _fraction(
    value: float, name: str, mode: CoordinateMode, obs_extent: int, obs: Observation
) -> float:
    """Map one model coordinate to a fraction 0..1 of the screen axis."""
    if mode == "normalized_1000":
        span = 1000.0
    elif mode == "relative":
        span = 1.0
    else:
        span = float(obs_extent)
    if value < -NEGATIVE_SLACK[mode] or value > OUT_OF_RANGE_FACTOR * span:
        hint = " (did you give pixels?)" if mode == "relative" and value > 2 else ""
        raise CoordinateError(
            f"{name}={value:g} is outside the screen{hint}; {_range_help(mode, obs)}"
        )
    return min(max(value / span, 0.0), 1.0)


def _fractions(
    x: object, y: object, mode: CoordinateMode, obs: Observation, names: tuple[str, str]
) -> tuple[float, float]:
    fx = _fraction(_number(x, names[0]), names[0], mode, obs.width, obs)
    fy = _fraction(_number(y, names[1]), names[1], mode, obs.height, obs)
    return fx, fy


def to_sandbox_coords(
    x: float | None,
    y: float | None,
    mode: CoordinateMode,
    obs: Observation,
    screen_size: tuple[int, int],
) -> tuple[int, int]:
    """Convert model coordinates (in ``mode``, relative to ``obs``) to sandbox pixels,
    clamped into the screen. ``(-1, -1)`` is the screen centre. Raises
    ``CoordinateError`` with a model-friendly message for unusable values (None, NaN,
    infinite, below -5 units, or more than twice the axis range)."""
    width, height = screen_size
    if x == CENTRE and y == CENTRE:
        return width // 2, height // 2
    fx, fy = _fractions(x, y, mode, obs, ("x", "y"))
    return min(round(fx * width), width - 1), min(round(fy * height), height - 1)


def crop_region(
    png_bytes: bytes,
    x: float,
    y: float,
    w: float,
    h: float,
    mode: CoordinateMode,
    obs: Observation,
    screen_size: tuple[int, int],
) -> bytes:
    """Crop a region (top-left ``x, y`` and size ``w, h`` in model coordinates) from
    the full-resolution screenshot for the ``zoom`` tool. Small regions are enlarged
    (at most 4x) for reading; the result fits in 1280x1280. Returns JPEG bytes.

    The region is mapped onto the PNG's own pixel size, which equals ``screen_size``
    for a well-behaved sandbox; ``(-1, -1)`` is not the centre here (it clamps to 0)."""
    del screen_size  # mapped via the PNG itself; kept for the public signature
    rw, rh = _number(w, "w"), _number(h, "h")
    if rw <= 0 or rh <= 0:
        raise CoordinateError("zoom region must have a positive width and height")
    fx1, fy1 = _fractions(x, y, mode, obs, ("x", "y"))
    fx2, fy2 = _fractions(_number(x, "x") + rw, _number(y, "y") + rh, mode, obs, ("x + w", "y + h"))
    img = _decode_rgb(png_bytes)
    pw, ph = img.size
    x1, y1, x2, y2 = round(fx1 * pw), round(fy1 * ph), round(fx2 * pw), round(fy2 * ph)
    if x2 <= x1 or y2 <= y1:
        raise CoordinateError(
            f"zoom region is empty or outside the screen; {_range_help(mode, obs)}"
        )
    region = img.crop((x1, y1, x2, y2))
    scale = min(ZOOM_MAX_UPSCALE, ZOOM_MAX_WIDTH / region.width, ZOOM_MAX_HEIGHT / region.height)
    if scale != 1:
        region = region.resize(
            (max(1, round(region.width * scale)), max(1, round(region.height * scale))),
            Image.Resampling.LANCZOS,
        )
    out = io.BytesIO()
    region.save(out, format="JPEG", quality=85)
    return out.getvalue()


# -- keys and text ------------------------------------------------------------------------


def normalize_key(combo: str) -> str:
    """Normalize a key combo to lower-case names joined by ``+``: ``"Ctrl + Shift + T"``
    → ``"ctrl+shift+t"``, ``"Return"`` → ``"enter"``, ``"Page Up"`` → ``"page_up"``
    (aliases in ``KEY_ALIASES``). Raises ``ActionError`` for an empty combo or part."""
    parts = ["_".join(p.lower().split()) for p in combo.split("+")]
    if any(not p for p in parts):
        raise ActionError(f"invalid key combo {combo!r}; use names like 'ctrl+l' or 'enter'")
    return "+".join(KEY_ALIASES.get(p, p) for p in parts)


def validate_type_text(text: str) -> None:
    """Reject text the ``type_text`` tool should not type."""
    if len(text) > MAX_TYPE_TEXT_CHARS:
        raise ActionError(
            f"text is {len(text)} characters (max {MAX_TYPE_TEXT_CHARS}); "
            "write a file with run_command instead"
        )


def normalize_tool_calls(
    resp: ModelResponse,
    valid_tool_names: Collection[str],
    fallback: FallbackParser | None = None,
) -> tuple[list[ToolCall], bool]:
    """Structured tool calls first; if there are none and ``fallback`` is given, parse
    them from the response text. Returns ``(calls, used_fallback)``.

    Unknown tool names are kept: the registry answers them with an ``ERROR`` result
    listing valid tools, so the model can self-correct.
    """
    if resp.tool_calls:
        return list(resp.tool_calls), False
    if fallback is not None and resp.text:
        parsed = fallback(resp.text, valid_tool_names)
        return parsed, bool(parsed)
    return [], False
