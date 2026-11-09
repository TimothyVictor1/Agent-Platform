"""Repositories return contract models and enforce their rules (SPEC §6, §17).

Each test runs in one transaction (``tx`` fixture) that is rolled back afterwards.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agentplatform.contracts.agent import SandboxRef
from agentplatform.contracts.artifact import Approval, Artifact, Frame
from agentplatform.contracts.common import Actor, new_id, utcnow
from agentplatform.contracts.plan import Plan
from agentplatform.contracts.session import Session, Usage
from agentplatform.contracts.task import TaskResult
from agentplatform.db.repositories import Repositories
from agentplatform.errors import ConflictError, NotFoundError
from agentplatform.testing.builders import (
    make_agent,
    make_board_entry,
    make_plan,
    make_session,
    make_task,
)

pytestmark = pytest.mark.infra

ADA = Actor.agent("a-ada", "Ada")
BROOK = Actor.agent("a-brook", "Brook")


@pytest.fixture
def repos(tx: AsyncSession) -> Repositories:
    return Repositories(tx)


@pytest.fixture
async def session(repos: Repositories) -> Session:
    s = make_session()
    await repos.sessions.insert(s)
    return s


# --- sessions ---------------------------------------------------------------------------


async def test_session_insert_get_and_conflict(repos: Repositories, session: Session) -> None:
    assert await repos.sessions.get(session.id) == session
    with pytest.raises(ConflictError):
        await repos.sessions.insert(session)
    await repos.sessions.insert(session, if_missing=True)  # no error
    assert await repos.sessions.find("missing") is None
    with pytest.raises(NotFoundError):
        await repos.sessions.get("missing")


async def test_session_updates(repos: Repositories, session: Session) -> None:
    started = utcnow()
    running = await repos.sessions.set_status(session.id, "running", started_at=started)
    assert running.status == "running"
    assert running.started_at == started
    assert running.ended_at is None

    assert (await repos.sessions.set_plan_version(session.id, 2)).plan_version == 2

    await repos.sessions.add_usage(session.id, Usage(steps=1, input_tokens=100, cost_usd=0.5))
    total = await repos.sessions.add_usage(session.id, Usage(steps=2, output_tokens=7))
    assert total.usage == Usage(steps=3, input_tokens=100, output_tokens=7, cost_usd=0.5)

    edited = total.model_copy(update={"title": "Renamed"})
    assert await repos.sessions.save(edited) == edited

    await repos.sessions.set_runtime_owner(session.id, "runtime-1", utcnow())
    with pytest.raises(NotFoundError):
        await repos.sessions.set_status("missing", "running")


async def test_session_list_filters_newest_first(repos: Repositories) -> None:
    older = make_session()
    newer = make_session().model_copy(
        update={"created_at": older.created_at + timedelta(seconds=1), "user_id": "u-list"}
    )
    older = older.model_copy(update={"user_id": "u-list"})
    await repos.sessions.insert(older)
    await repos.sessions.insert(newer)
    await repos.sessions.set_status(newer.id, "running")

    listed = await repos.sessions.list(user_id="u-list")
    assert [s.id for s in listed] == [newer.id, older.id]
    running = await repos.sessions.list(user_id="u-list", statuses=["running"])
    assert [s.id for s in running] == [newer.id]
    assert len(await repos.sessions.list(user_id="u-list", limit=1, offset=1)) == 1


# --- plans ------------------------------------------------------------------------------


async def test_plans_are_versioned(repos: Repositories, session: Session) -> None:
    assert await repos.plans.next_version(session.id) == 1
    assert await repos.plans.find(session.id) is None
    v1 = Plan(session_id=session.id, version=1, proposal=make_plan(), created_at=utcnow())
    v2 = Plan(
        session_id=session.id,
        version=2,
        proposal=make_plan({"T1": [], "T2": ["T1"]}),
        created_at=utcnow(),
    )
    await repos.plans.insert(v1)
    await repos.plans.insert(v2, source="user")

    assert await repos.plans.next_version(session.id) == 3
    assert await repos.plans.get(session.id) == v2
    assert await repos.plans.get(session.id, 1) == v1
    assert await repos.plans.list(session.id) == [v1, v2]
    with pytest.raises(ConflictError):
        await repos.plans.insert(v1)

    at = utcnow()
    approved = await repos.plans.approve(session.id, 2, at)
    assert approved.approved_at == at
    with pytest.raises(NotFoundError):
        await repos.plans.get(session.id, 9)
    with pytest.raises(NotFoundError):
        await repos.plans.approve(session.id, 9, at)


# --- tasks ------------------------------------------------------------------------------


async def test_task_upsert_get_list(repos: Repositories, session: Session) -> None:
    t1 = make_task(session_id=session.id, key="T1")
    t2 = make_task(session_id=session.id, key="T2", depends_on=["T1"], inputs=[("T1", "result")])
    await repos.tasks.upsert(t1)
    await repos.tasks.upsert(t2)

    assert await repos.tasks.get(t1.id) == t1
    assert await repos.tasks.get_by_key(session.id, "T2") == t2
    assert [t.key for t in await repos.tasks.list(session.id)] == ["T1", "T2"]

    done = t1.model_copy(
        update={
            "status": "done",
            "attempt": 1,
            "assigned_agent_id": "a-ada",
            "result": TaskResult(summary="ok", outputs={"result": {"n": 3}}),
        }
    )
    assert await repos.tasks.upsert(done) == done
    assert await repos.tasks.get(t1.id) == done
    assert [t.key for t in await repos.tasks.list(session.id, statuses=["pending"])] == ["T2"]

    ready = await repos.tasks.set_status(t2.id, "ready")
    assert ready.status == "ready"

    duplicate_key = make_task(session_id=session.id, key="T1")
    with pytest.raises(ConflictError):
        await repos.tasks.upsert(duplicate_key)
    with pytest.raises(NotFoundError):
        await repos.tasks.get_by_key(session.id, "T9")
    with pytest.raises(NotFoundError):
        await repos.tasks.set_status("missing", "ready")


# --- agents -----------------------------------------------------------------------------


async def test_agents(repos: Repositories, session: Session) -> None:
    ada = make_agent(0, session_id=session.id)
    brook = make_agent(1, session_id=session.id)
    await repos.agents.upsert(ada)
    await repos.agents.upsert(brook)
    assert [a.name for a in await repos.agents.list(session.id)] == ["Ada", "Brook"]

    ref = SandboxRef(provider="docker_local", sandbox_id="c-1", created_at=utcnow())
    ready = ada.model_copy(update={"sandbox": ref, "status": "idle", "stream_url": "http://x"})
    assert await repos.agents.upsert(ready) == ready

    working = await repos.agents.set_status(ada.id, "working")
    assert working.status == "working"
    at = utcnow()
    beat = await repos.agents.record_heartbeat(ada.id, at, step=12)
    assert (beat.last_heartbeat_at, beat.step) == (at, 12)

    with pytest.raises(ConflictError):  # names are unique per session
        await repos.agents.upsert(make_agent(0, session_id=session.id))
    with pytest.raises(NotFoundError):
        await repos.agents.set_status("missing", "idle")


# --- board ------------------------------------------------------------------------------


async def test_board_insert_get_status_and_list(repos: Repositories, session: Session) -> None:
    sid = session.id
    q = make_board_entry("question", seq=2, session_id=sid, title="Which region?", tags=["eu"])
    f = make_board_entry("fact", seq=3, session_id=sid, author=BROOK, tags=["pricing"])
    dm = make_board_entry("note", seq=4, session_id=sid, to=["a-ada"], task_key="T2")
    for e in (q, f, dm):
        await repos.board.insert(e)

    assert await repos.board.get(q.id) == q
    assert await repos.board.get(dm.id) == dm  # `to` round-trips
    with pytest.raises(ConflictError):  # (session_id, seq) is unique
        await repos.board.insert(make_board_entry("fact", seq=2, session_id=sid))

    resolved = await repos.board.set_status(q.id, "resolved")
    assert resolved.status == "resolved"

    assert [e.seq for e in await repos.board.list(sid)] == [2, 3, 4]
    assert [e.seq for e in await repos.board.list(sid, after_seq=2)] == [3, 4]
    assert [e.id for e in await repos.board.list(sid, kinds=["fact"])] == [f.id]
    assert [e.id for e in await repos.board.list(sid, statuses=["resolved"])] == [q.id]
    assert [e.id for e in await repos.board.list(sid, tags=["eu", "x"])] == [q.id]
    assert [e.id for e in await repos.board.list(sid, task_key="T2")] == [dm.id]
    assert [e.id for e in await repos.board.list(sid, author_id="a-brook")] == [f.id]
    assert len(await repos.board.list(sid, limit=1)) == 1
    with pytest.raises(NotFoundError):
        await repos.board.set_status("missing", "resolved")


async def test_board_full_text_search(repos: Repositories, session: Session) -> None:
    sid = session.id
    pricing = make_board_entry(
        "fact",
        seq=2,
        session_id=sid,
        title="Notion pricing page",
        body="The Plus plan costs 10 USD per user per month. Source: https://notion.so/pricing",
    )
    other = make_board_entry("fact", seq=3, session_id=sid, title="Obsidian sync is paid")
    claim = make_board_entry("claim", seq=4, session_id=sid, title="Checking Notion plans")
    for e in (pricing, other, claim):
        await repos.board.insert(e)
    elsewhere = make_session()
    await repos.sessions.insert(elsewhere)
    await repos.board.insert(
        make_board_entry("fact", seq=2, session_id=elsewhere.id, title="Notion")
    )

    # Stemming: "prices" matches "pricing"; body text is searched too.
    assert [e.id for e in await repos.board.search(sid, "prices")] == [pricing.id]
    assert [e.id for e in await repos.board.search(sid, "month user")] == [pricing.id]
    assert {e.id for e in await repos.board.search(sid, "notion")} == {pricing.id, claim.id}
    assert [e.id for e in await repos.board.search(sid, "notion", kinds=["claim"])] == [claim.id]
    assert [e.id for e in await repos.board.search(sid, "notion -checking")] == [pricing.id]
    assert await repos.board.search(sid, "   ") == []
    assert await repos.board.search(sid, "'); DROP TABLE board_entries; --") == []


async def test_similar_claims(repos: Repositories, session: Session) -> None:
    sid = session.id
    notion = make_board_entry(
        "claim", seq=2, session_id=sid, title="Research Notion pricing", tags=["notion", "pricing"]
    )
    obsidian = make_board_entry(
        "claim", seq=3, session_id=sid, author=BROOK, title="Research Obsidian pricing"
    )
    released = make_board_entry(
        "claim", seq=4, session_id=sid, title="Research Notion pricing", status="resolved"
    )
    fact = make_board_entry("fact", seq=5, session_id=sid, title="Research Notion pricing")
    for e in (notion, obsidian, released, fact):
        await repos.board.insert(e)

    matches = await repos.board.similar_claims(sid, "Researching Notion pricing tiers", [])
    assert matches[0].entry.id == notion.id
    assert {m.entry.id for m in matches} <= {notion.id, obsidian.id}
    assert all(0.35 <= m.score <= 1.0 for m in matches)
    assert matches == sorted(matches, key=lambda m: m.score, reverse=True)

    # Only open claims count; resolved claims and other kinds are ignored.
    exact = await repos.board.similar_claims(sid, "Research Notion pricing", [], threshold=0.99)
    assert [m.entry.id for m in exact] == [notion.id]

    # Unrelated titles do not match; a shared tag adds a small boost.
    assert await repos.board.similar_claims(sid, "Book a flight to Oslo", []) == []
    plain = await repos.board.similar_claims(sid, "Notion plans", [], threshold=0.0)
    boosted = await repos.board.similar_claims(sid, "Notion plans", ["notion"], threshold=0.0)
    score = {m.entry.id: m.score for m in plain}
    boosted_score = {m.entry.id: m.score for m in boosted}
    assert boosted_score[notion.id] == pytest.approx(score[notion.id] + 0.1)
    assert boosted_score[obsidian.id] == pytest.approx(score[obsidian.id])

    # A claimant's own claims can be excluded.
    others = await repos.board.similar_claims(
        sid, "Research Notion pricing", [], exclude_author_id="a-1"
    )
    assert [m.entry.id for m in others] == [obsidian.id]


# --- artifacts, approvals, frames -------------------------------------------------------


def _artifact(session_id: str, name: str, version: int) -> Artifact:
    return Artifact(
        id=new_id(),
        session_id=session_id,
        name=name,
        mime="text/csv",
        size_bytes=12,
        sha256="0" * 64,
        produced_by=ADA,
        task_key="T1",
        storage_key=f"{session_id}/{name}/{version}",
        version=version,
        created_at=utcnow(),
    )


async def test_artifacts(repos: Repositories, session: Session) -> None:
    sid = session.id
    assert await repos.artifacts.next_version(sid, "prices.csv") == 1
    v1 = await repos.artifacts.insert(_artifact(sid, "prices.csv", 1))
    v2 = await repos.artifacts.insert(_artifact(sid, "prices.csv", 2))
    other = await repos.artifacts.insert(
        _artifact(sid, "chart.png", 1).model_copy(update={"task_key": "T2"})
    )

    assert await repos.artifacts.get(v1.id) == v1
    assert await repos.artifacts.latest(sid, "prices.csv") == v2
    assert await repos.artifacts.latest(sid, "nope") is None
    assert await repos.artifacts.next_version(sid, "prices.csv") == 3
    assert {a.id for a in await repos.artifacts.list(sid)} == {v1.id, v2.id, other.id}
    assert [a.id for a in await repos.artifacts.list(sid, task_key="T2")] == [other.id]
    with pytest.raises(ConflictError):
        await repos.artifacts.insert(_artifact(sid, "prices.csv", 2))
    with pytest.raises(NotFoundError):
        await repos.artifacts.get("missing")


async def test_approvals_decide_once(repos: Repositories, session: Session) -> None:
    now = utcnow()
    approval = Approval(
        id=new_id(),
        session_id=session.id,
        agent_id="a-ada",
        action_summary="Submit the signup form",
        risk="high",
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    await repos.approvals.insert(approval)
    assert await repos.approvals.get(approval.id) == approval
    assert await repos.approvals.list(session.id, statuses=["pending"]) == [approval]
    assert await repos.approvals.due_for_expiry(now) == []
    due = await repos.approvals.due_for_expiry(now + timedelta(minutes=6))
    assert approval.id in {a.id for a in due}

    user = Actor.user("local")
    decided = await repos.approvals.decide(
        approval.id, "approved", decided_by=user, decided_at=now, note="go"
    )
    assert (decided.status, decided.decided_by, decided.note) == ("approved", user, "go")
    with pytest.raises(ConflictError):
        await repos.approvals.decide(approval.id, "denied", decided_by=user, decided_at=now)
    with pytest.raises(ValueError, match="final status"):
        await repos.approvals.decide(approval.id, "pending", decided_by=user, decided_at=now)
    with pytest.raises(NotFoundError):
        await repos.approvals.decide("missing", "denied", decided_by=user, decided_at=now)
    assert await repos.approvals.list(session.id, statuses=["pending"]) == []


async def test_frames(repos: Repositories, session: Session) -> None:
    t0 = utcnow()

    def frame(agent_id: str, step: int) -> Frame:
        return Frame(
            id=new_id(),
            session_id=session.id,
            agent_id=agent_id,
            step=step,
            ts=t0 + timedelta(seconds=step),
            storage_key_small=f"frames/{agent_id}/{step}.jpg",
            action_label=f"click {step}",
            phash="ff00ff00ff00ff00",
        )

    frames = [frame("a-ada", 1), frame("a-brook", 2), frame("a-ada", 3)]
    for f in frames:
        await repos.frames.insert(f)

    assert await repos.frames.get(frames[0].id) == frames[0]
    assert await repos.frames.list(session.id) == frames
    assert await repos.frames.list(session.id, agent_id="a-ada") == [frames[0], frames[2]]
    assert await repos.frames.list(session.id, after_ts=frames[0].ts) == frames[1:]
    assert await repos.frames.latest(session.id, "a-ada") == frames[2]
    assert await repos.frames.latest(session.id, "a-nobody") is None
    with pytest.raises(ConflictError):
        await repos.frames.insert(frames[0])
