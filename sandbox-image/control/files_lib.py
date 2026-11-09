"""File transfer endpoints' implementation: write/read/list with validation.

Paths must be absolute and stay under FILE_ROOT (/home/agent by default,
override with SANDBOX_FILE_ROOT): normalized paths may not traverse out of
the root, and symlinked paths are resolved before use so a link planted
inside the sandbox home cannot become a read lens into the rest of the
container. Uploads are capped at 50 MB and written atomically (temp file in
the same directory + rename).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
from collections.abc import AsyncIterator

from .config import FILE_ROOT, MAX_FILE_BYTES, MAX_FILE_READ_BYTES


def validate_absolute(path: str) -> str:
    if "\x00" in path:
        raise ValueError("path must not contain null bytes")
    if not path.startswith("/"):
        raise ValueError(f"path must be absolute, got {path!r}")
    if path.endswith("/"):
        raise ValueError(f"path must name a file, got {path!r}")
    return path


def _under_root(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root + os.sep)


def check_contained(path: str, *, must_exist: bool) -> str:
    """Validate that *path* stays under FILE_ROOT and return the safe path.

    For existing files (must_exist=True) the fully resolved realpath is
    returned, so the caller opens the verified target itself instead of
    re-resolving a symlink after the check. For write targets the nearest
    existing ancestor is resolved; the final leaf is replaced atomically
    (os.replace never writes through a symlink at the destination).
    """
    validate_absolute(path)
    root = os.path.realpath(FILE_ROOT)
    norm = os.path.normpath(path)
    if not _under_root(norm, root):
        raise ValueError(f"path must stay under {FILE_ROOT}, got {path!r}")

    if must_exist:
        real = os.path.realpath(norm)
        if not _under_root(real, root):
            raise ValueError(f"path resolves outside {FILE_ROOT} (symlink?): {path!r}")
        return real

    probe = os.path.dirname(norm) or "/"
    while probe != "/" and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    real_probe = os.path.realpath(probe)
    if not _under_root(real_probe, root):
        raise ValueError(f"path resolves outside {FILE_ROOT} (symlink?): {path!r}")
    return norm


def _atomic_write(norm: str, buf: bytearray) -> int:
    """Write *buf* to *norm* atomically (temp file in the same dir + rename)."""
    directory = os.path.dirname(norm) or "/"
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as out:
            out.write(buf)
        os.replace(tmp, norm)  # atomic within the same directory
        return len(buf)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def write_stream(path: str, chunks: AsyncIterator[bytes]) -> int:
    """Consume an async byte stream, cap total size, write atomically.

    The body is buffered in memory (bounded by MAX_FILE_BYTES, checked as
    the chunks arrive, so an oversized upload is rejected mid-stream) and
    handed to a worker thread for the blocking file work.
    """
    norm = check_contained(path, must_exist=False)
    buf = bytearray()
    async for chunk in chunks:
        buf.extend(chunk)
        if len(buf) > MAX_FILE_BYTES:
            raise OverflowError(f"file exceeds the {MAX_FILE_BYTES} byte limit")
    return await asyncio.to_thread(_atomic_write, norm, buf)


def read_bytes(path: str) -> bytes:
    # Returns the resolved realpath, so the open below cannot be redirected
    # through a symlink after validation.
    real = check_contained(path, must_exist=True)
    if not os.path.exists(real):
        raise FileNotFoundError(path)
    if os.path.isdir(real):
        raise IsADirectoryError(real)
    size = os.path.getsize(real)
    if size > MAX_FILE_READ_BYTES:
        raise OverflowError(f"file is {size} bytes, limit is {MAX_FILE_READ_BYTES}")
    with open(real, "rb") as fh:
        return fh.read()


def list_dir(path: str) -> list[dict[str, object]]:
    # Listing may name a directory with a trailing slash ("/home/agent/");
    # read and write keep the strict no-trailing-slash rule.
    real = check_contained(path.rstrip("/") or "/", must_exist=True)
    if not os.path.isdir(real):
        raise NotADirectoryError(real)
    entries: list[dict[str, object]] = []
    with os.scandir(real) as it:
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
