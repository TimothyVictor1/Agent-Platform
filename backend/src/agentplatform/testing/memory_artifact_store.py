"""In-memory ``ArtifactStore`` for tests."""

from __future__ import annotations

import hashlib

from agentplatform.artifacts.base import StoredObject, content_key, validate_key
from agentplatform.errors import NotFoundError

__all__ = ["InMemoryArtifactStore"]


class InMemoryArtifactStore:
    """Dict-backed ``ArtifactStore`` with the documented key rules."""

    def __init__(self) -> None:
        self._objects: dict[str, tuple[StoredObject, bytes]] = {}

    async def put(self, data: bytes, *, content_type: str, key: str | None = None) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        final_key = validate_key(key if key is not None else content_key(digest))
        obj = StoredObject(
            key=final_key, size_bytes=len(data), sha256=digest, content_type=content_type
        )
        self._objects[final_key] = (obj, bytes(data))
        return obj

    async def get(self, key: str) -> bytes:
        validate_key(key)
        try:
            return self._objects[key][1]
        except KeyError:
            raise NotFoundError(f"object {key!r} not found") from None

    async def stat(self, key: str) -> StoredObject | None:
        validate_key(key)
        entry = self._objects.get(key)
        return entry[0] if entry else None

    async def list(self, prefix: str = "") -> list[StoredObject]:
        return [self._objects[k][0] for k in sorted(self._objects) if k.startswith(prefix)]

    async def delete(self, key: str) -> None:
        validate_key(key)
        self._objects.pop(key, None)
