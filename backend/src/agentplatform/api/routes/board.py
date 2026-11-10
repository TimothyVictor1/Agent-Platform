"""Board read and user posts (SPEC §16 "Board", §13)."""

from __future__ import annotations

from typing import Annotated, get_args

from fastapi import APIRouter, Query, status

from agentplatform.api.deps import Deps, Tx
from agentplatform.api.routes._common import TERMINAL, load_session
from agentplatform.contracts.api import BoardPage, CommandAccepted, UserBoardPostRequest
from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.common import BoardEntryKind
from agentplatform.db.repositories import AgentRepository, BoardRepository
from agentplatform.errors import BoardRuleError, ConflictError, InvalidRequestError

__all__ = ["router"]

router = APIRouter(tags=["board"])

_KINDS: frozenset[str] = frozenset(get_args(BoardEntryKind))


def _csv(raw: str | None, field: str, *, max_items: int = 20) -> list[str] | None:
    if raw is None or not raw.strip():
        return None
    items = [part.strip() for part in raw.split(",") if part.strip()]
    if len(items) > max_items:
        raise InvalidRequestError(
            f"too many {field}",
            errors=[{"message": f"at most {max_items}", "loc": ["query", field]}],
        )
    return items


def _kinds(raw: str | None) -> list[BoardEntryKind] | None:
    items = _csv(raw, "kinds")
    if items is None:
        return None
    unknown = [k for k in items if k not in _KINDS]
    if unknown:
        raise InvalidRequestError(
            f"unknown board kinds: {', '.join(unknown)[:200]}",
            errors=[{"message": "unknown kind", "loc": ["query", "kinds"]}],
        )
    return [k for k in items if k in _KINDS]  # type: ignore[misc]


@router.get("/sessions/{session_id}/board", response_model=BoardPage)
async def list_board(
    session_id: str,
    tx: Tx,
    kinds: Annotated[str | None, Query(description="Comma-separated kinds")] = None,
    tags: Annotated[str | None, Query(description="Comma-separated tags (any)")] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> BoardPage:
    """Entries in ``seq`` order after ``after_seq``. With ``q``: full-text search, best
    match first, not paginated (``next_after_seq`` is null)."""
    await load_session(tx, session_id)
    kind_list = _kinds(kinds)
    tag_list = _csv(tags, "tags")
    repo = BoardRepository(tx)
    if q and q.strip():
        found = await repo.search(session_id, q, kinds=kind_list, limit=limit)
        if tag_list:
            wanted = set(tag_list)
            found = [e for e in found if wanted.intersection(e.tags)]
        if after_seq:
            found = [e for e in found if e.seq > after_seq]
        return BoardPage(items=found, next_after_seq=None)
    items = await repo.list(
        session_id, after_seq=after_seq, kinds=kind_list, tags=tag_list, limit=limit
    )
    return BoardPage(items=items, next_after_seq=items[-1].seq if len(items) == limit else None)


@router.post(
    "/sessions/{session_id}/board",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def post_board(
    session_id: str, body: UserBoardPostRequest, tx: Tx, deps: Deps
) -> CommandAccepted:
    session = await load_session(tx, session_id)
    if session.status in TERMINAL:
        raise ConflictError(f"cannot post to the board while the session is {session.status}")
    if body.kind == "answer":
        if len(body.refs) != 1:
            raise BoardRuleError("an answer must reference exactly one question")
        question = await BoardRepository(tx).find(body.refs[0])
        if question is None or question.session_id != session_id or question.kind != "question":
            raise BoardRuleError("an answer must reference a question of this session")
        if question.status != "open":
            raise BoardRuleError(f"the question is already {question.status}")
    if body.to:
        agents = {a.id for a in await AgentRepository(tx).list(session_id)}
        unknown = [a for a in body.to if a not in agents]
        if unknown:
            raise InvalidRequestError(
                "recipients must be agents of this session",
                errors=[{"message": "unknown agent", "loc": ["body", "to"]}],
            )
    entry = BoardEntryDraft(
        kind=body.kind,
        title=body.title,
        body=body.body,
        tags=body.tags,
        to=body.to,
        refs=body.refs,
    )
    return await deps.commands.issue("command.post_board", session_id, entry=entry)
