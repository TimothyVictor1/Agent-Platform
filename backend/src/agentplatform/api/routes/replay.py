"""Replay reads: events, frame metadata and frame JPEGs (SPEC §16 "Replay")."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response
from sqlalchemy import select

from agentplatform.api.deps import Deps, Tx
from agentplatform.api.routes._common import load_session
from agentplatform.contracts.api import EventPage, FrameList
from agentplatform.contracts.artifact import Frame
from agentplatform.db.models import FrameRow
from agentplatform.db.repositories import FrameRepository
from agentplatform.errors import InvalidRequestError

__all__ = ["FRAME_CACHE_CONTROL", "router"]

router = APIRouter(tags=["replay"])

FRAME_CACHE_CONTROL = "private, max-age=31536000, immutable"
"""Frames never change once written, so browsers may cache them for good."""


@router.get("/sessions/{session_id}/events", response_model=EventPage)
async def list_events(
    session_id: str,
    tx: Tx,
    deps: Deps,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> EventPage:
    await load_session(tx, session_id)
    items = await deps.events.read(session_id, after_seq, limit=limit)
    return EventPage(items=items, next_after_seq=items[-1].seq if len(items) == limit else None)


def _frame(row: FrameRow) -> Frame:
    return Frame(
        id=row.id,
        session_id=row.session_id,
        agent_id=row.agent_id,
        step=row.step,
        task_key=row.task_key,
        ts=row.ts,
        storage_key_small=row.storage_key_small,
        storage_key_full=row.storage_key_full,
        action_label=row.action_label,
        phash=row.phash,
    )


@router.get("/sessions/{session_id}/frames", response_model=FrameList)
async def list_frames(
    session_id: str,
    tx: Tx,
    agent_id: Annotated[str | None, Query(max_length=64)] = None,
    from_step: Annotated[int | None, Query(ge=0)] = None,
    to_step: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=5000)] = 2000,
) -> FrameList:
    """Frame metadata in time order; ``from_step``/``to_step`` are inclusive."""
    await load_session(tx, session_id)
    if from_step is not None and to_step is not None and from_step > to_step:
        raise InvalidRequestError(
            "from_step must not exceed to_step",
            errors=[{"message": "from_step > to_step", "loc": ["query", "from_step"]}],
        )
    stmt = select(FrameRow).where(FrameRow.session_id == session_id)
    if agent_id is not None:
        stmt = stmt.where(FrameRow.agent_id == agent_id)
    if from_step is not None:
        stmt = stmt.where(FrameRow.step >= from_step)
    if to_step is not None:
        stmt = stmt.where(FrameRow.step <= to_step)
    stmt = stmt.order_by(FrameRow.ts, FrameRow.id).limit(limit)
    return FrameList(items=[_frame(r) for r in (await tx.scalars(stmt)).all()])


@router.get(
    "/frames/{frame_id}",
    response_class=Response,
    responses={200: {"content": {"image/jpeg": {}}}, 304: {"description": "Not modified"}},
)
async def get_frame(frame_id: str, request: Request, tx: Tx, deps: Deps) -> Response:
    frame = await FrameRepository(tx).get(frame_id)
    etag = f'"{frame.id}"'
    headers = {
        "Cache-Control": FRAME_CACHE_CONTROL,
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if etag in [t.strip() for t in request.headers.get("if-none-match", "").split(",")]:
        return Response(status_code=304, headers=headers)
    data = await deps.artifacts.get(frame.storage_key_small)
    return Response(content=data, media_type="image/jpeg", headers=headers)
