"""Artifact metadata and transfers, independent of sandbox providers and T14.

Publication inserts metadata in the event transaction. The database uniqueness
constraint arbitrates versions across processes; a conflicting version is retried.
Board posts follow publication via the existing BoardService protocol.
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from pathlib import PurePosixPath
from typing import Protocol

from agentplatform.artifacts.base import ArtifactStore
from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.common import Actor, new_id, utcnow
from agentplatform.contracts.events import ArtifactFetched, ArtifactPublished, Event, EventDraft
from agentplatform.db.repositories import ArtifactRepository
from agentplatform.db.session import SessionMaker, as_session
from agentplatform.errors import (
    ArtifactTooLargeError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.sandboxes.base import SandboxHandle


class ArtifactMetadata(Protocol):
    async def get(self, artifact_id: str) -> Artifact: ...
    async def latest(self, session_id: str, name: str) -> Artifact | None: ...
    async def insert(self, artifact: Artifact, tx: object) -> None: ...


class DatabaseArtifactMetadata:
    """Adapter for T04 repositories; sessions are opened only for reads."""

    def __init__(self, sessions: SessionMaker) -> None:
        self._sessions = sessions

    async def get(self, artifact_id: str) -> Artifact:
        async with self._sessions() as tx:
            return await ArtifactRepository(tx).get(artifact_id)

    async def latest(self, session_id: str, name: str) -> Artifact | None:
        async with self._sessions() as tx:
            return await ArtifactRepository(tx).latest(session_id, name)

    async def insert(self, artifact: Artifact, tx: object) -> None:
        await ArtifactRepository(as_session(tx)).insert(artifact)


def sandbox_path(path: str, *, shared: bool = False) -> str:
    """Lexical validation only. The sandbox control server enforces real containment."""
    root = PurePosixPath("/home/agent/Shared" if shared else "/home/agent")
    parsed = PurePosixPath(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or ".." in path.split("/")
        or "\\" in path
        or any(ord(c) < 32 for c in path)
        or not parsed.is_relative_to(root)
    ):
        raise InvalidRequestError("path must be inside " + str(root))
    return str(parsed)


def artifact_name(name: str) -> str:
    # ASCII names avoid separators, normalization surprises and misleading controls.
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9._ -]{0,254}", name):
        raise InvalidRequestError("artifact name must be a safe ASCII filename")
    return name


def sniff_mime(data: bytes, name: str) -> str:
    for signature, mime in (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"%PDF-", "application/pdf"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"PK\x03\x04", "application/zip"),
    ):
        if data.startswith(signature):
            return mime
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return "application/octet-stream"
    if "\x00" in text:
        return "application/octet-stream"
    guessed = mimetypes.guess_type(name)[0]
    if guessed and (guessed.startswith("text/") or guessed == "application/json"):
        return guessed
    return "text/plain"


class ArtifactService:
    def __init__(
        self, metadata: ArtifactMetadata, events: EventRecorder, *, max_bytes: int = 50_000_000
    ) -> None:
        if max_bytes < 1:
            raise InvalidRequestError("artifact limit must be positive")
        self.metadata = metadata
        self.events = events
        self.max_bytes = max_bytes

    async def publish(
        self,
        *,
        store: ArtifactStore,
        board: BoardService,
        sandbox: SandboxHandle,
        session_id: str,
        actor: Actor,
        task_key: str,
        step: int,
        path: str,
        name: str | None = None,
        description: str | None = None,
    ) -> Artifact:
        path = sandbox_path(path)
        name = artifact_name(name if name is not None else PurePosixPath(path).name)
        if description is not None and len(description) > 500:
            raise InvalidRequestError("description exceeds 500 characters")
        # list_dir gives a provider-independent early size check; read_file enforces
        # the bound again if the file changes between the listing and read.
        parent = sandbox_path(str(PurePosixPath(path).parent))
        listing = await sandbox.list_dir(parent)
        info = next((item for item in listing if item.name == PurePosixPath(path).name), None)
        if info is not None and info.size_bytes > self.max_bytes:
            raise ArtifactTooLargeError("artifact exceeds size limit")
        data = await sandbox.read_file(path, max_bytes=self.max_bytes)
        if len(data) > self.max_bytes:
            raise ArtifactTooLargeError("artifact exceeds size limit")
        stored = await store.put(data, content_type=await asyncio.to_thread(sniff_mime, data, name))
        for attempt in range(5):
            latest = await self.metadata.latest(session_id, name)
            artifact = Artifact(
                id=new_id(),
                session_id=session_id,
                name=name,
                mime=stored.content_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
                produced_by=actor,
                task_key=task_key,
                source_path=path,
                storage_key=stored.key,
                version=1 if latest is None else latest.version + 1,
                description=description,
                created_at=utcnow(),
            )

            async def insert(event: Event, tx: object, artifact: Artifact = artifact) -> None:
                await self.metadata.insert(artifact, tx)

            try:
                await self.events.record(
                    session_id,
                    EventDraft(actor=actor, payload=ArtifactPublished(artifact=artifact)),
                    insert,
                )
                break
            except ConflictError:
                if attempt == 4:
                    raise
        await board.post(
            session_id,
            BoardEntryDraft(
                kind="artifact",
                title=name[:120],
                body=description or "",
                artifact_id=artifact.id,
                task_key=task_key,
            ),
            author=actor,
            step=step,
        )
        return artifact

    async def fetch(
        self,
        artifact_id: str,
        *,
        store: ArtifactStore,
        sandbox: SandboxHandle,
        session_id: str,
        actor: Actor,
        agent_id: str,
        directory: str = "/home/agent/Shared",
    ) -> str:
        directory = sandbox_path(directory, shared=True)
        try:
            artifact = await self.metadata.get(artifact_id)
        except NotFoundError:
            # Hide repository-specific details so callers cannot distinguish an
            # unknown ID from an artifact owned by a different session.
            raise NotFoundError("artifact not found in this session") from None
        if artifact.session_id != session_id:
            raise NotFoundError("artifact not found in this session")
        name = artifact_name(artifact.name)
        if artifact.size_bytes > self.max_bytes:
            raise ArtifactTooLargeError("artifact exceeds size limit")
        data = await store.get(artifact.storage_key)
        if len(data) > self.max_bytes:
            raise ArtifactTooLargeError("artifact exceeds size limit")
        if len(data) != artifact.size_bytes or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise InvalidRequestError("artifact content does not match metadata")
        path = sandbox_path(directory + "/" + name, shared=True)
        await sandbox.write_file(path, data)
        await self.events.record(
            session_id,
            EventDraft(
                actor=actor,
                payload=ArtifactFetched(
                    artifact_id=artifact_id, agent_id=agent_id, sandbox_path=path
                ),
            ),
        )
        return path
