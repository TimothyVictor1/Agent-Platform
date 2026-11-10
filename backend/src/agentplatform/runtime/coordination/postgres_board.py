"""``PostgresBoardService``: the BoardService over the repositories (SPEC §13.1).

Every post appends a ``board.entry_posted`` event whose seq becomes the entry's
``seq``; the entry row is inserted in the same transaction by the event store's
read-model hook. The store locks the session counter row for the whole append, so
the race-sensitive rules (an answer resolving an open question exactly once, claim
overlap) are checked inside that hook and are serialised per session.

Entries are untrusted data. Nothing here executes or renders them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Sequence

import structlog

from agentplatform.contracts.agent import Agent
from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import (
    Actor,
    BoardEntryKind,
    BoardEntryStatus,
    new_id,
    utcnow,
)
from agentplatform.contracts.events import (
    BoardEntryPosted,
    BoardEntryStatusChanged,
    Event,
    EventDraft,
    SupervisorTriggered,
)
from agentplatform.db.repositories import ClaimMatch, Repositories
from agentplatform.db.session import SessionMaker, as_session, transaction
from agentplatform.errors import BoardRuleError, ConflictError, NotFoundError
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.coordination.ratelimit import PostRateLimiter
from agentplatform.runtime.coordination.visibility import visible_to

__all__ = ["CLAIM_SIMILARITY_THRESHOLD", "PostgresBoardService"]

log = structlog.get_logger(__name__)

CLAIM_SIMILARITY_THRESHOLD = 0.5
"""``BoardRepository.similar_claims`` score (trigram similarity of titles, +0.1 when
tags overlap) at which two open claims count as overlapping (SPEC §13.1)."""

_STATUS_KINDS: frozenset[BoardEntryKind] = frozenset({"question", "claim", "blocker"})
_PAGE = 500
_MAX_TAG_CHARS = 40


def _clean_tags(tags: Sequence[str]) -> list[str]:
    seen: dict[str, None] = {}
    for tag in tags:
        cleaned = tag.strip().lower()[:_MAX_TAG_CHARS]
        if cleaned:
            seen[cleaned] = None
    return list(seen)


def _check_static_rules(draft: BoardEntryDraft, author: Actor) -> None:
    """Rules that need no database."""
    if author.type == "agent" and not author.id:
        raise BoardRuleError("agent authors need an id")
    if draft.kind == "decision":
        if author.type not in ("supervisor", "user"):
            raise BoardRuleError("only the supervisor or the user can post a decision")
        if draft.to is not None:
            raise BoardRuleError("a decision is addressed to everyone; leave `to` empty")
    if draft.to is not None:
        if not draft.to:
            raise BoardRuleError("`to` must list at least one agent id, or be omitted")
        if author.type == "agent" and author.id in draft.to:
            raise BoardRuleError("you cannot send a message to yourself")
    if draft.kind == "answer" and len(draft.refs) != 1:
        raise BoardRuleError("an answer must reference exactly one question (refs=[question id])")
    if draft.kind == "artifact" and not draft.artifact_id:
        raise BoardRuleError("an artifact entry needs artifact_id")
    if draft.kind != "artifact" and draft.artifact_id is not None:
        raise BoardRuleError("artifact_id is only allowed on artifact entries")


class PostgresBoardService:
    """Implements ``BoardService`` with Postgres (see module docstring)."""

    def __init__(
        self,
        sessionmaker: SessionMaker,
        recorder: EventRecorder,
        *,
        poll_interval_s: float = 1.0,
        limiter: PostRateLimiter | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._recorder = recorder
        self._poll_interval_s = poll_interval_s
        self._limiter = limiter or PostRateLimiter()
        self._waiters: defaultdict[str, set[asyncio.Event]] = defaultdict(set)

    # -- posting ---------------------------------------------------------------------

    async def post(
        self,
        session_id: str,
        draft: BoardEntryDraft,
        *,
        author: Actor,
        step: int | None = None,
    ) -> BoardEntry:
        draft = draft.model_copy(update={"tags": _clean_tags(draft.tags)})
        _check_static_rules(draft, author)
        if author.type == "agent" and step is None:
            raise BoardRuleError("agent posts require the current step number")
        limited_agent = author.id if author.type == "agent" else None
        if limited_agent is not None and step is not None:
            self._limiter.check(limited_agent, step, draft.kind)

        entry_id = new_id()
        resolved: list[BoardEntry] = []
        conflicts: list[ClaimMatch] = []

        def make_draft(seq: int) -> EventDraft:
            entry = BoardEntry.model_validate(
                {
                    **draft.model_dump(),
                    "id": entry_id,
                    "session_id": session_id,
                    "seq": seq,
                    "author": author,
                    "status": "open" if draft.kind in _STATUS_KINDS else None,
                    "created_at": utcnow(),
                }
            )
            return EventDraft(payload=BoardEntryPosted(entry=entry), actor=author)

        async def apply(event: Event, tx: object) -> None:
            payload = event.payload
            assert isinstance(payload, BoardEntryPosted)
            entry = payload.entry
            repos = Repositories(as_session(tx))
            question = await self._validate(repos, entry, author)
            if entry.kind == "claim":
                conflicts.extend(await self._overlapping_claims(repos, entry))
            await repos.board.insert(entry)
            if question is not None:
                await repos.board.set_status(question.id, "resolved")
                resolved.append(question)

        event = await self._recorder.record(session_id, make_draft, apply)
        assert isinstance(event.payload, BoardEntryPosted)
        entry = event.payload.entry

        if limited_agent is not None and step is not None:
            self._limiter.record(limited_agent, step, entry.kind)
        for question in resolved:
            await self._emit_resolved(session_id, question, entry, author, event)
            self._wake(question.id)
        if conflicts:
            await self._emit_claim_conflict(session_id, entry, conflicts, event)
        return entry

    async def _validate(
        self, repos: Repositories, entry: BoardEntry, author: Actor
    ) -> BoardEntry | None:
        """Database rules; returns the question an ``answer`` resolves."""
        roster = await repos.agents.list(entry.session_id)
        agent_ids = {a.id for a in roster}
        if author.type == "agent" and author.id not in agent_ids:
            raise BoardRuleError("you are not an agent of this session")
        if entry.to is not None:
            unknown = [a for a in entry.to if a not in agent_ids]
            if unknown:
                raise BoardRuleError(f"unknown agent id(s) in `to`: {', '.join(unknown)}")
        question: BoardEntry | None = None
        for ref in entry.refs:
            target = await repos.board.find(ref)
            if (
                target is None
                or target.session_id != entry.session_id
                or (author.type == "agent" and author.id and not visible_to(target, author.id))
            ):
                # Same message for missing, foreign and hidden entries.
                raise BoardRuleError(f"ref {ref!r} is not an entry on this session's board")
            if entry.kind == "answer":
                question = target
        if entry.kind == "answer":
            assert question is not None
            if question.kind != "question":
                raise BoardRuleError(f"entry {question.id} is a {question.kind}, not a question")
            if question.status != "open":
                raise BoardRuleError(f"question {question.id} is already {question.status}")
        return question

    async def _overlapping_claims(self, repos: Repositories, entry: BoardEntry) -> list[ClaimMatch]:
        matches = await repos.board.similar_claims(
            entry.session_id,
            entry.title,
            entry.tags,
            CLAIM_SIMILARITY_THRESHOLD,
            exclude_author_id=entry.author.id,
        )
        mine = set(entry.tags)
        # SPEC §13.1: "same tags": when both claims carry tags they must share one.
        return [m for m in matches if not (mine and m.entry.tags and mine.isdisjoint(m.entry.tags))]

    async def _emit_resolved(
        self, session_id: str, question: BoardEntry, answer: BoardEntry, actor: Actor, cause: Event
    ) -> None:
        await self._emit_safely(
            session_id,
            EventDraft(
                payload=BoardEntryStatusChanged(
                    entry_id=question.id,
                    status="resolved",
                    previous="open",
                    reason=f"answered by #{answer.seq}",
                ),
                actor=actor,
                causation_id=cause.id,
            ),
        )

    async def _emit_claim_conflict(
        self, session_id: str, entry: BoardEntry, matches: list[ClaimMatch], cause: Event
    ) -> None:
        others = ", ".join(f"#{m.entry.seq} {m.entry.author.name}" for m in matches)
        await self._emit_safely(
            session_id,
            EventDraft(
                payload=SupervisorTriggered(
                    reason="claim_conflict",
                    detail=f"claim #{entry.seq} by {entry.author.name} overlaps {others}",
                    task_key=entry.task_key,
                    agent_id=entry.author.id,
                    entry_ids=[entry.id, *(m.entry.id for m in matches)],
                ),
                actor=Actor.system(),
                causation_id=cause.id,
            ),
        )

    async def _emit_safely(self, session_id: str, draft: EventDraft) -> None:
        """The post is already committed; a failing follow-up event is logged, not
        raised, so the author is not told to repost something that exists."""
        try:
            await self._recorder.record(session_id, draft)
        except Exception:
            log.exception("board_followup_event_failed", session_id=session_id, type=draft.type)

    # -- lifecycle -------------------------------------------------------------------

    async def set_status(
        self,
        entry_id: str,
        status: BoardEntryStatus,
        *,
        actor: Actor,
        reason: str | None = None,
    ) -> BoardEntry:
        entry = await self.get(entry_id)
        if actor.type == "agent" and (actor.id is None or not visible_to(entry, actor.id)):
            raise NotFoundError(f"board entry {entry_id} does not exist")
        is_author = actor.type == "agent" and actor.id == entry.author.id
        if not (is_author or actor.type in ("supervisor", "user")):
            raise BoardRuleError("only the author, the supervisor or the user can change this")
        if entry.status is None:
            raise BoardRuleError(f"{entry.kind} entries have no status")
        if status == "open":
            raise BoardRuleError("entries cannot be reopened; post a new one")
        if entry.status == status:
            raise BoardRuleError(f"entry {entry_id} is already {status}")
        previous = entry.status

        async def apply(event: Event, tx: object) -> None:
            repos = Repositories(as_session(tx))
            if (await repos.board.get(entry_id)).status != previous:
                raise ConflictError(f"entry {entry_id} changed concurrently")
            await repos.board.set_status(entry_id, status)

        draft = EventDraft(
            payload=BoardEntryStatusChanged(
                entry_id=entry_id, status=status, previous=previous, reason=reason
            ),
            actor=actor,
        )
        await self._recorder.record(entry.session_id, draft, apply)
        return await self.get(entry_id)

    # -- reading ---------------------------------------------------------------------

    async def get(self, entry_id: str) -> BoardEntry:
        async with transaction(self._sessionmaker) as tx:
            return await Repositories(tx).board.get(entry_id)

    async def get_by_seq(self, session_id: str, seq: int) -> BoardEntry:
        async with transaction(self._sessionmaker) as tx:
            found = await Repositories(tx).board.list(session_id, after_seq=seq - 1, limit=1)
        if not found or found[0].seq != seq:
            raise NotFoundError(f"no board entry #{seq}")
        return found[0]

    async def find_agent(self, session_id: str, name: str) -> Agent | None:
        wanted = name.strip().casefold()
        async with transaction(self._sessionmaker) as tx:
            roster = await Repositories(tx).agents.list(session_id)
        return next((a for a in roster if a.name.casefold() == wanted), None)

    async def search(
        self,
        session_id: str,
        *,
        query: str | None = None,
        kinds: list[BoardEntryKind] | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        viewer_agent_id: str | None = None,
    ) -> list[BoardEntry]:
        limit = max(1, min(limit, 100))
        wanted_tags = _clean_tags(tags or [])
        found: list[BoardEntry] = []
        async with transaction(self._sessionmaker) as tx:
            repos = Repositories(tx)
            if query and query.strip():
                # Best match first. Visibility and tags are filtered here (the
                # repository has no viewer filter), so fetch generously.
                candidates = await repos.board.search(
                    session_id, query, kinds=kinds, limit=min(200, limit * 5)
                )
                for entry in candidates:
                    if self._matches(entry, wanted_tags, viewer_agent_id):
                        found.append(entry)
                        if len(found) == limit:
                            break
                return found
            newest: list[BoardEntry] = []
            after = 0
            while True:
                page = await repos.board.list(
                    session_id, after_seq=after, kinds=kinds, tags=wanted_tags or None, limit=_PAGE
                )
                for entry in page:
                    if self._matches(entry, [], viewer_agent_id):
                        newest.append(entry)
                if len(newest) > limit:
                    del newest[: len(newest) - limit]
                if len(page) < _PAGE:
                    break
                after = page[-1].seq
        return newest[::-1]

    @staticmethod
    def _matches(entry: BoardEntry, tags: list[str], viewer: str | None) -> bool:
        if viewer is not None and not visible_to(entry, viewer):
            return False
        return not tags or not set(tags).isdisjoint(entry.tags)

    async def entries_after(
        self,
        session_id: str,
        after_seq: int,
        *,
        viewer_agent_id: str | None = None,
        limit: int = 200,
    ) -> list[BoardEntry]:
        limit = max(1, min(limit, 1000))
        found: list[BoardEntry] = []
        after = after_seq
        async with transaction(self._sessionmaker) as tx:
            repos = Repositories(tx)
            while len(found) < limit:
                page = await repos.board.list(session_id, after_seq=after, limit=_PAGE)
                for entry in page:
                    if viewer_agent_id is None or visible_to(entry, viewer_agent_id):
                        found.append(entry)
                        if len(found) == limit:
                            break
                if len(page) < _PAGE:
                    break
                after = page[-1].seq
        return found

    # -- waiting ---------------------------------------------------------------------

    async def wait_for_answer(self, question_id: str, timeout_s: float) -> BoardEntry | None:
        question = await self.get(question_id)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, timeout_s)
        wake = asyncio.Event()
        self._waiters[question_id].add(wake)
        try:
            while True:
                wake.clear()  # before the check, so a post in between is not missed
                answer = await self._find_answer(question)
                if answer is not None:
                    return answer
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                # On timeout poll again: the answer may come from another process.
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(wake.wait(), min(self._poll_interval_s, remaining))
        finally:
            waiters = self._waiters[question_id]
            waiters.discard(wake)
            if not waiters:
                del self._waiters[question_id]

    async def _find_answer(self, question: BoardEntry) -> BoardEntry | None:
        after = question.seq
        async with transaction(self._sessionmaker) as tx:
            repos = Repositories(tx)
            while True:
                page = await repos.board.list(
                    question.session_id, after_seq=after, kinds=["answer"], limit=_PAGE
                )
                for entry in page:
                    if question.id in entry.refs:
                        return entry
                if len(page) < _PAGE:
                    return None
                after = page[-1].seq

    def _wake(self, question_id: str) -> None:
        for event in self._waiters.get(question_id, ()):
            event.set()
