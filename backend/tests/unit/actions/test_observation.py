"""prepare_observation: resize, JPEG, pHash, bad input (SPEC §10.5, T03)."""

from __future__ import annotations

import io
import struct
import warnings
import zlib

import imagehash
import pytest
from PIL import Image

from agentplatform.errors import ActionError, SandboxError
from agentplatform.runtime.agent.actions import crop_region, prepare_observation
from agentplatform.testing import make_png


@pytest.mark.parametrize(
    ("src", "target", "size", "scale"),
    [
        ((1280, 800), 1024, (1024, 640), (1.25, 1.25)),
        ((1920, 1080), 1280, (1280, 720), (1.5, 1.5)),
        ((1280, 800), 1280, (1280, 800), (1.0, 1.0)),  # same width: untouched
        ((800, 600), 1280, (800, 600), (1.0, 1.0)),  # never upscaled
        ((3000, 10), 100, (100, 1), (30.0, 10.0)),  # height never drops to 0
    ],
)
def test_resize_keeps_aspect_and_records_scale(
    src: tuple[int, int],
    target: int,
    size: tuple[int, int],
    scale: tuple[float, float],
) -> None:
    obs = prepare_observation(make_png(*src, seed=4), target)
    assert (obs.width, obs.height) == size
    assert (obs.scale_x, obs.scale_y) == pytest.approx(scale)
    assert (obs.source_width, obs.source_height) == src
    with Image.open(io.BytesIO(obs.jpeg)) as img:
        assert img.format == "JPEG"
        assert img.size == size


def _png(mode: str) -> bytes:
    img = Image.new(mode, (64, 40))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


@pytest.mark.parametrize("mode", ["RGBA", "P", "L", "LA", "1"])
def test_any_png_mode_becomes_rgb_jpeg(mode: str) -> None:
    obs = prepare_observation(_png(mode), 32)
    with Image.open(io.BytesIO(obs.jpeg)) as img:
        assert img.mode == "RGB"
        assert img.size == (32, 20)


def test_jpeg_quality_is_used() -> None:
    png = make_png(seed=5)
    assert len(prepare_observation(png, 1280, 95).jpeg) > len(
        prepare_observation(png, 1280, 20).jpeg
    )


def test_phash_is_hex_stable_and_discriminating() -> None:
    a1 = prepare_observation(make_png(seed=1), 1280).phash
    a2 = prepare_observation(make_png(seed=1), 1280).phash
    b = prepare_observation(make_png(seed=7), 1280).phash
    assert len(a1) == 16
    int(a1, 16)  # valid hex
    assert a1 == a2
    assert a1 != b
    assert imagehash.hex_to_hash(a1) - imagehash.hex_to_hash(b) > 2


def test_phash_survives_downscaling() -> None:
    png = make_png(seed=3)
    full = imagehash.hex_to_hash(prepare_observation(png, 1280).phash)
    small = imagehash.hex_to_hash(prepare_observation(png, 640).phash)
    assert full - small <= 2


@pytest.mark.parametrize("target", [0, -1])
def test_bad_target_width_raises(target: int) -> None:
    with pytest.raises(ActionError, match="target width"):
        prepare_observation(make_png(), target)


def _bomb_png() -> bytes:
    """A tiny PNG whose header claims 20000x20000 pixels (a decompression bomb)."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", 20000, 20000, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )


@pytest.mark.parametrize(
    "data",
    [b"", b"not a png", make_png()[:200], _bomb_png()],
    ids=["empty", "garbage", "truncated", "bomb"],
)
def test_invalid_screenshot_is_a_sandbox_error(data: bytes) -> None:
    with pytest.raises(SandboxError, match="not a valid image"):
        prepare_observation(data, 1280)


# -- decompression bombs (T03 review) ------------------------------------------------------

BOMB_W, BOMB_H = 40, 30  # 1200 px: above a 1000 px limit, below Pillow's 2x hard error


@pytest.fixture
def low_pixel_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)


@pytest.mark.usefixtures("low_pixel_limit")
def test_decompression_bomb_warning_is_a_sandbox_error() -> None:
    png = make_png(BOMB_W, BOMB_H)
    with (
        pytest.warns(Image.DecompressionBombWarning),
        pytest.raises(SandboxError, match="too large"),
    ):
        prepare_observation(png, 1280)  # was decoded despite the warning


@pytest.mark.usefixtures("low_pixel_limit")
def test_decompression_bomb_warning_escalated_by_filters_is_a_sandbox_error() -> None:
    png = make_png(BOMB_W, BOMB_H)
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with pytest.raises(SandboxError, match="not a valid image"):
            prepare_observation(png, 1280)


@pytest.mark.usefixtures("low_pixel_limit")
def test_crop_region_rejects_a_decompression_bomb_too() -> None:
    obs = prepare_observation(make_png(20, 20), 1280)  # 400 px: under the limit
    with (
        pytest.warns(Image.DecompressionBombWarning),
        pytest.raises(SandboxError, match="too large"),
    ):
        crop_region(make_png(BOMB_W, BOMB_H), 0, 0, 10, 10, "absolute", obs, (BOMB_W, BOMB_H))


def test_no_pixel_limit_means_no_bomb_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", None)
    assert prepare_observation(make_png(BOMB_W, BOMB_H), 1280).width == BOMB_W
