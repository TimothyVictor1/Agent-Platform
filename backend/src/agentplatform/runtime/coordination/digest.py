"""``DigestBuilder``: the inbox digest of SPEC §13.2, plugged into ``AgentRuntime``.

For agent A with cursor ``c`` (the last board ``seq`` A has seen):

1. fetch entries with ``seq > c`` visible to A, drop A's own;
2. score them (user steering 100, decision 90, answer to A's question 85, direct to
   A 80, blocker 60, fact/claim with tag overlap or from an upstream/downstream
   task 50, relevant artifact 45, other 10);
3. take whole entries, highest score first (older first on ties), until the token
   budget (4 chars ≈ 1 token) is used; the first entry that does not fit ends the
   selection, so a lower-priority entry never displaces a higher one;
4. add ``+{k} other new entries (use read_board to search).`` for the rest;
5. return ``cursor = max seq considered``.

Entries are never cut in the middle: an entry is either shown as one compact line or
counted in the tail. Body text is clipped to ``MAX_BODY_CHARS`` *inside* the line, so
the line boundaries the loop relies on (``Digest.text`` is shown whole or not at all,
and the cursor only advances when it was) stay intact. Entries not shown are counted
in the tail and the cursor still passes them, as the spec says; ``read_board`` finds
them again.

Board text is untrusted data: newlines and control characters are collapsed so an
entry cannot forge extra digest lines.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from agentplatform.contracts.agent import Agent
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.task import Task
from agentplatform.db.session import SessionMaker, transaction
from agentplatform.errors import NotFoundError
from agentplatform.runtime.agent.loop import Digest
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.runtime.coordination.visibility import is_own

__all__ = [
    "MAX_BODY_CHARS",
    "SCORE_ANSWER_TO_ME",
    "DigestBuilder",
    "TaskLister",
    "estimate_tokens",
    "format_entry",
    "postgres_task_lister",
    "sanitize",
]

SCORE_USER_STEERING = 100
SCORE_DECISION = 90
SCORE_ANSWER_TO_ME = 85
SCORE_DIRECT = 80
SCORE_BLOCKER = 60
SCORE_RELATED_FACT = 50
SCORE_RELATED_ARTIFACT = 45
SCORE_OTHER = 10

MAX_BODY_CHARS = 160
TAIL_RESERVE_TOKENS = 16
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]+")

TaskLister = Callable[[str], Awaitable[Sequence[Task]]]
"""``await list_tasks(session_id)``: the session's tasks (current plan version)."""


def estimate_tokens(text: str) -> int:
    """Approximate token count: 4 characters ≈ 1 token (SPEC §10.4)."""
    return -(-len(text) // 4)


def sanitize(text: str, limit: int) -> str:
    """One line, no control characters, at most ``limit`` characters."""
    flat = " ".join(_CONTROL.sub(" ", text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def format_entry(entry: BoardEntry, *, body_chars: int = MAX_BODY_CHARS) -> str:
    """``[#123 fact · Brook · T2] Title — body``. Open questions also show their id
    (``answer`` and ``wait_for`` take it); ``#123`` works for ``answer`` and
    ``release_claim`` too."""
    head = f"#{entry.seq} {entry.kind} · {sanitize(entry.author.name, 40)}"
    if entry.task_key:
        head += f" · {sanitize(entry.task_key, 20)}"
    if entry.kind == "question" and entry.status == "open":
        head += f" · id {entry.id}"
    elif entry.kind == "claim" and entry.status == "open":
        head += " · open"
    line = f"[{head}] {sanitize(entry.title, 120)}"
    body = sanitize(entry.body, body_chars)
    return f"{line} — {body}" if body else line


@dataclass(frozen=True, slots=True)
class _Relevance:
    tags: frozenset[str]
    related_tasks: frozenset[str]
    agent_id: str


def postgres_task_lister(sessionmaker: SessionMaker) -> TaskLister:
    """A ``TaskLister`` reading the task repository."""
    from agentplatform.db.repositories import Repositories

    async def list_tasks(session_id: str) -> Sequence[Task]:
        async with transaction(sessionmaker) as tx:
            return await Repositories(tx).tasks.list(session_id)

    return list_tasks


class DigestBuilder:
    """Implements ``agentplatform.runtime.agent.loop.DigestProvider``."""

    def __init__(self, board: BoardService, list_tasks: TaskLister, *, fetch_limit: int = 200):
        self._board = board
        self._list_tasks = list_tasks
        self._fetch_limit = fetch_limit

    async def build(self, agent: Agent, task: Task, *, since: int, budget_tokens: int) -> Digest:
        entries = await self._board.entries_after(
            agent.session_id, since, viewer_agent_id=agent.id, limit=self._fetch_limit
        )
        if not entries:
            return Digest("", since)
        cursor = max(e.seq for e in entries)
        candidates = [e for e in entries if not is_own(e, agent.id)]
        if not candidates:
            return Digest("", cursor)

        relevance = await self._relevance(agent, task)
        questions: dict[str, bool] = {}  # question id -> asked by this agent
        ranked: list[tuple[int, BoardEntry]] = []
        for entry in candidates:
            ranked.append((await self._score(entry, relevance, questions), entry))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].seq))

        lines = [format_entry(e) for _, e in ranked]
        costs = [estimate_tokens(line) + 1 for line in lines]
        room = budget_tokens if sum(costs) <= budget_tokens else budget_tokens - TAIL_RESERVE_TOKENS
        shown = 0
        used = 0
        for cost in costs:
            if used + cost > room and shown > 0:
                break
            used += cost
            shown += 1
        out = lines[:shown]
        if shown == 1 and costs[0] > max(room, 1):  # a single oversized line: clip it
            out = [lines[0][: max(room, 1) * 4]]
        hidden = len(candidates) - shown
        if hidden:
            out.append(f"+{hidden} other new entries (use read_board to search).")
        return Digest("\n".join(out), cursor)

    async def _relevance(self, agent: Agent, task: Task) -> _Relevance:
        related = {task.key, *task.depends_on}
        for other in await self._list_tasks(agent.session_id):
            if task.key in other.depends_on:
                related.add(other.key)
        return _Relevance(frozenset(task.tags), frozenset(related), agent.id)

    async def _score(self, entry: BoardEntry, rel: _Relevance, questions: dict[str, bool]) -> int:
        if entry.kind == "decision":
            return SCORE_DECISION
        if entry.author.type == "user":
            return SCORE_USER_STEERING
        if entry.kind == "answer" and await self._answers_my_question(entry, rel, questions):
            return SCORE_ANSWER_TO_ME
        if entry.to is not None and rel.agent_id in entry.to:
            return SCORE_DIRECT
        if entry.kind == "blocker":
            return SCORE_BLOCKER
        related = bool(rel.tags & set(entry.tags)) or entry.task_key in rel.related_tasks
        if entry.kind in ("fact", "claim") and related:
            return SCORE_RELATED_FACT
        if entry.kind == "artifact" and related:
            return SCORE_RELATED_ARTIFACT
        return SCORE_OTHER

    async def _answers_my_question(
        self, answer: BoardEntry, rel: _Relevance, cache: dict[str, bool]
    ) -> bool:
        for ref in answer.refs:
            if ref not in cache:
                try:
                    question = await self._board.get(ref)
                except NotFoundError:
                    cache[ref] = False
                else:
                    cache[ref] = question.kind == "question" and is_own(question, rel.agent_id)
            if cache[ref]:
                return True
        return False
