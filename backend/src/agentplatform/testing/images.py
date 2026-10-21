"""Tiny image helpers for tests (canned screenshots)."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw

__all__ = ["make_png", "solid_png"]


def solid_png(
    width: int = 1280, height: int = 800, color: tuple[int, int, int] = (32, 32, 48)
) -> bytes:
    """A single-color PNG."""
    out = io.BytesIO()
    Image.new("RGB", (width, height), color).save(out, format="PNG")
    return out.getvalue()


def make_png(
    width: int = 1280,
    height: int = 800,
    *,
    seed: int = 0,
    label: str | None = None,
) -> bytes:
    """A deterministic PNG whose content (and perceptual hash) depends on ``seed``.

    Different seeds give visibly different images (stripes at different positions),
    so stuck-detection tests can tell "screen changed" from "screen unchanged".
    """
    img = Image.new("RGB", (width, height), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    band = max(8, height // 10)
    for i in range(6):
        y = ((seed * 37 + i * 101) % max(1, height - band)) if seed else i * band * 2
        shade = (seed * 53 + i * 40) % 200
        draw.rectangle((0, y, width, y + band), fill=(shade, 80 + i * 20, 200 - shade // 2))
    x = (seed * 71) % max(1, width - 200)
    draw.rectangle((x, height // 3, x + 200, height // 3 + 150), fill=(20, 20, 20))
    if label:
        draw.text((20, 20), label, fill=(0, 0, 0))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()
