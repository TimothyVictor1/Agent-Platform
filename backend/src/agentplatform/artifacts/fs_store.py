"""Atomic filesystem objects with metadata and bytes in one replaceable file.

Every directory is opened with O_NOFOLLOW, relative to an already opened parent.
This also prevents symlink swaps between validation and use. Object headers let
stat read metadata without reading the blob; temporary files are never listed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import stat
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from dataclasses import asdict
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from agentplatform.artifacts.base import DEFAULT_CHUNK_BYTES, StoredObject, content_key
from agentplatform.artifacts.keys import validate_storage_key
from agentplatform.errors import InvalidRequestError, NotFoundError


class FileSystemArtifactStore:
    def __init__(self, root: str | Path) -> None:
        raw = Path(root).absolute()
        if ".." in raw.parts:
            raise InvalidRequestError("store root must not contain parent segments")
        self.root = raw

    @contextmanager
    def _directory(self, parts: tuple[str, ...], *, create: bool = False) -> Iterator[int]:
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in (*self.root.parts[1:], *parts):
                if create:
                    with suppress(FileExistsError):
                        os.mkdir(part, mode=0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            # Resolve only after opening without following links; reject escaped roots.
            if self.root.resolve() != self.root:
                raise InvalidRequestError("store root contains a symlink")
            yield fd
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise InvalidRequestError("unsafe or inaccessible store path") from exc
        finally:
            os.close(fd)

    def _open(self, key: str) -> tuple[StoredObject, BinaryIO]:
        """The object's header and its file, positioned at the first byte of data.
        The caller closes the file."""
        parts = validate_storage_key(key).split("/")
        try:
            with self._directory(tuple(parts[:-1])) as directory:
                fd = os.open(
                    parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                stream = os.fdopen(fd, "rb")
                try:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise InvalidRequestError("object must be a regular file")
                    header = json.loads(stream.readline(8192))
                    return StoredObject(**header), stream
                except BaseException:
                    stream.close()
                    raise
        except FileNotFoundError:
            raise NotFoundError("object not found") from None
        except OSError as exc:
            raise InvalidRequestError("unsafe object path") from exc

    def _read(self, key: str, *, metadata_only: bool = False) -> tuple[StoredObject, bytes]:
        obj, stream = self._open(key)
        with stream:
            return obj, b"" if metadata_only else stream.read()

    def _put(self, data: bytes, content_type: str, key: str | None) -> StoredObject:
        digest = hashlib.sha256(data).hexdigest()
        final_key = validate_storage_key(key if key is not None else content_key(digest))
        obj = StoredObject(final_key, len(data), digest, content_type)
        if key is None:
            try:
                current = self._read(final_key, metadata_only=True)[0]
                if current == obj:
                    return current
            except NotFoundError:
                pass
        parts = final_key.split("/")
        with self._directory(tuple(parts[:-1]), create=True) as directory:
            try:
                existing = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(existing.st_mode):
                    raise InvalidRequestError("object must be a regular file")
            except FileNotFoundError:
                pass
            temporary = f".tmp~{uuid4().hex}"
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(json.dumps(asdict(obj)).encode() + b"\n")
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
        return obj

    async def put(self, data: bytes, *, content_type: str, key: str | None = None) -> StoredObject:
        return await asyncio.to_thread(self._put, data, content_type, key)

    async def get(self, key: str) -> bytes:
        return (await asyncio.to_thread(self._read, key))[1]

    async def open_read(
        self, key: str, *, chunk_size: int = DEFAULT_CHUNK_BYTES
    ) -> tuple[StoredObject, AsyncIterator[bytes]]:
        obj, stream = await asyncio.to_thread(self._open, key)

        async def chunks() -> AsyncIterator[bytes]:
            try:
                while block := await asyncio.to_thread(stream.read, chunk_size):
                    yield block
            finally:
                stream.close()

        return obj, chunks()

    async def stat(self, key: str) -> StoredObject | None:
        try:
            return (await asyncio.to_thread(self._read, key, metadata_only=True))[0]
        except NotFoundError:
            return None

    def _keys(self) -> list[str]:
        def walk(directory: int, prefix: str) -> list[str]:
            keys: list[str] = []
            for name in os.listdir(directory):
                if name.startswith(".tmp~"):
                    continue
                info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise InvalidRequestError("symlink in store")
                key = prefix + name
                validate_storage_key(key)
                if stat.S_ISDIR(info.st_mode):
                    child = os.open(
                        name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                    try:
                        keys.extend(walk(child, key + "/"))
                    finally:
                        os.close(child)
                else:
                    keys.append(key)
            return keys

        try:
            with self._directory(()) as directory:
                return walk(directory, "")
        except FileNotFoundError:
            return []

    async def list(self, prefix: str = "") -> list[StoredObject]:
        keys = await asyncio.to_thread(self._keys)
        objects = [await self.stat(key) for key in sorted(keys) if key.startswith(prefix)]
        return [obj for obj in objects if obj is not None]

    def _delete(self, key: str) -> None:
        parts = validate_storage_key(key).split("/")
        try:
            with self._directory(tuple(parts[:-1])) as directory:
                if stat.S_ISLNK(
                    os.stat(parts[-1], dir_fd=directory, follow_symlinks=False).st_mode
                ):
                    raise InvalidRequestError("symlink in store")
                os.unlink(parts[-1], dir_fd=directory)
        except FileNotFoundError:
            pass

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._delete, key)
