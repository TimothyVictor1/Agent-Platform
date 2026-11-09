"""File transfer endpoints' implementation: write/read/list with validation.

Paths must be absolute and inside the container; uploads are capped at
50 MB and written atomically (temp file in the same directory + rename).
"""
from __future__ import annotations

import os
import tempfile

from .config import MAX_FILE_BYTES, MAX_FILE_READ_BYTES


def validate_absolute(path: str) -> str:
    if "\x00" in path:
        raise ValueError("path must not contain null bytes")
    if not path.startswith("/"):
        raise ValueError(f"path must be absolute, got {path!r}")
    if path.endswith("/"):
        raise ValueError(f"path must name a file, got {path!r}")
    return path


async def write_stream(path: str, chunks) -> int:
    """Consume an async byte stream, cap total size, write atomically."""
    validate_absolute(path)
    directory = os.path.dirname(path) or "/"
    if not os.path.isdir(directory):
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=directory)
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            async for chunk in chunks:
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    raise OverflowError(
                        f"file exceeds the {MAX_FILE_BYTES} byte limit"
                    )
                out.write(chunk)
        os.replace(tmp, path)  # atomic within the same directory
        return total
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_bytes(path: str) -> bytes:
    validate_absolute(path)
    if not os.path.exists(path):
        raise FileNotFoundError(path)
    if os.path.isdir(path):
        raise IsADirectoryError(path)
    size = os.path.getsize(path)
    if size > MAX_FILE_READ_BYTES:
        raise OverflowError(f"file is {size} bytes, limit is {MAX_FILE_READ_BYTES}")
    with open(path, "rb") as fh:
        return fh.read()


def list_dir(path: str) -> list[dict[str, object]]:
    if not os.path.isabs(path):
        raise ValueError(f"path must be absolute, got {path!r}")
    if not os.path.isdir(path):
        raise NotADirectoryError(path)
    entries: list[dict[str, object]] = []
    with os.scandir(path) as it:
        for entry in it:
            try:
                stat = entry.stat(follow_symlinks=False)
            except (PermissionError, OSError):
                continue
            entries.append(
                {
                    "name": entry.name,
                    "size": stat.st_size if not entry.is_dir() else None,
                    "is_dir": entry.is_dir(),
                    "mtime": round(stat.st_mtime, 3),
                }
            )
    entries.sort(key=lambda item: (not item["is_dir"], str(item["name"])))
    return entries
