"""Fakes shared by the T18 unit tests."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable

from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import Actor, utcnow
from agentplatform.contracts.events import BoardEntryPosted, EventDraft, SupervisorTriggered
from agentplatform.contracts.session import Session
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.supervisor import SupervisorView, detect_triggers
from agentplatform.testing import InMemoryEventStore


class FakeBoard:
    """Records posts; implements the parts of ``BoardService`` T18 uses. Like the
    real board it emits ``board.entry_posted`` (when given a recorder)."""

    def __init__(self, recorder: EventRecorder | None = None) -> None:
        self.entries: list[BoardEntry] = []
        self._seq = itertools.count(1)
        self._recorder = recorder

    async def post(
        self,
        session_id: str,
        draft: BoardEntryDraft,
        *,
        author: Actor,
        step: int | None = None,
    ) -> BoardEntry:
        seq = next(self._seq)
        entry = BoardEntry(
            **draft.model_dump(),
            id=f"e{seq}",
            session_id=session_id,
            seq=seq,
            author=author,
            created_at=utcnow(),
        )
        self.entries.append(entry)
        if self._recorder is not None:
            await self._recorder.record(
                session_id, EventDraft(payload=BoardEntryPosted(entry=entry), actor=author)
            )
        return entry

    def visible_to(self, agent_id: str) -> list[BoardEntry]:
        return [e for e in self.entries if e.to is None or agent_id in e.to]


async def until(check: Callable[[], bool], limit_s: float = 5.0) -> None:
    """Poll a fake until ``check()`` holds (``TimeoutError`` after ``limit_s``)."""
    async with asyncio.timeout(limit_s):
        for _ in itertools.count():  # polling a fake: nothing to wait on
            if check():
                return
            await asyncio.sleep(0.005)


async def until_async(check: Callable[[], Awaitable[bool]], limit_s: float = 5.0) -> None:
    async with asyncio.timeout(limit_s):
        for _ in itertools.count():
            if await check():
                return
            await asyncio.sleep(0.01)


async def supervisor_triggers(
    store: InMemoryEventStore, session: Session
) -> list[SupervisorTriggered]:
    """Every trigger T17's ``detect_triggers`` raises for the session's events, plus
    any ``supervisor.triggered`` events emitted directly."""
    view = SupervisorView.for_session(session)
    out: list[SupervisorTriggered] = []
    for event in await store.read(session.id, 0, 10_000):
        view.apply(event)
        out.extend(detect_triggers(event, view))
        if isinstance(event.payload, SupervisorTriggered):
            out.append(event.payload)
    return out


class FakeNotes:
    """``NoteSink`` that records notes."""

    def __init__(self) -> None:
        self.notes: list[str] = []

    def add_note(self, text: str) -> None:
        self.notes.append(text)
