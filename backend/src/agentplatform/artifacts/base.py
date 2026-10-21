"""ArtifactStore interface: blob storage for artifacts and frames (SPEC §6.5, §13.3).

Implementations (T15): ``fs_store.py`` (default, under ``ARTIFACT_DIR``) and
``s3_store.py`` (S3-compatible). Test double:
``agentplatform.testing.memory_artifact_store.InMemoryArtifactStore``.

The store only holds bytes. Artifact metadata (``contracts.Artifact`` rows, versions,
board entries) is handled by the artifact tools/repositories on top of it.

Keys are ``/``-separated relative paths made of ``[A-Za-z0-9._-]`` segments (no empty
segments, no ``.``/``..``). Conventions:

* artifacts: content-addressed ``sha256/<first 2 hex>/<sha256>`` (``put`` with
  ``key=None``),
* frames: ``frames/<session_id>/<agent_id>/<step>.jpg`` (small) and
  ``frames/<session_id>/<agent_id>/<step>.full.jpg``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from agentplatform.errors import InvalidRequestError

__all__ = [
    "ArtifactStore",
    "StoredObject",
    "content_key",
    "frame_key",
    "validate_key",
]

_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_key(key: str) -> str:
    """Return ``key`` if it is a safe storage key, else raise ``InvalidRequestError``."""
    parts = key.split("/")
    if (
        not key
        or len(key) > 512
        or any(p in {"", ".", ".."} or not _SEGMENT.match(p) for p in parts)
    ):
        raise InvalidRequestError(f"invalid storage key {key!r}")
    return key


def content_key(sha256_hex: str) -> str:
    """Content-addressed key for a blob with the given sha256 (lower-case hex)."""
    return f"sha256/{sha256_hex[:2]}/{sha256_hex}"


def frame_key(session_id: str, agent_id: str, step: int, *, full: bool = False) -> str:
    """Storage key of a saved frame."""
    suffix = "full.jpg" if full else "jpg"
    return validate_key(f"frames/{session_id}/{agent_id}/{step}.{suffix}")


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    size_bytes: int
    sha256: str
    content_type: str


@runtime_checkable
class ArtifactStore(Protocol):
    """Byte storage with atomic writes. Safe for concurrent use."""

    async def put(self, data: bytes, *, content_type: str, key: str | None = None) -> StoredObject:
        """Store ``data``. With ``key=None`` the key is ``content_key(sha256)`` and
        storing identical bytes twice is a cheap no-op. With an explicit key an
        existing object is replaced atomically (readers see old or new, never a mix).
        Raises ``InvalidRequestError`` for bad keys."""
        ...

    async def get(self, key: str) -> bytes:
        """Return the bytes. Raises ``NotFoundError`` if missing."""
        ...

    async def stat(self, key: str) -> StoredObject | None:
        """Metadata without reading the bytes; ``None`` if missing."""
        ...

    async def list(self, prefix: str = "") -> list[StoredObject]:
        """Objects whose key starts with ``prefix``, sorted by key."""
        ...

    async def delete(self, key: str) -> None:
        """Remove the object. No error if it does not exist."""
        ...
