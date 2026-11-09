"""Screen inspection: PNG screenshots via scrot and Xvfb geometry."""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile

from . import input as input_lib
from .config import SCREEN_HEIGHT, SCREEN_WIDTH


def grab_png() -> bytes:
    fd, path = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        proc = subprocess.run(  # noqa: S603 - fixed tool, no user input in args
            ["/usr/bin/scrot", "-o", path],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0 or not os.path.exists(path):
            raise input_lib.ToolError(
                f"scrot failed: {proc.stderr.strip()[:300] or 'no screenshot produced'}"
            )
        with open(path, "rb") as fh:
            return fh.read()
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def asdict() -> dict[str, int]:
    return {"width": SCREEN_WIDTH, "height": SCREEN_HEIGHT}
