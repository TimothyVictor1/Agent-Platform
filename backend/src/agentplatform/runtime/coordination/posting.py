"""Posting helpers for the supervisor (T17) and the user (T18).

Thin wrappers over ``BoardService.post`` that fix the author and shape the common
messages: binding decisions, answers, direct messages to one agent, and user
steering (the top-priority digest entries, SPEC §13.2). The board enforces the rules
(only supervisor/user may post decisions, answers must ref an open question, ...);
these helpers only build well-formed drafts. They are not rate limited.
"""

from __future__ import annotations

from collections.abc import Sequence

from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import Actor
from agentplatform.errors import BoardRuleError
from agentplatform.runtime.coordination.board import BoardService

__all__ = [
    "answer_as_supervisor",
    "message_agent",
    "post_as_supervisor",
    "post_as_user",
    "post_decision",
    "post_user_steering",
]

_TITLE_CHARS = 120


def _split(text: str) -> tuple[str, str]:
    """One-line title and, if the text does not fit it, the full text as the body."""
    flat = " ".join(text.split())
    if not flat:
        raise BoardRuleError("the message is empty")
    if len(flat) <= _TITLE_CHARS:
        return flat, ""
    return flat[: _TITLE_CHARS - 1].rstrip() + "…", text.strip()


async def post_as_supervisor(
    board: BoardService, session_id: str, draft: BoardEntryDraft
) -> BoardEntry:
    """Post any draft as the supervisor."""
    return await board.post(session_id, draft, author=Actor.supervisor())


async def post_as_user(
    board: BoardService,
    session_id: str,
    draft: BoardEntryDraft,
    *,
    user_id: str | None = None,
    user_name: str = "You",
) -> BoardEntry:
    """Post any draft as the user."""
    return await board.post(session_id, draft, author=Actor.user(user_id, user_name))


async def post_decision(
    board: BoardService,
    session_id: str,
    title: str,
    body: str = "",
    *,
    tags: Sequence[str] = (),
    author: Actor | None = None,
) -> BoardEntry:
    """A binding instruction for everyone (supervisor by default; pass ``Actor.user(...)``)."""
    draft = BoardEntryDraft(kind="decision", title=title, body=body, tags=list(tags))
    return await board.post(session_id, draft, author=author or Actor.supervisor())


async def answer_as_supervisor(
    board: BoardService,
    session_id: str,
    question_id: str,
    body: str,
    *,
    to: Sequence[str] | None = None,
) -> BoardEntry:
    """Answer (and thereby resolve) a question. Pass ``to=[asker id]`` to keep the
    answer private to the asker, e.g. for a direct question."""
    title, full = _split(body)
    draft = BoardEntryDraft(
        kind="answer",
        title=title,
        body=full,
        refs=[question_id],
        to=None if to is None else list(to),
    )
    return await board.post(session_id, draft, author=Actor.supervisor())


async def message_agent(
    board: BoardService,
    session_id: str,
    agent_id: str,
    body: str,
    *,
    author: Actor | None = None,
) -> BoardEntry:
    """A direct note to one agent (supervisor by default)."""
    title, full = _split(body)
    draft = BoardEntryDraft(kind="note", title=title, body=full, to=[agent_id])
    return await board.post(session_id, draft, author=author or Actor.supervisor())


async def post_user_steering(
    board: BoardService,
    session_id: str,
    text: str,
    *,
    to: Sequence[str] | None = None,
    user_id: str | None = None,
    user_name: str = "You",
) -> BoardEntry:
    """The user steers one agent (``to=[agent id]``) or everyone (``to=None``). Shown
    first in the addressed agents' next digest."""
    title, full = _split(text)
    draft = BoardEntryDraft(
        kind="note", title=title, body=full, to=None if to is None else list(to)
    )
    return await post_as_user(board, session_id, draft, user_id=user_id, user_name=user_name)
