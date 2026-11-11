"""Scenarios over the real Postgres stores (marker ``infra``; ``make db-up``).

The fake-based scenario files prove the behaviour; these re-run the scenarios whose
fakes stand in for real SQL (board rules and resolution, artifact metadata, approval
rows, session leases and read models) against Postgres: 1, 5, 6, 9 and 11."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from runtime.support import Behavior, payload, until

from agentplatform.contracts.events import (
    ApprovalRequested,
    ArtifactPublished,
    BoardEntryPosted,
    TaskStarted,
)
from agentplatform.db.repositories import Repositories
from agentplatform.db.session import SessionMaker, transaction
from scenarios.harness import (
    AgentCall,
    ScriptedSupervisor,
    gate,
    multi_tool_response,
    shell,
    tool_response,
)
from scenarios.pg import PgScenarioWorld

pytestmark = pytest.mark.infra


@pytest.fixture
async def pg(sessionmaker: SessionMaker) -> AsyncIterator[PgScenarioWorld]:
    world = PgScenarioWorld(supervisor_model=ScriptedSupervisor(answers="Use EUR"))
    world.attach(sessionmaker)
    yield world
    await world.close()


async def _status(world: PgScenarioWorld, sid: str) -> str:
    assert world.sessionmaker is not None
    async with transaction(world.sessionmaker) as tx:
        return (await Repositories(tx).sessions.get(sid)).status


async def test_s01_single_agent_over_postgres(pg: PgScenarioWorld) -> None:
    await pg.runtime()
    sid = await pg.approved_session({"T1": []})
    await pg.wait_event(sid, "session.completed", limit_s=20)
    assert pg.sessionmaker is not None
    async with transaction(pg.sessionmaker) as tx:
        repos = Repositories(tx)
        tasks = await repos.tasks.list(sid)
        assert [(t.key, t.status) for t in tasks] == [("T1", "done")]
        assert tasks[0].result is not None
        assert (await repos.sessions.get(sid)).status == "completed"
    stored = await pg.store.read(sid, 0, 5000)
    assert [e.seq for e in stored] == list(range(1, len(stored) + 1))


async def test_s05_question_resolved_by_the_real_board(pg: PgScenarioWorld) -> None:
    m = pg.agent_model
    m.behaviors = {"T1": Behavior(steps=6)}
    m.script(
        "T1",
        2,
        multi_tool_response(
            ("post", {"kind": "question", "title": "Which currency?"}),
            ("run_command", {"cmd": "echo asked"}),
        ),
    )
    release = asyncio.Event()
    m.script("T1", 3, lambda _call: gate(release, shell("waiting")))
    await pg.runtime()
    sid = await pg.approved_session({"T1": []})
    q = await pg.wait_event(
        sid, "board.entry_posted", lambda e: payload(e, BoardEntryPosted).entry.kind == "question"
    )
    a = await pg.wait_event(
        sid,
        "board.entry_posted",
        lambda e: payload(e, BoardEntryPosted).entry.kind == "answer",
        limit_s=15,
    )
    release.set()
    await pg.wait_event(sid, "session.completed", limit_s=20)
    question = await pg.board.get(payload(q, BoardEntryPosted).entry.id)
    assert question.status == "resolved"
    answer = payload(a, BoardEntryPosted).entry
    assert f"[#{answer.seq} answer · Supervisor] Use EUR" in m.of("T1")[3].inbox


async def test_s06_artifact_over_the_database_metadata(pg: PgScenarioWorld) -> None:
    m = pg.agent_model
    m.behaviors = {"T1": Behavior(steps=4), "T2": Behavior(steps=4)}
    holder: dict[str, str] = {}
    shared: dict[str, bytes] = {}

    def publish(call: AgentCall) -> object:
        sid = holder["sid"]
        pg.sandbox_of(sid, pg.agent_named(sid, call.agent)).files["/home/agent/a.txt"] = b"hi"
        return multi_tool_response(
            ("publish_artifact", {"sandbox_path": "/home/agent/a.txt"}),
            ("run_command", {"cmd": "echo ok"}),
        )

    def first(call: AgentCall) -> object:
        sid = holder["sid"]
        files = pg.sandbox_of(sid, pg.agent_named(sid, call.agent)).files
        shared.update({p: d for p, d in files.items() if p.startswith("/home/agent/Shared/")})
        return shell("start")

    m.script("T1", 2, publish)  # type: ignore[arg-type]
    m.script("T2", 1, first)  # type: ignore[arg-type]
    await pg.runtime()
    pg.models.planner.plans = []
    from agentplatform.testing import make_tasks

    pg.models.planner.plans = [make_tasks({"T1": [], "T2": ["T1"]})]
    session = await pg.create_session()
    holder["sid"] = session.id
    await pg.send("command.plan_session", session.id)
    await pg.wait_event(session.id, "session.plan_proposed")
    await pg.send("command.approve_plan", session.id, plan_version=1)
    await pg.wait_event(session.id, "session.completed", limit_s=20)
    published = [payload(e, ArtifactPublished) for e in pg.events(session.id, "artifact.published")]
    assert [p.artifact.task_key for p in published] == ["T1"]
    assert shared == {"/home/agent/Shared/a.txt": b"hi"}


async def test_s09_approval_rows_over_postgres(pg: PgScenarioWorld) -> None:
    m = pg.agent_model
    m.behaviors = {"T1": Behavior(steps=5)}
    m.script(
        "T1", 2, tool_response("request_approval", action_summary="Send the email", risk="low")
    )
    await pg.runtime()
    sid = await pg.approved_session({"T1": []})
    req = await pg.wait_event(sid, "approval.requested")
    approval = payload(req, ApprovalRequested).approval
    await pg.send("command.decide_approval", sid, approval_id=approval.id, decision="approve")
    await pg.wait_event(sid, "session.completed", limit_s=20)
    assert m.of("T1")[2].tool_results == ("Approved: Send the email",)


async def test_s11_restart_over_postgres(pg: PgScenarioWorld) -> None:
    pg.agent_model.behaviors = {"T1": Behavior(steps=40, latency_s=0.02)}
    first = await pg.runtime("rt-a")
    sid = await pg.approved_session({"T1": [], "T2": ["T1"]})
    await pg.wait_event(sid, "agent.step")
    await first.crash()
    await pg.expire_session_lease(sid)
    pg.agent_model.behaviors = {"T1": Behavior(steps=4)}
    second = await pg.runtime("rt-b")
    assert sid in second.hosts
    await pg.wait_event(sid, "session.completed", limit_s=30)
    attempts = [
        payload(e, TaskStarted).attempt
        for e in pg.events(sid, "task.started")
        if payload(e, TaskStarted).task_key == "T1"
    ]
    assert attempts == [1, 2]
    await until(lambda: True)
    assert await _status(pg, sid) == "completed"
    assert pg.sessionmaker is not None
    async with transaction(pg.sessionmaker) as tx:
        tasks = await Repositories(tx).tasks.list(sid)
    assert sorted((t.key, t.status, t.attempt) for t in tasks) == [
        ("T1", "done", 2),
        ("T2", "done", 1),
    ]
