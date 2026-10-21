"""Action layer: observations, coordinate mapping, key normalization (SPEC §10.5).

T00 provides the public signatures and a simple implementation for
``coordinate_mode="absolute"``. T03 completes it (other modes, the screen-centre
value, key aliases, full validation) without changing these signatures.

All functions here are pure (no I/O) so they can run anywhere and be unit-tested.
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
from agentplatform.errors import ActionError, CoordinateError

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


def prepare_observation(png_bytes: bytes, target_width: int, jpeg_quality: int = 70) -> Observation:
    """Decode a PNG screenshot, downscale to ``target_width`` keeping the aspect ratio
    (never upscale), JPEG-encode it and compute its perceptual hash (hex)."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        rgb = img.convert("RGB")
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


def to_sandbox_coords(
    x: float | None,
    y: float | None,
    mode: CoordinateMode,
    obs: Observation,
    screen_size: tuple[int, int],
) -> tuple[int, int]:
    """Convert model coordinates (in ``mode``, relative to ``obs``) to sandbox pixels,
    clamped into the screen. Raises ``CoordinateError`` with a model-friendly message
    for unusable values."""
    if x is None or y is None or math.isnan(x) or math.isnan(y):
        raise CoordinateError("coordinates must be numbers")
    if mode != "absolute":
        raise NotImplementedError("T03")
    if x < 0 or y < 0:
        raise CoordinateError(
            f"coordinates ({x}, {y}) are negative; use pixels inside the "
            f"{obs.width}x{obs.height} screenshot"
        )
    width, height = screen_size
    sx = min(max(round(x * obs.scale_x), 0), width - 1)
    sy = min(max(round(y * obs.scale_y), 0), height - 1)
    return sx, sy


def normalize_key(combo: str) -> str:
    """Normalize a key combo to lower-case names joined by ``+`` (``"Ctrl + L"`` →
    ``"ctrl+l"``). Raises ``ActionError`` for an empty combo. (T03 adds aliases.)"""
    parts = [p.strip().lower() for p in combo.split("+")]
    if not combo.strip() or any(not p for p in parts):
        raise ActionError(f"invalid key combo {combo!r}; use names like 'ctrl+l' or 'enter'")
    return "+".join(parts)


def validate_type_text(text: str) -> None:
    """Reject text the ``type_text`` tool should not type."""
    if len(text) > MAX_TYPE_TEXT_CHARS:
        raise ActionError(
            f"text is {len(text)} characters (max {MAX_TYPE_TEXT_CHARS}); "
            "write a file with run_command instead"
        )


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
    """Crop a region (given in model coordinates) from the full-resolution screenshot
    for the ``zoom`` tool. Returns JPEG bytes at most ``ZOOM_MAX_WIDTH`` wide."""
    x1, y1 = to_sandbox_coords(x, y, mode, obs, screen_size)
    x2, y2 = to_sandbox_coords(x + w, y + h, mode, obs, screen_size)
    if x2 <= x1 or y2 <= y1:
        raise CoordinateError("zoom region must have a positive width and height")
    with Image.open(io.BytesIO(png_bytes)) as img:
        region = img.convert("RGB").crop((x1, y1, x2 + 1, y2 + 1))
    scale = min(ZOOM_MAX_WIDTH / region.width, 4.0)
    if scale != 1:
        region = region.resize(
            (max(1, round(region.width * scale)), max(1, round(region.height * scale))),
            Image.Resampling.LANCZOS,
        )
    out = io.BytesIO()
    region.save(out, format="JPEG", quality=85)
    return out.getvalue()


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
