"""Shared fakes for the T18 unit tests: a recording board and an approval world."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from agentplatform.contracts.events import Event
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.hitl import HitlApprovalGate, InMemoryApprovalStore
from agentplatform.testing import InMemoryBus, InMemoryEventStore

from .fakes import FakeBoard


@dataclass
class ApprovalWorld:
    store: InMemoryEventStore
    bus: InMemoryBus
    recorder: EventRecorder
    approvals: InMemoryApprovalStore
    board: FakeBoard
    make_gate: Callable[..., HitlApprovalGate]
    gates: list[HitlApprovalGate] = field(default_factory=list)

    async def events(self, session_id: str, type_: str | None = None) -> list[Event]:
        events = await self.store.read(session_id, 0, 10_000)
        return [e for e in events if type_ is None or e.type == type_]

    async def payloads(self, session_id: str, type_: str) -> list[Any]:
        return [e.payload for e in await self.events(session_id, type_)]


@pytest.fixture
async def aw() -> Any:
    store, bus = InMemoryEventStore(), InMemoryBus()
    recorder = EventRecorder(store, bus)
    approvals = InMemoryApprovalStore()
    board = FakeBoard(recorder)
    gates: list[HitlApprovalGate] = []

    def make_gate(**kw: Any) -> HitlApprovalGate:
        kw.setdefault("board", board)
        gate = HitlApprovalGate(events=recorder, store=approvals, **kw)
        gates.append(gate)
        return gate

    world = ApprovalWorld(store, bus, recorder, approvals, board, make_gate, gates)
    yield world
    for gate in gates:
        await gate.aclose()
