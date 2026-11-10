"""Artifacts: list, user upload, download (SPEC §16 "Artifacts", §13.3).

Uploads are multipart (``file`` + optional ``description``), at most
``max_upload_bytes`` (50 MB). ``BodySizeLimitMiddleware`` stops oversized bodies
while they stream in; the route checks the file size again. The MIME type is sniffed
from the bytes (``sniff_mime``), never taken from the client.

The upload is the one write the API does itself: it stores the bytes and records
``artifact.published`` (actor = user, no causing command, so ``causation_id`` is
``None``), inserting the artifact row in the same transaction.

Downloads are always ``Content-Disposition: attachment`` with ``nosniff`` and a
sandboxing CSP, and active content types are served as ``application/octet-stream``.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import AsyncIterator
from pathlib import PurePosixPath, PureWindowsPath
from typing import Final
from urllib.parse import quote

from fastapi import APIRouter, Request, status
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile

from agentplatform.api.deps import Deps, Tx
from agentplatform.api.routes._common import load_session
from agentplatform.artifacts.service import artifact_name, sniff_mime
from agentplatform.contracts.api import ArtifactList
from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.common import Actor, new_id, utcnow
from agentplatform.contracts.events import ArtifactPublished, Event, EventDraft
from agentplatform.db.repositories import ArtifactRepository
from agentplatform.db.session import as_session
from agentplatform.errors import ArtifactTooLargeError, ConflictError, InvalidRequestError

__all__ = ["download_media_type", "router", "safe_upload_name"]

router = APIRouter(tags=["artifacts"])

CHUNK: Final = 64 * 1024
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")
_PASSIVE_TYPES: Final = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "application/pdf",
        "application/zip",
        "application/json",
        "text/plain",
        "text/csv",
        "text/markdown",
    }
)


def safe_upload_name(filename: str | None) -> str:
    """A safe ASCII artifact name from a client-supplied filename (path parts dropped,
    other characters replaced by ``_``)."""
    raw = filename or ""
    base = PureWindowsPath(PurePosixPath(raw).name).name
    ascii_name = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode()
    cleaned = _UNSAFE_CHARS.sub("_", ascii_name).strip(" .")
    cleaned = cleaned.lstrip("-. ")
    if not cleaned:
        cleaned = "upload"
    if not re.match(r"[A-Za-z0-9_]", cleaned[0]):
        cleaned = "_" + cleaned
    return artifact_name(cleaned[:255].rstrip())


def download_media_type(mime: str) -> str:
    """The stored MIME type if it is passive content, else ``application/octet-stream``."""
    base = mime.split(";", 1)[0].strip().lower()
    if base not in _PASSIVE_TYPES:
        return "application/octet-stream"
    return f"{base}; charset=utf-8" if base.startswith("text/") or base.endswith("json") else base


def _content_disposition(name: str) -> str:
    fallback = re.sub(r"[^A-Za-z0-9._ -]", "_", name) or "download"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"


@router.get("/sessions/{session_id}/artifacts", response_model=ArtifactList)
async def list_artifacts(session_id: str, tx: Tx) -> ArtifactList:
    await load_session(tx, session_id)
    return ArtifactList(items=await ArtifactRepository(tx).list(session_id))


@router.post(
    "/sessions/{session_id}/artifacts",
    status_code=status.HTTP_201_CREATED,
    response_model=Artifact,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {
                            "file": {"type": "string", "format": "binary"},
                            "description": {"type": "string", "maxLength": 500},
                        },
                    }
                }
            },
        }
    },
)
async def upload_artifact(session_id: str, request: Request, deps: Deps) -> Artifact:
    async with deps.sessionmaker() as tx:
        await load_session(tx, session_id)
    limit = deps.max_upload_bytes
    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise InvalidRequestError("the upload must be multipart/form-data")
    form = await request.form(max_files=1, max_fields=4, max_part_size=4096)
    try:
        upload = form.get("file")
        if not isinstance(upload, UploadFile):
            raise InvalidRequestError(
                "a file is required",
                errors=[{"message": "missing file", "loc": ["body", "file"]}],
            )
        description = form.get("description")
        if description is not None and not isinstance(description, str):
            raise InvalidRequestError("description must be text")
        if description is not None and len(description) > 500:
            raise InvalidRequestError(
                "description exceeds 500 characters",
                errors=[{"message": "too long", "loc": ["body", "description"]}],
            )
        data = await upload.read(limit + 1)
        if len(data) > limit:
            raise ArtifactTooLargeError(f"the file exceeds {limit // (1024 * 1024)} MB")
        name = safe_upload_name(upload.filename)
    finally:
        await form.close()

    mime = await asyncio.to_thread(sniff_mime, data, name)
    stored = await deps.artifacts.put(data, content_type=mime)
    actor = Actor.user()
    for attempt in range(5):
        async with deps.sessionmaker() as tx:
            version = await ArtifactRepository(tx).next_version(session_id, name)
        artifact = Artifact(
            id=new_id(),
            session_id=session_id,
            name=name,
            mime=stored.content_type,
            size_bytes=stored.size_bytes,
            sha256=stored.sha256,
            produced_by=actor,
            storage_key=stored.key,
            version=version,
            description=description or None,
            created_at=utcnow(),
        )

        async def insert(event: Event, tx: object, artifact: Artifact = artifact) -> None:
            await ArtifactRepository(as_session(tx)).insert(artifact)

        try:
            await deps.recorder.record(
                session_id,
                EventDraft(payload=ArtifactPublished(artifact=artifact), actor=actor),
                insert,
            )
        except ConflictError:
            if attempt == 4:
                raise
            continue
        deps.metrics.inc("artifacts.uploaded")
        return artifact
    raise AssertionError("unreachable")  # pragma: no cover


@router.get(
    "/artifacts/{artifact_id}/download",
    response_class=StreamingResponse,
    responses={200: {"content": {"application/octet-stream": {}}}},
)
async def download_artifact(artifact_id: str, tx: Tx, deps: Deps) -> StreamingResponse:
    artifact = await ArtifactRepository(tx).get(artifact_id)
    data = await deps.artifacts.get(artifact.storage_key)

    async def chunks() -> AsyncIterator[bytes]:
        for start in range(0, len(data), CHUNK):
            yield data[start : start + CHUNK]

    return StreamingResponse(
        chunks(),
        media_type=download_media_type(artifact.mime),
        headers={
            "Content-Disposition": _content_disposition(artifact.name),
            "Content-Length": str(len(data)),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Cache-Control": "private, max-age=3600",
            "ETag": f'"{artifact.sha256}"',
        },
    )
