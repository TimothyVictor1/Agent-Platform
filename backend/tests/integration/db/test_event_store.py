"""PostgresEventStore: seq assignment, ordering, atomic read-model updates (SPEC §7.2, §17)."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

from agentplatform.contracts.common import Actor
from agentplatform.contracts.events import (
    BoardEntryPosted,
    Event,
    EventDraft,
    SessionCreated,
    SessionPaused,
    SessionResumed,
)
from agentplatform.contracts.session import Session
from agentplatform.db.event_store import EventStore, PostgresEventStore
from agentplatform.db.models import BoardEntryRow, EventRow, SessionCounterRow
from agentplatform.db.repositories import BoardRepository, SessionRepository
from agentplatform.db.session import (
    SessionMaker,
    as_session,
    create_engine,
    create_sessionmaker,
    transaction,
)
from agentplatform.errors import ConflictError, InvalidRequestError, NotFoundError
from agentplatform.testing.builders import make_board_entry, make_session

pytestmark = pytest.mark.infra


def created(session: Session) -> EventDraft:
    return EventDraft(payload=SessionCreated(session=session), actor=Actor.user("local"))


def paused(reason: str = "test") -> EventDraft:
    return EventDraft(payload=SessionPaused(reason=reason), actor=Actor.system())


def test_implements_protocol(store: PostgresEventStore) -> None:
    assert isinstance(store, EventStore)


async def test_session_created_creates_session_row_and_counter(
    store: PostgresEventStore, sessionmaker: SessionMaker
) -> None:
    session = make_session()
    event = await store.append(session.id, created(session))

    assert event.seq == 1
    assert event.type == "session.created"
    assert await store.last_seq(session.id) == 1
    async with transaction(sessionmaker) as tx:
        assert await SessionRepository(tx).get(session.id) == session


async def test_append_to_unknown_session_raises_not_found(store: PostgresEventStore) -> None:
    with pytest.raises(NotFoundError):
        await store.append("no-such-session", paused())
    assert await store.last_seq("no-such-session") == 0


async def test_second_session_created_conflicts(
    store: PostgresEventStore, created_session: Session
) -> None:
    with pytest.raises(ConflictError):
        await store.append(created_session.id, created(created_session))
    assert await store.last_seq(created_session.id) == 1


async def test_session_created_for_other_id_is_rejected(store: PostgresEventStore) -> None:
    session = make_session()
    with pytest.raises(InvalidRequestError):
        await store.append("another-id", created(session))


async def test_concurrent_creation_has_one_winner(store: PostgresEventStore) -> None:
    session = make_session()
    results = await asyncio.gather(
        *(store.append(session.id, created(session)) for _ in range(5)),
        return_exceptions=True,
    )
    winners = [r for r in results if not isinstance(r, BaseException)]
    assert len(winners) == 1
    assert all(isinstance(r, ConflictError) for r in results if isinstance(r, BaseException))
    assert await store.last_seq(session.id) == 1


async def test_50_concurrent_appends_get_gapless_increasing_seqs(
    store: PostgresEventStore, sessionmaker: SessionMaker, created_session: Session
) -> None:
    sid = created_session.id
    events = await asyncio.gather(*(store.append(sid, paused(f"p{i}")) for i in range(50)))

    assert sorted(e.seq for e in events) == list(range(2, 52))
    assert await store.last_seq(sid) == 51
    stored = await store.read(sid, limit=5000)
    assert [e.seq for e in stored] == list(range(1, 52))
    assert {e.id for e in stored[1:]} == {e.id for e in events}
    async with transaction(sessionmaker) as tx:
        count = await tx.scalar(select(func.count()).where(EventRow.session_id == sid))
        next_seq = await tx.scalar(
            select(SessionCounterRow.next_seq).where(SessionCounterRow.session_id == sid)
        )
    assert count == 51
    assert next_seq == 52


async def test_concurrent_appends_from_separate_stores(
    sessionmaker: SessionMaker, database_url: str, created_session: Session
) -> None:
    """Two stores with their own engines behave like two processes."""
    other_engine = create_engine(url=database_url)
    try:
        stores = [
            PostgresEventStore(sessionmaker),
            PostgresEventStore(create_sessionmaker(other_engine)),
        ]
        sid = created_session.id
        events = await asyncio.gather(
            *(stores[i % 2].append(sid, paused(f"p{i}")) for i in range(20))
        )
        assert sorted(e.seq for e in events) == list(range(2, 22))
    finally:
        await other_engine.dispose()


async def test_read_round_trips_and_paginates(
    store: PostgresEventStore, created_session: Session
) -> None:
    sid = created_session.id
    appended = [await store.append(sid, paused(f"p{i}")) for i in range(9)]

    first = await store.read(sid, after_seq=0, limit=4)
    second = await store.read(sid, after_seq=first[-1].seq, limit=4)
    third = await store.read(sid, after_seq=second[-1].seq, limit=4)

    assert [e.seq for e in first] == [1, 2, 3, 4]
    assert [e.seq for e in second] == [5, 6, 7, 8]
    assert [e.seq for e in third] == [9, 10]
    assert await store.read(sid, after_seq=10) == []
    assert [*first, *second, *third][1:] == appended
    assert first[0].type == "session.created"
    assert first[0].payload == SessionCreated(session=created_session)


async def test_read_clamps_limit(store: PostgresEventStore, created_session: Session) -> None:
    await store.append(created_session.id, paused())
    assert len(await store.read(created_session.id, limit=0)) == 1
    assert len(await store.read(created_session.id, limit=10**6)) == 2


async def test_draft_factory_receives_assigned_seq(
    store: PostgresEventStore, sessionmaker: SessionMaker, created_session: Session
) -> None:
    sid = created_session.id

    def draft(seq: int) -> EventDraft:
        entry = make_board_entry("fact", seq=seq, session_id=sid, title="Price is 10 USD")
        return EventDraft(payload=BoardEntryPosted(entry=entry), actor=entry.author)

    async def insert_entry(event: Event, tx: object) -> None:
        assert isinstance(event.payload, BoardEntryPosted)
        await BoardRepository(as_session(tx)).insert(event.payload.entry)

    event = await store.append(sid, draft, insert_entry)

    assert event.seq == 2
    assert isinstance(event.payload, BoardEntryPosted)
    assert event.payload.entry.seq == 2
    async with transaction(sessionmaker) as tx:
        stored = await BoardRepository(tx).get(event.payload.entry.id)
    assert stored == event.payload.entry


async def test_failed_read_model_update_rolls_back_everything(
    store: PostgresEventStore, sessionmaker: SessionMaker, created_session: Session
) -> None:
    sid = created_session.id
    entry = make_board_entry("fact", seq=2, session_id=sid)

    async def half_then_fail(event: Event, tx: object) -> None:
        await BoardRepository(as_session(tx)).insert(entry)
        raise RuntimeError("read model broke")

    with pytest.raises(RuntimeError, match="read model broke"):
        await store.append(
            sid,
            EventDraft(payload=BoardEntryPosted(entry=entry), actor=entry.author),
            half_then_fail,
        )

    assert await store.last_seq(sid) == 1
    assert [e.seq for e in await store.read(sid)] == [1]
    async with transaction(sessionmaker) as tx:
        assert await tx.scalar(select(func.count()).where(BoardEntryRow.session_id == sid)) == 0
    # The next append reuses seq 2: no gap was left behind.
    nxt = await store.append(sid, EventDraft(payload=SessionResumed(), actor=Actor.system()))
    assert nxt.seq == 2


async def test_read_model_update_sees_the_event_in_the_same_transaction(
    store: PostgresEventStore, created_session: Session
) -> None:
    seen: list[int] = []

    async def check(event: Event, tx: object) -> None:
        count = await as_session(tx).scalar(
            select(func.count()).where(EventRow.session_id == created_session.id)
        )
        assert isinstance(count, int)
        seen.append(count)

    await store.append(created_session.id, paused(), check)
    assert seen == [2]


async def test_sessions_are_independent(store: PostgresEventStore) -> None:
    a, b = make_session(), make_session()
    await store.append(a.id, created(a))
    await store.append(b.id, created(b))
    await store.append(a.id, paused())
    assert await store.last_seq(a.id) == 2
    assert await store.last_seq(b.id) == 1
    assert [e.session_id for e in await store.read(b.id)] == [b.id]
