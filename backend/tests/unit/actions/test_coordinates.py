"""to_sandbox_coords for every coordinate mode and resized observations (SPEC §8.2,
§10.5, T03)."""

from __future__ import annotations

import math
from functools import cache
from typing import Any

import pytest

from agentplatform.contracts.common import CoordinateMode
from agentplatform.errors import CoordinateError
from agentplatform.runtime.agent.actions import Observation, prepare_observation, to_sandbox_coords
from agentplatform.testing import make_png

SMALL = (1280, 800)
HD = (1920, 1080)


@cache
def obs_for(screen: tuple[int, int], target_width: int) -> Observation:
    return prepare_observation(make_png(*screen, seed=2), target_width)


# (screen, target_width) -> observation size, so each table row says what the model saw
LAYOUTS = {
    "1280x800@1024": (SMALL, 1024),  # observation 1024x640
    "1920x1080@1280": (HD, 1280),  # observation 1280x720
    "1280x800@1280": (SMALL, 1280),  # not resized
}

CASES: list[tuple[str, CoordinateMode, float, float, tuple[int, int]]] = [
    # absolute: pixels in the observation the model saw
    ("1280x800@1024", "absolute", 0, 0, (0, 0)),
    ("1280x800@1024", "absolute", 512, 320, (640, 400)),
    ("1280x800@1024", "absolute", 100, 60, (125, 75)),
    ("1280x800@1024", "absolute", 1024, 640, (1279, 799)),
    ("1920x1080@1280", "absolute", 640, 360, (960, 540)),
    ("1920x1080@1280", "absolute", 100, 100, (150, 150)),
    ("1920x1080@1280", "absolute", 1280, 720, (1919, 1079)),
    ("1280x800@1280", "absolute", 321, 123, (321, 123)),
    ("1280x800@1280", "absolute", 1279, 799, (1279, 799)),
    ("1280x800@1024", "absolute", 99.7, 0.4, (125, 0)),  # floats round to the nearest pixel
    # normalized_1000: 0..1000 on both axes, independent of the observation size
    ("1280x800@1024", "normalized_1000", 0, 0, (0, 0)),
    ("1280x800@1024", "normalized_1000", 500, 500, (640, 400)),
    ("1280x800@1024", "normalized_1000", 250, 100, (320, 80)),
    ("1280x800@1024", "normalized_1000", 1000, 1000, (1279, 799)),
    ("1920x1080@1280", "normalized_1000", 500, 500, (960, 540)),
    ("1920x1080@1280", "normalized_1000", 100, 250, (192, 270)),
    ("1280x800@1280", "normalized_1000", 750, 250, (960, 200)),
    # relative: 0..1 floats
    ("1280x800@1024", "relative", 0, 0, (0, 0)),
    ("1280x800@1024", "relative", 0.5, 0.5, (640, 400)),
    ("1280x800@1024", "relative", 0.25, 0.1, (320, 80)),
    ("1280x800@1024", "relative", 1, 1, (1279, 799)),
    ("1920x1080@1280", "relative", 0.75, 0.5, (1440, 540)),
    ("1280x800@1280", "relative", 0.1, 0.9, (128, 720)),
]


@pytest.mark.parametrize(
    ("layout", "mode", "x", "y", "expected"),
    CASES,
    ids=[f"{c[0]}-{c[1]}-{c[2]},{c[3]}" for c in CASES],
)
def test_modes_map_to_sandbox_pixels(
    layout: str, mode: CoordinateMode, x: float, y: float, expected: tuple[int, int]
) -> None:
    screen, target = LAYOUTS[layout]
    assert to_sandbox_coords(x, y, mode, obs_for(screen, target), screen) == expected


CLAMPED: list[tuple[CoordinateMode, float, float, tuple[int, int]]] = [
    ("absolute", 1536, 900, (1279, 799)),  # 1.5x the 1024x640 observation
    ("absolute", 2048, 1280, (1279, 799)),  # exactly 2x is still clamped
    ("absolute", -5, -2, (0, 0)),  # small negative slack
    ("absolute", -3, 320, (0, 400)),
    ("normalized_1000", 1500, -5, (1279, 0)),
    ("normalized_1000", 2000, 2000, (1279, 799)),
    ("relative", 1.5, -0.005, (1279, 0)),
    ("relative", 2, 2, (1279, 799)),
]


@pytest.mark.parametrize(("mode", "x", "y", "expected"), CLAMPED)
def test_slightly_outside_is_clamped_into_the_screen(
    mode: CoordinateMode, x: float, y: float, expected: tuple[int, int]
) -> None:
    assert to_sandbox_coords(x, y, mode, obs_for(SMALL, 1024), SMALL) == expected


