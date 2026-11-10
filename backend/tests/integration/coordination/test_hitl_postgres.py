"""T18 over Postgres (marker ``infra``): user steering through the real board and
digest, and the approval store writing rows atomically with its events."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.common import Actor
from agentplatform.db.event_store import PostgresEventStore
from agentplatform.db.models import ApprovalRow
from agentplatform.db.session import SessionMaker, transaction
from agentplatform.errors import ConflictError
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.coordination.digest import DigestBuilder, postgres_task_lister
from agentplatform.runtime.coordination.postgres_board import PostgresBoardService
from agentplatform.runtime.hitl import HitlApprovalGate, HitlController, PostgresApprovalStore
from agentplatform.testing import make_task

from .conftest import World

pytestmark = pytest.mark.infra

USER = Actor.user("u1", "Tim")


class NoScheduler:
    async def stop(self, reason: object = "user", detail: str | None = None) -> None:
        return None


@pytest.fixture
def digests(board: PostgresBoardService, sessionmaker: SessionMaker) -> DigestBuilder:
    return DigestBuilder(board, postgres_task_lister(sessionmaker))


def _controller(
    world: World, board: PostgresBoardService, recorder: EventRecorder, sm: SessionMaker
) -> HitlController:
    gate = HitlApprovalGate(events=recorder, store=PostgresApprovalStore(sm), board=board)
    return HitlController(
        session=world.session,
        events=recorder,
        gate=gate,
        scheduler=NoScheduler(),
        board=board,
        agent_ids=lambda: [world.ada.id, world.brook.id, world.cleo.id],
    )


async def test_steer_reaches_only_the_target_agents_digest_first(
    world: World,
    board: PostgresBoardService,
    recorder: EventRecorder,
    sessionmaker: SessionMaker,
    digests: DigestBuilder,
) -> None:
    await board.post(
        world.id,
        BoardEntryDraft(kind="fact", title="Prices are in EUR"),
        author=world.actor(world.ada),
    )
    ctrl = _controller(world, board, recorder, sessionmaker)
    entry = await ctrl.steer(world.brook.id, "Check the annual plan too.", by=USER)
    task = make_task(session_id=world.id)
    brook = await digests.build(world.brook, task, since=0, budget_tokens=700)
    ada = await digests.build(world.ada, task, since=0, budget_tokens=700)
    cleo = await digests.build(world.cleo, task, since=0, budget_tokens=700)
    assert "Check the annual plan too." in brook.text
    assert brook.text.index("Check the annual plan too.") < brook.text.index("Prices are in EUR")
    assert "Check the annual plan too." not in ada.text
    assert "Check the annual plan too." not in cleo.text
    assert entry.to == [world.brook.id]


async def test_broadcast_steer_reaches_everyone(
    world: World,
    board: PostgresBoardService,
    recorder: EventRecorder,
    sessionmaker: SessionMaker,
    digests: DigestBuilder,
    store: PostgresEventStore,
) -> None:
    ctrl = _controller(world, board, recorder, sessionmaker)
    entry = await ctrl.steer(None, "Wrap up in 5 minutes.", by=USER)
    task = make_task(session_id=world.id)
    for agent in (world.ada, world.brook, world.cleo):
        digest = await digests.build(agent, task, since=0, budget_tokens=700)
        assert "Wrap up in 5 minutes." in digest.text
    events = await store.read(world.id, 0, 500)
    triggers = [e.payload for e in events if e.type == "supervisor.triggered"]
    assert [t.entry_ids for t in triggers] == [[entry.id]]


async def test_approval_rows_follow_the_events(
    world: World,
    board: PostgresBoardService,
    recorder: EventRecorder,
    sessionmaker: SessionMaker,
    store: PostgresEventStore,
) -> None:
    approvals = PostgresApprovalStore(sessionmaker)
    gate = HitlApprovalGate(events=recorder, store=approvals, board=board)
    other = HitlApprovalGate(events=recorder, store=approvals)  # a second runtime
    req = asyncio.create_task(
        gate.request(
            session_id=world.id,
            agent_id=world.ada.id,
            task_key="T1",
            action_summary="Submit the order form",
            risk="high",
            frame_id=None,
        )
    )
    async with asyncio.timeout(5):
        while not await approvals.pending(world.id):  # noqa: ASYNC110 - polling Postgres
            await asyncio.sleep(0.02)
    (pending,) = await approvals.pending(world.id)
    await gate.decide(pending.id, "approve", by=USER, note="ok")
    assert (await req).status == "approved"
    with pytest.raises(ConflictError):
        await other.decide(pending.id, "deny", by=USER)
    async with transaction(sessionmaker) as tx:
        row = (
            await tx.execute(select(ApprovalRow).where(ApprovalRow.id == pending.id))
        ).scalar_one()
    assert (row.status, row.note) == ("approved", "ok")
    decided = [e for e in await store.read(world.id, 0, 500) if e.type == "approval.decided"]
    assert len(decided) == 1

    flagged = await gate.flag(
        session_id=world.id, agent_id=world.cleo.id, action_summary="Log in", risk="medium"
    )
    assert [a.id for a in await approvals.pending(world.id)] == [flagged.id]
    expired = await gate.expire_pending(world.id)
    assert [a.status for a in expired] == ["expired"]
    assert await approvals.pending(world.id) == []
    await gate.aclose()
