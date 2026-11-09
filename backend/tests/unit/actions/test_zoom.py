"""crop_region for the zoom tool, in every coordinate mode (SPEC §10.3, §10.5, T03)."""

from __future__ import annotations

import io
import math
from functools import cache
from typing import Any

import pytest
from PIL import Image

from agentplatform.contracts.common import CoordinateMode
from agentplatform.errors import CoordinateError, SandboxError
from agentplatform.runtime.agent.actions import crop_region, prepare_observation
from agentplatform.testing import make_png

SMALL = (1280, 800)
HD = (1920, 1080)


@cache
def png_for(screen: tuple[int, int]) -> bytes:
    return make_png(*screen, seed=6)


def zoom_size(
    screen: tuple[int, int],
    target: int,
    mode: CoordinateMode,
    region: tuple[float, float, float, float],
) -> tuple[int, int]:
    png = png_for(screen)
    obs = prepare_observation(png, target)
    jpeg = crop_region(png, *region, mode, obs, screen)
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.format == "JPEG"
        return img.size


@pytest.mark.parametrize(
    ("screen", "target", "mode", "region", "size"),
    [
        # 200x100 px region, enlarged 4x for reading
        (SMALL, 1280, "absolute", (100, 100, 200, 100), (800, 400)),
        # observation 1024 wide: 256x160 obs px = 320x200 screen px, then 4x
        (SMALL, 1024, "absolute", (0, 0, 256, 160), (1280, 800)),
        # 960x540 px region, enlarged until 1280 wide
        (HD, 1280, "normalized_1000", (0, 0, 500, 500), (1280, 720)),
        # whole 1920x1080 screen, shrunk to 1280 wide
        (HD, 1280, "relative", (0, 0, 1, 1), (1280, 720)),
        # whole 1280x800 screen: already fits, left as is
        (SMALL, 1280, "relative", (0, 0, 1, 1), (1280, 800)),
        # tall narrow region: height is capped at 1280 too
        (SMALL, 1280, "absolute", (0, 0, 10, 800), (16, 1280)),
        # region running off the screen is clamped (80x100 px), then 4x
        (SMALL, 1280, "absolute", (1200, 700, 200, 200), (320, 400)),
        # (-1, -1) is not the centre here: it clamps to the top-left corner
        (SMALL, 1280, "absolute", (-1, -1, 100, 100), (396, 396)),
    ],
)
def test_crop_sizes(
    screen: tuple[int, int],
    target: int,
    mode: CoordinateMode,
    region: tuple[float, float, float, float],
    size: tuple[int, int],
) -> None:
    out = zoom_size(screen, target, mode, region)
    assert out == size
    assert out[0] <= 1280
    assert out[1] <= 1280


def test_crop_takes_pixels_from_the_requested_region() -> None:
    img = Image.new("RGB", SMALL, (255, 255, 255))
    img.paste((255, 0, 0), (640, 400, 1280, 800))  # bottom-right quarter is red
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    png = buf.getvalue()
    obs = prepare_observation(png, 1024)

    def centre_pixel(x: float, y: float) -> tuple[int, int, int]:
        jpeg = crop_region(png, x, y, 100, 100, "normalized_1000", obs, SMALL)
        with Image.open(io.BytesIO(jpeg)) as out:
            rgb = out.convert("RGB")
            pixel = rgb.getpixel((rgb.width // 2, rgb.height // 2))
        assert isinstance(pixel, tuple)
        return pixel[0], pixel[1], pixel[2]

    r, g, b = centre_pixel(600, 600)  # pixels 768..896 x 480..560: red
    assert (r > 200, g < 60, b < 60) == (True, True, True)
    r, g, b = centre_pixel(100, 100)  # top-left: white
    assert min(r, g, b) > 200


BAD_REGIONS: list[tuple[CoordinateMode, Any, Any, Any, Any, str]] = [
    ("absolute", 100, 100, 0, 10, "positive width and height"),
    ("absolute", 100, 100, 10, -1, "positive width and height"),
    ("absolute", 100, 100, math.nan, 10, "w must be a finite number"),
    ("absolute", 100, 100, 10, None, "h must be a number"),
    ("absolute", None, 100, 10, 10, "x must be a number"),
    ("absolute", 1280, 0, 100, 100, "empty or outside the screen"),
    ("absolute", 0, 900, 100, 100, "empty or outside the screen"),
    ("absolute", 2500, 0, 200, 10, r"x \+ w=2700"),
    ("normalized_1000", 0, 1900, 10, 200, r"y \+ h=2100"),
    ("relative", 0, 0, 3, 0.5, "did you give pixels"),
]


@pytest.mark.parametrize(("mode", "x", "y", "w", "h", "message"), BAD_REGIONS)
def test_bad_regions_raise(
    mode: CoordinateMode, x: Any, y: Any, w: Any, h: Any, message: str
) -> None:
    png = png_for(SMALL)
    obs = prepare_observation(png, 1280)
    with pytest.raises(CoordinateError, match=message):
        crop_region(png, x, y, w, h, mode, obs, SMALL)


def test_invalid_png_is_a_sandbox_error() -> None:
    obs = prepare_observation(png_for(SMALL), 1280)
    with pytest.raises(SandboxError):
        crop_region(b"nope", 0, 0, 10, 10, "absolute", obs, SMALL)