OUT_OF_RANGE: list[tuple[CoordinateMode, float, float, str]] = [
    ("absolute", 2049, 0, "1024x640 screenshot"),
    ("absolute", 0, 1281, "y=1281"),
    ("absolute", -5.1, 0, "x=-5.1"),
    ("absolute", 5000, 10, "outside the screen"),
    ("normalized_1000", 2001, 0, "0..1000"),
    ("normalized_1000", 0, -6, "y=-6"),
    ("relative", 0, -0.01, "decimals 0..1"),
    ("relative", 2.5, 0, "did you give pixels?"),
    ("relative", 512, 300, "did you give pixels?"),
]


@pytest.mark.parametrize(("mode", "x", "y", "message"), OUT_OF_RANGE)
def test_far_outside_raises_a_model_friendly_error(
    mode: CoordinateMode, x: float, y: float, message: str
) -> None:
    with pytest.raises(CoordinateError, match=message.replace("?", r"\?")):
        to_sandbox_coords(x, y, mode, obs_for(SMALL, 1024), SMALL)


def test_pixel_hint_only_for_relative_mode() -> None:
    with pytest.raises(CoordinateError) as exc:
        to_sandbox_coords(5000, 0, "absolute", obs_for(SMALL, 1024), SMALL)
    assert "did you give pixels" not in exc.value.detail
    with pytest.raises(CoordinateError) as exc:
        to_sandbox_coords(0, -0.5, "relative", obs_for(SMALL, 1024), SMALL)
    assert "did you give pixels" not in exc.value.detail


BAD_VALUES: list[tuple[Any, Any, str]] = [
    (None, 1, "x must be a number"),
    (1, None, "y must be a number"),
    ("12", 1, "x must be a number"),
    (True, 1, "x must be a number"),
    (1, [2], "y must be a number"),
    (math.nan, 1, "x must be a finite number"),
    (1, math.nan, "y must be a finite number"),
    (math.inf, 1, "x must be a finite number"),
    (1, -math.inf, "y must be a finite number"),
]


@pytest.mark.parametrize("mode", ["absolute", "normalized_1000", "relative"])
@pytest.mark.parametrize(("x", "y", "message"), BAD_VALUES)
def test_non_numbers_raise(mode: CoordinateMode, x: Any, y: Any, message: str) -> None:
    with pytest.raises(CoordinateError, match=message):
        to_sandbox_coords(x, y, mode, obs_for(SMALL, 1024), SMALL)


@pytest.mark.parametrize("mode", ["absolute", "normalized_1000", "relative"])
@pytest.mark.parametrize(
    ("layout", "expected"),
    [("1280x800@1024", (640, 400)), ("1920x1080@1280", (960, 540))],
)
@pytest.mark.parametrize("value", [-1, -1.0])
def test_minus_one_minus_one_is_the_screen_centre(
    mode: CoordinateMode, layout: str, expected: tuple[int, int], value: float
) -> None:
    screen, target = LAYOUTS[layout]
    assert to_sandbox_coords(value, value, mode, obs_for(screen, target), screen) == expected


def test_a_single_minus_one_is_not_the_centre() -> None:
    obs = obs_for(SMALL, 1024)
    assert to_sandbox_coords(-1, 80, "absolute", obs, SMALL) == (0, 100)
    assert to_sandbox_coords(500, -1, "normalized_1000", obs, SMALL) == (640, 0)
    with pytest.raises(CoordinateError):
        to_sandbox_coords(-1, 0.5, "relative", obs, SMALL)


def test_screen_size_drives_the_result_not_the_screenshot_size() -> None:
    """A screenshot whose size differs from the screen (e.g. HiDPI capture) still maps
    onto the screen the sandbox clicks in."""
    obs = obs_for(HD, 1280)  # 1280x720 observation of a 1920x1080 capture
    assert to_sandbox_coords(640, 360, "absolute", obs, (3840, 2160)) == (1920, 1080)
    assert to_sandbox_coords(0.5, 0.5, "relative", obs, (3840, 2160)) == (1920, 1080)


def test_result_is_always_int_inside_the_screen() -> None:
    obs = obs_for(HD, 1280)
    for mode, points in (
        ("absolute", [(0, 0), (1279.9, 719.9), (2560, 1440), (-4.9, -4.9)]),
        ("normalized_1000", [(999.999, 0.001), (2000, 2000)]),
        ("relative", [(0.99999, 0.00001), (2, 2)]),
    ):
        for x, y in points:
            sx, sy = to_sandbox_coords(x, y, mode, obs, HD)  # type: ignore[arg-type]
            assert isinstance(sx, int)
            assert isinstance(sy, int)
            assert 0 <= sx < HD[0]
            assert 0 <= sy < HD[1]
