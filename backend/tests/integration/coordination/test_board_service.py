"""BoardService over Postgres: kind rules, lifecycle, search, rate limits, waiting."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.common import Actor
from agentplatform.contracts.events import BoardEntryStatusChanged, SupervisorTriggered
from agentplatform.errors import BoardRuleError, NotFoundError, RateLimitedError
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.runtime.coordination.postgres_board import PostgresBoardService
from agentplatform.testing.memory_bus import InMemoryBus

from .conftest import World

pytestmark = pytest.mark.infra


def draft(kind: Any = "fact", title: str = "A fact", **kw: Any) -> BoardEntryDraft:
    return BoardEntryDraft(kind=kind, title=title, **kw)


def types(bus: InMemoryBus, session_id: str) -> list[str]:
    return [e.type for e in bus.published if e.session_id == session_id]


def test_implements_the_protocol(board: PostgresBoardService) -> None:
    assert isinstance(board, BoardService)


async def test_post_assigns_event_seq_and_initial_status(
    board: PostgresBoardService, world: World
) -> None:
    fact = await board.post(world.id, draft(), author=world.actor(world.ada), step=1)
    question = await board.post(
        world.id, draft("question", "Which?"), author=world.actor(world.ada), step=2
    )
    assert fact.status is None
    assert question.status == "open"
    assert question.seq == fact.seq + 1
    assert (await board.get(question.id)) == question


async def test_entry_seq_equals_event_seq(
    board: PostgresBoardService, world: World, bus: InMemoryBus
) -> None:
    entry = await board.post(world.id, draft(), author=world.actor(world.ada), step=1)
    posted = [e for e in bus.published if e.type == "board.entry_posted"]
    assert [e.seq for e in posted] == [entry.seq]


async def test_tags_are_normalised(board: PostgresBoardService, world: World) -> None:
    entry = await board.post(
        world.id,
        draft(tags=[" Pricing ", "pricing", "", "X"]),
        author=world.actor(world.ada),
        step=1,
    )
    assert entry.tags == ["pricing", "x"]


async def test_decision_only_from_supervisor_or_user(
    board: PostgresBoardService, world: World
) -> None:
    with pytest.raises(BoardRuleError, match="decision"):
        await board.post(world.id, draft("decision"), author=world.actor(world.ada), step=1)
    for author in (Actor.supervisor(), Actor.user("u1")):
        entry = await board.post(world.id, draft("decision", "Use EUR"), author=author, step=1)
        assert entry.status is None
        assert entry.to is None
    with pytest.raises(BoardRuleError, match="everyone"):
        await board.post(
            world.id, draft("decision", to=[world.ada.id]), author=Actor.supervisor(), step=1
        )


async def test_answer_resolves_the_question(
    board: PostgresBoardService, world: World, bus: InMemoryBus
) -> None:
    q = await board.post(
        world.id, draft("question", "Which?"), author=world.actor(world.ada), step=1
    )
    a = await board.post(
        world.id, draft("answer", "EUR", refs=[q.id]), author=world.actor(world.brook), step=1
    )
    assert (await board.get(q.id)).status == "resolved"
    changes = [
        e.payload
        for e in bus.published
        if isinstance(e.payload, BoardEntryStatusChanged) and e.payload.entry_id == q.id
    ]
    assert [(c.status, c.previous) for c in changes] == [("resolved", "open")]
    assert a.status is None


@pytest.mark.parametrize(
    ("refs", "message"),
    [([], "exactly one"), (["a", "b"], "exactly one")],
)
async def test_answer_needs_exactly_one_ref(
    board: PostgresBoardService, world: World, refs: list[str], message: str
) -> None:
    with pytest.raises(BoardRuleError, match=message):
        await board.post(
            world.id, draft("answer", refs=refs), author=world.actor(world.ada), step=1
        )


async def test_answer_must_ref_an_open_question(board: PostgresBoardService, world: World) -> None:
    fact = await board.post(world.id, draft(), author=world.actor(world.ada), step=1)
    with pytest.raises(BoardRuleError, match="not a question"):
        await board.post(
            world.id, draft("answer", refs=[fact.id]), author=world.actor(world.brook), step=1
        )
    q = await board.post(world.id, draft("question"), author=world.actor(world.ada), step=1)
    await board.post(
        world.id, draft("answer", refs=[q.id]), author=world.actor(world.brook), step=1
    )
    with pytest.raises(BoardRuleError, match="already resolved"):
        await board.post(
            world.id, draft("answer", refs=[q.id]), author=world.actor(world.cleo), step=1
        )
    with pytest.raises(BoardRuleError, match="not an entry"):
        await board.post(
            world.id, draft("answer", refs=["nope"]), author=world.actor(world.cleo), step=1
        )


async def test_concurrent_answers_resolve_once(board: PostgresBoardService, world: World) -> None:
    q = await board.post(world.id, draft("question"), author=world.actor(world.ada), step=1)
    results = await asyncio.gather(
        *(
            board.post(world.id, draft("answer", refs=[q.id]), author=world.actor(a), step=1)
            for a in (world.brook, world.cleo)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, BoardRuleError) for r in results) == 1


async def test_answer_cannot_reference_a_hidden_direct_question(
    board: PostgresBoardService, world: World
) -> None:
    q = await board.post(
        world.id, draft("question", to=[world.brook.id]), author=world.actor(world.ada), step=1
    )
    with pytest.raises(BoardRuleError, match="not an entry"):
        await board.post(
            world.id, draft("answer", refs=[q.id]), author=world.actor(world.cleo), step=1
        )
    # The addressee and the supervisor can answer.
    await board.post(
        world.id, draft("answer", refs=[q.id]), author=world.actor(world.brook), step=1
    )


async def test_direct_message_rules(board: PostgresBoardService, world: World) -> None:
    dm = await board.post(
        world.id, draft("note", to=[world.brook.id]), author=world.actor(world.ada), step=1
    )
    assert dm.to == [world.brook.id]
    with pytest.raises(BoardRuleError, match="unknown agent"):
        await board.post(
            world.id, draft("note", to=["ghost"]), author=world.actor(world.ada), step=1
        )
    with pytest.raises(BoardRuleError, match="yourself"):
        await board.post(
            world.id, draft("note", to=[world.ada.id]), author=world.actor(world.ada), step=1
        )
    with pytest.raises(BoardRuleError, match="at least one"):
        await board.post(world.id, draft("note", to=[]), author=world.actor(world.ada), step=1)


async def test_artifact_entries_need_an_artifact_id(
    board: PostgresBoardService, world: World
) -> None:
    with pytest.raises(BoardRuleError, match="artifact_id"):
        await board.post(world.id, draft("artifact"), author=world.actor(world.ada), step=1)
    with pytest.raises(BoardRuleError, match="only allowed"):
        await board.post(world.id, draft(artifact_id="x"), author=world.actor(world.ada), step=1)
    entry = await board.post(
        world.id, draft("artifact", artifact_id="art-1"), author=world.actor(world.ada), step=1
    )
    assert entry.artifact_id == "art-1"


async def test_agent_of_another_session_cannot_post(
    board: PostgresBoardService, world: World
) -> None:
    with pytest.raises(BoardRuleError, match="not an agent"):
        await board.post(world.id, draft(), author=Actor.agent("someone-else", "Eve"), step=1)


async def test_release_claim_and_permissions(
    board: PostgresBoardService, world: World, bus: InMemoryBus
) -> None:
    claim = await board.post(
        world.id, draft("claim", "Research Obsidian"), author=world.actor(world.ada), step=1
    )
    with pytest.raises(BoardRuleError, match="author"):
        await board.set_status(claim.id, "resolved", actor=world.actor(world.brook))
    released = await board.set_status(
        claim.id, "resolved", actor=world.actor(world.ada), reason="done"
    )
    assert released.status == "resolved"
    with pytest.raises(BoardRuleError, match="already resolved"):
        await board.set_status(claim.id, "resolved", actor=Actor.supervisor())
    fact = await board.post(world.id, draft(), author=world.actor(world.ada), step=1)
    with pytest.raises(BoardRuleError, match="no status"):
        await board.set_status(fact.id, "resolved", actor=Actor.supervisor())
    assert "board.entry_status_changed" in types(bus, world.id)


async def test_claim_conflict_triggers_the_supervisor(
    board: PostgresBoardService, world: World, bus: InMemoryBus
) -> None:
    first = await board.post(
        world.id,
        draft("claim", "Research Obsidian Sync pricing", tags=["pricing"]),
        author=world.actor(world.ada),
        step=1,
    )
    other = await board.post(
        world.id,
        draft("claim", "Research Obsidian Sync pricing plans", tags=["pricing"]),
        author=world.actor(world.brook),
        step=1,
    )
    triggers = [e.payload for e in bus.published if isinstance(e.payload, SupervisorTriggered)]
    assert len(triggers) == 1
    assert triggers[0].reason == "claim_conflict"
    assert triggers[0].agent_id == world.brook.id
    assert triggers[0].entry_ids == [other.id, first.id]


async def test_no_conflict_for_own_unrelated_or_released_claims(
    board: PostgresBoardService, world: World, bus: InMemoryBus
) -> None:
    a = await board.post(
        world.id, draft("claim", "Research Obsidian pricing"), author=world.actor(world.ada), step=1
    )
    await board.post(  # same author
        world.id, draft("claim", "Research Obsidian pricing"), author=world.actor(world.ada), step=1
    )
    await board.post(  # different scope
        world.id,
        draft("claim", "Draw the logo in Inkscape"),
        author=world.actor(world.brook),
        step=1,
    )
    await board.set_status(a.id, "resolved", actor=world.actor(world.ada))
    for author in (world.brook,):
        await board.post(
            world.id,
            draft("claim", "Research Obsidian pricing"),
            author=world.actor(author),
            step=1,
        )
    # the first duplicate by Ada is still open, so Brook's repeat conflicts exactly once
    triggers = [e for e in bus.published if isinstance(e.payload, SupervisorTriggered)]
    assert len(triggers) == 1


async def test_claims_with_disjoint_tags_do_not_conflict(
    board: PostgresBoardService, world: World, bus: InMemoryBus
) -> None:
    await board.post(
        world.id,
        draft("claim", "Research pricing", tags=["notes"]),
        author=world.actor(world.ada),
        step=1,
    )
    await board.post(
        world.id,
        draft("claim", "Research pricing", tags=["cloud"]),
        author=world.actor(world.brook),
        step=1,
    )
    assert not [e for e in bus.published if isinstance(e.payload, SupervisorTriggered)]


async def test_rate_limits_return_errors(board: PostgresBoardService, world: World) -> None:
    ada = world.actor(world.ada)
    await board.post(world.id, draft("progress", "p1"), author=ada, step=1)
    with pytest.raises(RateLimitedError, match="progress"):
        await board.post(world.id, draft("progress", "p2"), author=ada, step=5)
    for i in range(5):
        await board.post(world.id, draft(title=f"f{i}"), author=ada, step=6)
    with pytest.raises(RateLimitedError, match="post limit"):
        await board.post(world.id, draft(title="one too many"), author=ada, step=7)
    # another agent is unaffected; the window moves on; the supervisor is not limited
    await board.post(world.id, draft(), author=world.actor(world.brook), step=7)
    await board.post(world.id, draft("progress", "p3"), author=ada, step=11)
    await board.post(world.id, draft(), author=Actor.supervisor(), step=7)


async def test_rejected_post_does_not_count_against_the_limit(
    board: PostgresBoardService, world: World
) -> None:
    ada = world.actor(world.ada)
    for _ in range(10):
        with pytest.raises(BoardRuleError):
            await board.post(world.id, draft("decision"), author=ada, step=1)
    await board.post(world.id, draft(), author=ada, step=1)


async def test_search_filters_visibility_kinds_and_tags(
    board: PostgresBoardService, world: World
) -> None:
    ada, brook = world.actor(world.ada), world.actor(world.brook)
    pub = await board.post(
        world.id,
        draft(title="Obsidian pricing", body="yearly $4", tags=["pricing"]),
        author=ada,
        step=1,
    )
    private = await board.post(
        world.id, draft("note", "Obsidian secret", to=[world.cleo.id]), author=brook, step=1
    )
    other = await board.post(
        world.id, draft("claim", "Notion pricing", tags=["cloud"]), author=ada, step=1
    )

    hits = await board.search(world.id, query="obsidian", viewer_agent_id=world.ada.id)
    assert [h.id for h in hits] == [pub.id]
    hits = await board.search(world.id, query="obsidian", viewer_agent_id=world.cleo.id)
    assert {h.id for h in hits} == {pub.id, private.id}
    hits = await board.search(world.id, query="obsidian")  # no viewer: unfiltered
    assert {h.id for h in hits} == {pub.id, private.id}
    assert [h.id for h in await board.search(world.id, kinds=["claim"])] == [other.id]
    assert [h.id for h in await board.search(world.id, tags=["Cloud"])] == [other.id]
    assert [h.id for h in await board.search(world.id, query="pricing", tags=["cloud"])] == [
        other.id
    ]
    newest_first = await board.search(world.id, viewer_agent_id=world.ada.id)
    assert [h.id for h in newest_first] == [other.id, pub.id]
    assert len(await board.search(world.id, limit=1)) == 1
    assert await board.search("another-session", query="obsidian") == []


async def test_entries_after_orders_filters_and_limits(
    board: PostgresBoardService, world: World
) -> None:
    ada, brook = world.actor(world.ada), world.actor(world.brook)
    e1 = await board.post(world.id, draft(title="one"), author=ada, step=1)
    await board.post(world.id, draft("note", "private", to=[world.cleo.id]), author=brook, step=1)
    e3 = await board.post(world.id, draft(title="three"), author=brook, step=1)
    seen = await board.entries_after(world.id, 0, viewer_agent_id=world.ada.id)
    assert [e.id for e in seen] == [e1.id, e3.id]
    assert [e.id for e in await board.entries_after(world.id, e1.seq)] != []
    assert [
        e.id for e in await board.entries_after(world.id, e1.seq, viewer_agent_id=world.ada.id)
    ] == [e3.id]
    assert len(await board.entries_after(world.id, 0, limit=2)) == 2


async def test_get_by_seq_and_find_agent(board: PostgresBoardService, world: World) -> None:
    e = await board.post(world.id, draft(), author=world.actor(world.ada), step=1)
    assert (await board.get_by_seq(world.id, e.seq)).id == e.id
    with pytest.raises(NotFoundError):
        await board.get_by_seq(world.id, e.seq + 50)
    with pytest.raises(NotFoundError):
        await board.get("missing")
    brook = await board.find_agent(world.id, " BROOK ")
    assert brook is not None
    assert brook.id == world.brook.id
    assert await board.find_agent(world.id, "Zed") is None


async def test_wait_for_answer_returns_existing_answer(
    board: PostgresBoardService, world: World
) -> None:
    q = await board.post(world.id, draft("question"), author=world.actor(world.ada), step=1)
    a = await board.post(
        world.id, draft("answer", "yes", refs=[q.id]), author=Actor.supervisor(), step=1
    )
    got = await board.wait_for_answer(q.id, 1)
    assert got is not None
    assert got.id == a.id


async def test_wait_for_answer_wakes_on_local_post(
    board: PostgresBoardService, world: World
) -> None:
    q = await board.post(world.id, draft("question"), author=world.actor(world.ada), step=1)
    waiter = asyncio.create_task(board.wait_for_answer(q.id, 10))
    await asyncio.sleep(0.1)
    assert not waiter.done()
    a = await board.post(
        world.id, draft("answer", "42", refs=[q.id]), author=Actor.supervisor(), step=1
    )
    got = await asyncio.wait_for(waiter, 2)
    assert got is not None
    assert got.id == a.id


async def test_wait_for_answer_sees_posts_from_another_service_instance(
    board: PostgresBoardService, world: World, sessionmaker: Any, recorder: EventRecorder
) -> None:
    other_process = PostgresBoardService(sessionmaker, recorder)
    q = await board.post(world.id, draft("question"), author=world.actor(world.ada), step=1)
    waiter = asyncio.create_task(board.wait_for_answer(q.id, 10))
    await asyncio.sleep(0.1)
    await other_process.post(
        world.id, draft("answer", refs=[q.id]), author=Actor.supervisor(), step=1
    )
    assert await asyncio.wait_for(waiter, 2) is not None  # found by polling


async def test_wait_for_answer_times_out_and_cleans_up(
    board: PostgresBoardService, world: World
) -> None:
    q = await board.post(world.id, draft("question"), author=world.actor(world.ada), step=1)
    assert await board.wait_for_answer(q.id, 0.15) is None
    assert not board._waiters
    with pytest.raises(NotFoundError):
        await board.wait_for_answer("missing", 1)


async def test_hidden_status_changes_look_like_missing_entries(
    board: PostgresBoardService, world: World, bus: InMemoryBus
) -> None:
    claim = await board.post(
        world.id, draft("claim", to=[world.brook.id]), author=world.actor(world.ada), step=1
    )
    before = list(bus.published)
    for entry_id in (claim.id, "missing-entry"):
        with pytest.raises(NotFoundError) as caught:
            await board.set_status(entry_id, "resolved", actor=world.actor(world.cleo))
        assert str(caught.value) == f"board entry {entry_id} does not exist"
    assert bus.published == before
    assert (await board.get(claim.id)).status == "open"
    # Visible recipients still get the ordinary permission error.
    with pytest.raises(BoardRuleError, match="author"):
        await board.set_status(claim.id, "resolved", actor=world.actor(world.brook))
    await board.set_status(claim.id, "resolved", actor=world.actor(world.ada))


@pytest.mark.parametrize("kind", ["fact", "progress"])
async def test_agent_posts_require_a_step(
    board: PostgresBoardService, world: World, bus: InMemoryBus, kind: str
) -> None:
    before = list(bus.published)
    with pytest.raises(BoardRuleError, match="step"):
        await board.post(world.id, draft(kind), author=world.actor(world.ada), step=None)
    with pytest.raises(BoardRuleError, match="step"):
        await board.post(world.id, draft(kind), author=world.actor(world.ada))
    assert bus.published == before
    assert await board.search(world.id) == []
    await board.post(world.id, draft(kind), author=world.actor(world.ada), step=1)
    if kind == "progress":
        with pytest.raises(RateLimitedError):
            await board.post(world.id, draft(kind), author=world.actor(world.ada), step=2)
    for author in (Actor.supervisor(), Actor.user("local"), Actor.system()):
        await board.post(world.id, draft("fact"), author=author)
