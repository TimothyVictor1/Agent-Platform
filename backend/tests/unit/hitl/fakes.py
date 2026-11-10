"""Fakes shared by the T18 unit tests."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable

from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import Actor, utcnow


class FakeBoard:
    """Records posts; implements the parts of ``BoardService`` T18 uses."""

    def __init__(self) -> None:
        self.entries: list[BoardEntry] = []
        self._seq = itertools.count(1)

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
