"""T00 baseline of the action layer (absolute mode). T03 adds the full table-driven
suite; these tests pin the public behaviour other tasks rely on."""

from __future__ import annotations

import io
import math

import pytest
from PIL import Image

from agentplatform.contracts.model_io import ModelResponse, ToolCall
from agentplatform.errors import ActionError, CoordinateError
from agentplatform.runtime.agent.actions import (
    crop_region,
    normalize_key,
    normalize_tool_calls,
    prepare_observation,
    to_sandbox_coords,
    validate_type_text,
)
from agentplatform.testing import make_png


def test_prepare_observation_downscales_keeping_aspect() -> None:
    obs = prepare_observation(make_png(1280, 800, seed=1), target_width=1024)
    assert (obs.width, obs.height) == (1024, 640)
    assert obs.scale_x == pytest.approx(1.25)
    assert obs.scale_y == pytest.approx(1.25)
    assert (obs.source_width, obs.source_height) == (1280, 800)
    with Image.open(io.BytesIO(obs.jpeg)) as img:
        assert img.format == "JPEG"
        assert img.size == (1024, 640)


def test_prepare_observation_never_upscales() -> None:
    obs = prepare_observation(make_png(800, 600, seed=1), target_width=1280)
    assert (obs.width, obs.height) == (800, 600)
    assert obs.scale_x == 1.0


def test_phash_stable_and_discriminating() -> None:
    a1 = prepare_observation(make_png(seed=1), 1280).phash
    a2 = prepare_observation(make_png(seed=1), 1280).phash
    b = prepare_observation(make_png(seed=7), 1280).phash
    assert a1 == a2
    assert a1 != b


def test_absolute_coords_scale_and_clamp() -> None:
    obs = prepare_observation(make_png(1280, 800), target_width=1024)
    assert to_sandbox_coords(512, 320, "absolute", obs, (1280, 800)) == (640, 400)
    assert to_sandbox_coords(5000, 10, "absolute", obs, (1280, 800)) == (1279, 12)


@pytest.mark.parametrize(("x", "y"), [(None, 1), (1, None), (math.nan, 1), (-3, 4)])
def test_bad_coords_raise_coordinate_error(x: float | None, y: float | None) -> None:
    obs = prepare_observation(make_png(), 1280)
    with pytest.raises(CoordinateError):
        to_sandbox_coords(x, y, "absolute", obs, (1280, 800))


def test_other_modes_are_left_to_t03() -> None:
    obs = prepare_observation(make_png(), 1280)
    with pytest.raises(NotImplementedError, match="T03"):
        to_sandbox_coords(1, 1, "normalized_1000", obs, (1280, 800))


def test_normalize_key_basic() -> None:
    assert normalize_key("Ctrl + Shift + T") == "ctrl+shift+t"
    assert normalize_key("enter") == "enter"
    for bad in ("", "  ", "ctrl+", "+"):
        with pytest.raises(ActionError):
            normalize_key(bad)


def test_validate_type_text() -> None:
    validate_type_text("x" * 2000)
    with pytest.raises(ActionError, match="run_command"):
        validate_type_text("x" * 2001)


def test_crop_region_returns_jpeg() -> None:
    png = make_png(1280, 800, seed=3)
    obs = prepare_observation(png, 1280)
    jpeg = crop_region(png, 100, 100, 200, 100, "absolute", obs, (1280, 800))
    with Image.open(io.BytesIO(jpeg)) as img:
        assert img.format == "JPEG"
        assert img.width <= 1280
        assert img.width > 200  # small regions are enlarged for reading
    with pytest.raises(CoordinateError):
        crop_region(png, 100, 100, 0, 10, "absolute", obs, (1280, 800))


def test_normalize_tool_calls_prefers_structured() -> None:
    structured = ModelResponse(tool_calls=[ToolCall(id="1", name="click", arguments={})])
    calls, fallback = normalize_tool_calls(structured, ["click"], fallback=lambda t, n: [])
    assert [c.name for c in calls] == ["click"]
    assert fallback is False


def test_normalize_tool_calls_uses_fallback_parser() -> None:
    seen: list[tuple[str, list[str]]] = []

    def parser(text: str, names: object) -> list[ToolCall]:
        seen.append((text, sorted(names)))  # type: ignore[call-overload]
        return [ToolCall(id="p1", name="click", arguments={"x": 1, "y": 2})]

    calls, used = normalize_tool_calls(
        ModelResponse(text="click(1, 2)"), ["click", "key"], fallback=parser
    )
    assert used is True
    assert calls[0].arguments == {"x": 1, "y": 2}
    assert seen == [("click(1, 2)", ["click", "key"])]
    assert normalize_tool_calls(ModelResponse(text="hmm"), ["click"]) == ([], False)
