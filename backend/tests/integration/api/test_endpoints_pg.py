"""Every REST endpoint against real Postgres read models (marker ``infra``).

Each command endpoint must enqueue exactly one contract command whose ``id`` equals
the returned ``command_id`` (the runtime copies it into ``causation_id``).
"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from agentplatform.api.app import create_app
from agentplatform.api.deps import ApiDeps
from agentplatform.api.routes.replay import FRAME_CACHE_CONTROL
from agentplatform.bus.base import NEW_SESSIONS_QUEUE, QueueItem, session_command_queue
from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Approval, Frame
from agentplatform.contracts.commands import (
    Command,
    CommandBase,
    PlanSessionCommand,
    PostBoardCommand,
    UpdatePlanCommand,
)
from agentplatform.contracts.common import (
    Actor,
    AgentStatus,
    SessionStatus,
    TaskStatus,
    new_id,
    utcnow,
)
from agentplatform.contracts.events import EventDraft, SessionPaused
from agentplatform.contracts.plan import Plan
from agentplatform.contracts.session import Session
from agentplatform.db.event_store import PostgresEventStore
from agentplatform.db.repositories import Repositories
from agentplatform.db.session import SessionMaker, transaction
from agentplatform.testing import (
    InMemoryBus,
    make_agent,
    make_board_entry,
    make_plan,
    make_task,
    solid_png,
)

from .conftest import build_deps, client_for, create_db_session

pytestmark = pytest.mark.infra


@dataclass
class Api:
    app: FastAPI
    deps: ApiDeps
    client: httpx.AsyncClient
    bus: InMemoryBus
    store: PostgresEventStore
    sm: SessionMaker

    async def session(
        self,
        status: SessionStatus = "planning",
        *,
        plan: bool = False,
        started: bool = False,
        ended: bool = False,
    ) -> Session:
        session = await create_db_session(self.store)
        async with transaction(self.sm) as tx:
            repos = Repositories(tx)
            if plan:
                await repos.plans.insert(
                    Plan(
                        session_id=session.id, version=1, proposal=make_plan(), created_at=utcnow()
                    )
                )
                await repos.sessions.set_plan_version(session.id, 1)
            session = await repos.sessions.set_status(
                session.id,
                status,
                started_at=utcnow() if started or status in {"running", "paused"} else None,
                ended_at=utcnow() if ended else None,
            )
        return session

    async def agent(
        self, session: Session, index: int = 0, status: AgentStatus = "working"
    ) -> Agent:
        agent = make_agent(index, session_id=session.id).model_copy(update={"status": status})
        async with transaction(self.sm) as tx:
            await Repositories(tx).agents.upsert(agent)
        return agent

    def commands(self) -> list[tuple[str, QueueItem]]:
        return list(self.bus.enqueued)

    def last_command(self, response: httpx.Response, queue: str) -> Command:
        """The single command enqueued by ``response``; its id must be the returned one."""
        assert response.status_code == 202, response.text
        command_id = response.json()["command_id"]
        matches = [
            (q, item)
            for q, item in self.bus.enqueued
            if isinstance(item, CommandBase) and item.id == command_id
        ]
        assert len(matches) == 1
        q, item = matches[0]
        assert q == queue
        assert isinstance(item, CommandBase)
        assert item.issued_by.type == "user"
        return item


@pytest.fixture
async def api(sessionmaker: SessionMaker, pg_store: PostgresEventStore) -> AsyncIterator[Api]:
    bus = InMemoryBus()
    deps = build_deps(sessionmaker=sessionmaker, events=pg_store, bus=bus, max_upload_bytes=4096)
    app = create_app(deps)
    async with client_for(app) as client:
        yield Api(app, deps, client, bus, pg_store, sessionmaker)
    await deps.providers_health.aclose()


def problem(response: httpx.Response, status: int) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == "application/problem+json"
    body: dict[str, Any] = response.json()
    assert body["status"] == status
    return body


# --- sessions --------------------------------------------------------------------------


async def test_create_session_records_event_and_enqueues_plan_command(api: Api) -> None:
    response = await api.client.post(
        "/api/v1/sessions", json={"prompt": "  Compare\n note   apps ", "model_profile": "test"}
    )
    assert response.status_code == 201, response.text
    session = response.json()
    assert session["status"] == "planning"
    assert session["title"] == "Compare note apps"
    assert session["budgets"]["max_cost_usd"] == 5.0  # policy default
    (created,) = await api.store.read(session["id"])
    assert created.type == "session.created"
    assert created.causation_id is None
    assert created.actor.type == "user"
    ((queue, command),) = api.commands()
    assert queue == NEW_SESSIONS_QUEUE
    assert isinstance(command, PlanSessionCommand)
    assert command.session_id == session["id"]
    detail = await api.client.get(f"/api/v1/sessions/{session['id']}")
    assert detail.json()["session"]["id"] == session["id"]


async def test_create_session_rejects_unknown_profile_and_unconfigured_provider(api: Api) -> None:
    bad_profile = await api.client.post(
        "/api/v1/sessions", json={"prompt": "x", "model_profile": "nope"}
    )
    assert problem(bad_profile, 422)["errors"][0]["loc"] == ["body", "model_profile"]
    bad_provider = await api.client.post(
        "/api/v1/sessions",
        json={"prompt": "x", "model_profile": "test", "sandbox_provider": "e2b"},
    )
    problem(bad_provider, 422)
    invalid = await api.client.post("/api/v1/sessions", json={"prompt": "", "extra": 1})
    locs = [e["loc"] for e in problem(invalid, 422)["errors"]]
    assert ["body", "prompt"] in locs
    assert not api.commands()


async def test_create_session_returns_503_when_the_plan_command_cannot_be_enqueued(
    api: Api,
) -> None:
    async def broken(queue: str, item: QueueItem) -> str:
        raise ConnectionError("redis down")

    api.bus.enqueue = broken  # type: ignore[method-assign]
    response = await api.client.post(
        "/api/v1/sessions", json={"prompt": "do it", "model_profile": "test"}
    )
    problem(response, 503)
    assert "redis" not in response.text
    page = (await api.client.get("/api/v1/sessions", params={"limit": 200})).json()
    assert any(s["status"] == "planning" for s in page["items"])


async def test_list_sessions_paginates_newest_first(api: Api) -> None:
    created = [await api.session() for _ in range(3)]
    first = await api.client.get("/api/v1/sessions", params={"limit": 2})
    assert first.status_code == 200
    page1 = first.json()
    assert len(page1["items"]) == 2
    assert page1["next_cursor"]
    ids: list[str] = [s["id"] for s in page1["items"]]
    cursor = page1["next_cursor"]
    while cursor:
        page = (
            await api.client.get("/api/v1/sessions", params={"limit": 2, "cursor": cursor})
        ).json()
        ids += [s["id"] for s in page["items"]]
        cursor = page["next_cursor"]
    assert len(ids) == len(set(ids))
    positions = [ids.index(s.id) for s in created]
    assert positions == sorted(positions, reverse=True)  # newest first
    problem(await api.client.get("/api/v1/sessions", params={"cursor": "%%%"}), 422)


async def test_get_session_detail_and_404(api: Api) -> None:
    session = await api.session("running", plan=True)
    agent = await api.agent(session)
    async with transaction(api.sm) as tx:
        repos = Repositories(tx)
        statuses: list[TaskStatus] = ["done", "running", "running"]
        for i, st in enumerate(statuses):
            await repos.tasks.upsert(make_task(session_id=session.id, status=st, key=f"T{i + 1}"))
    body = (await api.client.get(f"/api/v1/sessions/{session.id}")).json()
    assert body["session"]["status"] == "running"
    assert body["plan"]["version"] == 1
    assert [a["id"] for a in body["agents"]] == [agent.id]
    assert body["counts"] == {"by_status": {"done": 1, "running": 2}, "total": 3}
    assert body["last_seq"] == 1
    problem(await api.client.get("/api/v1/sessions/missing"), 404)


# --- plan commands ------------------------------------------------------------------------


async def test_refine_enqueues_command_or_409_without_plan(api: Api) -> None:
    session = await api.session("awaiting_approval", plan=True)
    response = await api.client.post(
        f"/api/v1/sessions/{session.id}/refine", json={"instruction": "use 2 agents"}
    )
    cmd = api.last_command(response, session_command_queue(session.id))
    assert cmd.type == "command.refine_plan"
    empty = await api.session("planning")
    problem(
        await api.client.post(f"/api/v1/sessions/{empty.id}/refine", json={"instruction": "x"}), 409
    )


async def test_put_plan_enqueues_update_with_the_returned_command_id(api: Api) -> None:
    session = await api.session("awaiting_approval", plan=True)
    tasks = [t.model_dump(mode="json") for t in make_plan({"T1": [], "T2": ["T1"]}).tasks]
    response = await api.client.put(
        f"/api/v1/sessions/{session.id}/plan", json={"tasks": tasks, "base_version": 1}
    )
    cmd = api.last_command(response, session_command_queue(session.id))
    assert isinstance(cmd, UpdatePlanCommand)
    assert cmd.base_version == 1
    assert [t.key for t in cmd.tasks] == ["T1", "T2"]
    # causation: the runtime sets causation_id = cmd.id on session.plan_updated
    assert cmd.id == response.json()["command_id"]


async def test_put_plan_returns_per_task_plan_errors(api: Api) -> None:
    session = await api.session("awaiting_approval", plan=True)
    tasks = [t.model_dump(mode="json") for t in make_plan({"T1": ["T2"], "T2": ["T1"]}).tasks]
    tasks.append({**tasks[0], "key": "T3", "depends_on": ["T9"], "inputs": []})
    response = await api.client.put(f"/api/v1/sessions/{session.id}/plan", json={"tasks": tasks})
    body = problem(response, 422)
    assert body["type"].endswith(":plan_invalid")
    codes = {(e["code"], e["task_key"]) for e in body["errors"]}
    assert ("unknown_dependency", "T3") in codes
    cycle = next(e for e in body["errors"] if e["code"] == "cycle")
    assert set(cycle["details"]["cycle"]) >= {"T1", "T2"}
    assert not api.commands()


async def test_put_plan_conflicts_and_shape_errors(api: Api) -> None:
    session = await api.session("awaiting_approval", plan=True)
    tasks = [t.model_dump(mode="json") for t in make_plan({"T1": []}).tasks]
    stale = await api.client.put(
        f"/api/v1/sessions/{session.id}/plan", json={"tasks": tasks, "base_version": 7}
    )
    problem(stale, 409)
    bad_shape = await api.client.put(
        f"/api/v1/sessions/{session.id}/plan", json={"tasks": [{**tasks[0], "key": "bad key!"}]}
    )
    loc = problem(bad_shape, 422)["errors"][0]["loc"]
    assert loc[:3] == ["body", "tasks", 0]
    no_plan = await api.session("planning")
    body = problem(
        await api.client.put(f"/api/v1/sessions/{no_plan.id}/plan", json={"tasks": tasks}), 409
    )
    assert body["detail"] == "no plan to edit yet"
    running = await api.session("running", plan=True)
    problem(await api.client.put(f"/api/v1/sessions/{running.id}/plan", json={"tasks": tasks}), 409)
    assert not api.commands()


async def test_approve_current_version_or_409(api: Api) -> None:
    session = await api.session("awaiting_approval", plan=True)
    no_body = await api.client.post(f"/api/v1/sessions/{session.id}/approve")
    cmd = api.last_command(no_body, session_command_queue(session.id))
    assert cmd.model_dump()["plan_version"] == 1
    explicit = await api.client.post(
        f"/api/v1/sessions/{session.id}/approve", json={"plan_version": 1}
    )
    api.last_command(explicit, session_command_queue(session.id))
    wrong = await api.client.post(
        f"/api/v1/sessions/{session.id}/approve", json={"plan_version": 2}
    )
    problem(wrong, 409)
    planning = await api.session("planning", plan=True)
    problem(await api.client.post(f"/api/v1/sessions/{planning.id}/approve"), 409)


async def test_followup_needs_an_executed_session(api: Api) -> None:
    done = await api.session("completed", plan=True)
    ok = await api.client.post(f"/api/v1/sessions/{done.id}/followup", json={"prompt": "more"})
    assert api.last_command(ok, session_command_queue(done.id)).type == "command.followup"
    waiting = await api.session("awaiting_approval", plan=True)
    problem(
        await api.client.post(f"/api/v1/sessions/{waiting.id}/followup", json={"prompt": "x"}), 409
    )


# --- control commands ---------------------------------------------------------------------


async def test_steer_all_or_one_agent(api: Api) -> None:
    session = await api.session("running")
    agent = await api.agent(session)
    everyone = await api.client.post(f"/api/v1/sessions/{session.id}/steer", json={"text": "hi"})
    assert api.last_command(everyone, session_command_queue(session.id)).type == "command.steer"
    one = await api.client.post(
        f"/api/v1/sessions/{session.id}/steer", json={"text": "hi", "agent_id": agent.id}
    )
    assert (
        api.last_command(one, session_command_queue(session.id)).model_dump()["agent_id"]
        == agent.id
    )
    other = await api.session("running")
    stranger = await api.agent(other)
    problem(
        await api.client.post(
            f"/api/v1/sessions/{session.id}/steer", json={"text": "x", "agent_id": stranger.id}
        ),
        404,
    )
    planning = await api.session("planning")
    problem(await api.client.post(f"/api/v1/sessions/{planning.id}/steer", json={"text": "x"}), 409)


async def test_stop_pause_resume_state_rules(api: Api) -> None:
    running = await api.session("running")
    q = session_command_queue(running.id)
    pause = await api.client.post(f"/api/v1/sessions/{running.id}/pause")
    assert api.last_command(pause, q).type == "command.pause"
    resume_running = await api.client.post(f"/api/v1/sessions/{running.id}/resume")
    assert api.last_command(resume_running, q).type == "command.resume"
    stop = await api.client.post(f"/api/v1/sessions/{running.id}/stop")
    assert api.last_command(stop, q).type == "command.stop"

    paused = await api.session("paused")
    resume = await api.client.post(f"/api/v1/sessions/{paused.id}/resume")
    assert api.last_command(resume, session_command_queue(paused.id)).type == "command.resume"
    pause_paused = await api.client.post(f"/api/v1/sessions/{paused.id}/pause")
    assert api.last_command(pause_paused, session_command_queue(paused.id)).type == "command.pause"

    stopped = await api.session("stopped")
    problem(await api.client.post(f"/api/v1/sessions/{stopped.id}/stop"), 409)
    problem(await api.client.post("/api/v1/sessions/missing/stop"), 404)


async def test_takeover_and_release(api: Api) -> None:
    session = await api.session("running")
    working = await api.agent(session, 0, "working")
    taken = await api.agent(session, 1, "paused_by_user")
    q = session_command_queue(session.id)
    r = await api.client.post(f"/api/v1/agents/{working.id}/takeover")
    assert api.last_command(r, q).model_dump()["agent_id"] == working.id
    problem(await api.client.post(f"/api/v1/agents/{working.id}/release"), 409)
    problem(await api.client.post(f"/api/v1/agents/{taken.id}/takeover"), 409)
    r = await api.client.post(f"/api/v1/agents/{taken.id}/release")
    assert api.last_command(r, q).type == "command.release"
    problem(await api.client.post("/api/v1/agents/missing/takeover"), 404)


@pytest.mark.parametrize(
    ("started", "ended"),
    [(False, False), (True, False), (True, True)],
    ids=["initial-plan", "active-followup", "finished-followup"],
)
async def test_controls_while_awaiting_plan_approval(api: Api, started: bool, ended: bool) -> None:
    session = await api.session("awaiting_approval", plan=True, started=started, ended=ended)
    working = await api.agent(session, 0, "working")
    taken = await api.agent(session, 1, "paused_by_user")
    approval = Approval(
        id=new_id(),
        session_id=session.id,
        agent_id=working.id,
        action_summary="Submit the form",
        risk="high",
        created_at=utcnow(),
    )
    async with transaction(api.sm) as tx:
        await Repositories(tx).approvals.insert(approval)

    base = f"/api/v1/sessions/{session.id}"
    requests = [
        ("steer", f"{base}/steer", {"agent_id": working.id, "text": "keep going"}),
        ("pause", f"{base}/pause", None),
        ("resume", f"{base}/resume", None),
        ("takeover", f"/api/v1/agents/{working.id}/takeover", None),
        ("release", f"/api/v1/agents/{taken.id}/release", None),
        ("decide_approval", f"/api/v1/approvals/{approval.id}/decide", {"decision": "approve"}),
        ("stop", f"{base}/stop", None),
    ]
    for command, url, body in requests:
        response = await api.client.post(url, json=body)
        if not ended and (started or command == "stop"):
            assert api.last_command(response, session_command_queue(session.id)).type == (
                f"command.{command}"
            )
        else:
            problem(response, 409)


# --- board / approvals --------------------------------------------------------------------


async def test_board_list_filters_and_search(api: Api) -> None:
    session = await api.session("running")
    async with transaction(api.sm) as tx:
        repos = Repositories(tx)
        await repos.board.insert(
            make_board_entry(
                "fact", seq=2, session_id=session.id, title="Notion price", tags=["price"]
            )
        )
        await repos.board.insert(
            make_board_entry("question", seq=3, session_id=session.id, title="Which currency?")
        )
        await repos.board.insert(
            make_board_entry("fact", seq=4, session_id=session.id, title="Obsidian is free")
        )
    base = f"/api/v1/sessions/{session.id}/board"
    page = (await api.client.get(base, params={"kinds": "fact", "limit": 1})).json()
    assert [e["seq"] for e in page["items"]] == [2]
    assert page["next_after_seq"] == 2
    page = (await api.client.get(base, params={"kinds": "fact", "after_seq": 2})).json()
    assert [e["seq"] for e in page["items"]] == [4]
    assert page["next_after_seq"] is None
    tagged = (await api.client.get(base, params={"tags": "price,other"})).json()
    assert [e["seq"] for e in tagged["items"]] == [2]
    found = (await api.client.get(base, params={"q": "obsidian"})).json()
    assert [e["seq"] for e in found["items"]] == [4]
    problem(await api.client.get(base, params={"kinds": "fact,bogus"}), 422)


async def test_user_board_posts_and_answer_rules(api: Api) -> None:
    session = await api.session("running")
    agent = await api.agent(session)
    question = make_board_entry("question", seq=5, session_id=session.id, title="Currency?")
    async with transaction(api.sm) as tx:
        await Repositories(tx).board.insert(question)
    base = f"/api/v1/sessions/{session.id}/board"
    note = await api.client.post(
        base, json={"kind": "note", "title": "Use EUR", "to": [agent.id], "tags": ["price"]}
    )
    cmd = api.last_command(note, session_command_queue(session.id))
    assert isinstance(cmd, PostBoardCommand)
    assert cmd.entry.kind == "note"
    assert cmd.entry.to == [agent.id]
    answer = await api.client.post(
        base, json={"kind": "answer", "title": "EUR", "refs": [question.id]}
    )
    api.last_command(answer, session_command_queue(session.id))
    problem(await api.client.post(base, json={"kind": "answer", "title": "EUR"}), 422)
    problem(
        await api.client.post(base, json={"kind": "answer", "title": "x", "refs": ["nope"]}), 422
    )
    problem(await api.client.post(base, json={"kind": "note", "title": "x", "to": ["ghost"]}), 422)
    problem(await api.client.post(base, json={"kind": "fact", "title": "x"}), 422)


async def test_approvals_list_and_decide(api: Api) -> None:
    session = await api.session("running")
    agent = await api.agent(session)
    pending = Approval(
        id=new_id(),
        session_id=session.id,
        agent_id=agent.id,
        action_summary="Submit the form",
        risk="high",
        created_at=utcnow(),
    )
    decided = pending.model_copy(update={"id": new_id(), "status": "approved"})
    async with transaction(api.sm) as tx:
        await Repositories(tx).approvals.insert(pending)
        await Repositories(tx).approvals.insert(decided)
    listing = await api.client.get(
        f"/api/v1/sessions/{session.id}/approvals", params={"status": "pending"}
    )
    assert [a["id"] for a in listing.json()["items"]] == [pending.id]
    everything = await api.client.get(f"/api/v1/sessions/{session.id}/approvals")
    assert len(everything.json()["items"]) == 2
    problem(
        await api.client.get(f"/api/v1/sessions/{session.id}/approvals", params={"status": "x"}),
        422,
    )
    ok = await api.client.post(
        f"/api/v1/approvals/{pending.id}/decide", json={"decision": "deny", "note": "no"}
    )
    cmd = api.last_command(ok, session_command_queue(session.id))
    assert cmd.model_dump()["decision"] == "deny"
    problem(
        await api.client.post(
            f"/api/v1/approvals/{decided.id}/decide", json={"decision": "approve"}
        ),
        409,
    )
    problem(
        await api.client.post("/api/v1/approvals/nope/decide", json={"decision": "approve"}), 404
    )


# --- artifacts ----------------------------------------------------------------------------


async def test_upload_list_download_and_versions(api: Api) -> None:
    session = await api.session("running")
    png = solid_png(8, 8)
    files = {"file": ("C:\\fakepath\\shot é.png", io.BytesIO(png), "text/html")}
    first = await api.client.post(
        f"/api/v1/sessions/{session.id}/artifacts", files=files, data={"description": "a shot"}
    )
    assert first.status_code == 201, first.text
    artifact = first.json()
    assert artifact["name"] == "shot e.png"
    assert artifact["mime"] == "image/png"  # sniffed, not the client's text/html
    assert artifact["version"] == 1
    assert artifact["produced_by"]["type"] == "user"
    events = await api.store.read(session.id)
    published = [e for e in events if e.type == "artifact.published"]
    assert len(published) == 1
    assert published[0].causation_id is None

    again = await api.client.post(
        f"/api/v1/sessions/{session.id}/artifacts",
        files={"file": ("shot e.png", io.BytesIO(b"plain text"), "image/png")},
    )
    assert again.json()["version"] == 2
    assert again.json()["mime"] == "text/plain"

    listing = (await api.client.get(f"/api/v1/sessions/{session.id}/artifacts")).json()
    assert [a["version"] for a in listing["items"]] == [1, 2]

    download = await api.client.get(f"/api/v1/artifacts/{artifact['id']}/download")
    assert download.status_code == 200
    assert download.content == png
    assert download.headers["content-type"] == "image/png"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert "filename*=UTF-8''shot%20e.png" in download.headers["content-disposition"]
    assert download.headers["x-content-type-options"] == "nosniff"
    problem(await api.client.get("/api/v1/artifacts/missing/download"), 404)


async def test_active_content_downloads_as_octet_stream(api: Api) -> None:
    session = await api.session("running")
    html = b"<html><script>alert(1)</script></html>"
    up = await api.client.post(
        f"/api/v1/sessions/{session.id}/artifacts",
        files={"file": ("page.html", io.BytesIO(html), "text/html")},
    )
    assert up.status_code == 201
    download = await api.client.get(f"/api/v1/artifacts/{up.json()['id']}/download")
    assert download.headers["content-type"] == "application/octet-stream"
    assert "sandbox" in download.headers["content-security-policy"]


async def test_upload_limits_and_errors(api: Api) -> None:
    session = await api.session("running")
    url = f"/api/v1/sessions/{session.id}/artifacts"
    too_big = await api.client.post(
        url, files={"file": ("big.bin", io.BytesIO(b"x" * 5000), "application/octet-stream")}
    )
    problem(too_big, 413)
    huge = await api.client.post(
        url, files={"file": ("huge.bin", io.BytesIO(b"x" * (2 * 1024 * 1024)), "x/y")}
    )
    problem(huge, 413)
    problem(await api.client.post(url, data={"description": "no file"}), 422)
    problem(await api.client.post(url, json={"file": "nope"}), 422)
    problem(
        await api.client.post(
            "/api/v1/sessions/missing/artifacts", files={"file": ("a.txt", io.BytesIO(b"a"), "t/p")}
        ),
        404,
    )
    assert (await api.client.get(f"/api/v1/sessions/{session.id}/artifacts")).json()["items"] == []


# --- replay -------------------------------------------------------------------------------


async def test_events_page(api: Api) -> None:
    session = await api.session("running")
    for i in range(4):
        await api.store.append(
            session.id, EventDraft(payload=SessionPaused(reason=str(i)), actor=Actor.system())
        )
    base = f"/api/v1/sessions/{session.id}/events"
    page = (await api.client.get(base, params={"after_seq": 1, "limit": 2})).json()
    assert [e["seq"] for e in page["items"]] == [2, 3]
    assert page["next_after_seq"] == 3
    rest = (await api.client.get(base, params={"after_seq": 3, "limit": 10})).json()
    assert [e["seq"] for e in rest["items"]] == [4, 5]
    assert rest["next_after_seq"] is None
    problem(await api.client.get(base, params={"limit": 0}), 422)
    problem(await api.client.get("/api/v1/sessions/missing/events"), 404)


async def test_frames_list_filters_and_jpeg_with_cache_headers(api: Api) -> None:
    session = await api.session("running")
    agent = await api.agent(session)
    jpeg = b"\xff\xd8\xff\xe0fake-jpeg"
    frames = []
    for step in range(1, 5):
        key = f"frames/{session.id}/{agent.id}/{step}.jpg"
        await api.deps.artifacts.put(jpeg + bytes([step]), content_type="image/jpeg", key=key)
        frames.append(
            Frame(
                id=new_id(),
                session_id=session.id,
                agent_id=agent.id,
                step=step,
                ts=utcnow(),
                storage_key_small=key,
                phash="0" * 16,
            )
        )
    async with transaction(api.sm) as tx:
        for frame in frames:
            await Repositories(tx).frames.insert(frame)
    listing = await api.client.get(
        f"/api/v1/sessions/{session.id}/frames",
        params={"agent_id": agent.id, "from_step": 2, "to_step": 3},
    )
    assert [f["step"] for f in listing.json()["items"]] == [2, 3]
    problem(
        await api.client.get(
            f"/api/v1/sessions/{session.id}/frames", params={"from_step": 3, "to_step": 2}
        ),
        422,
    )
    image = await api.client.get(f"/api/v1/frames/{frames[0].id}")
    assert image.status_code == 200
    assert image.headers["content-type"] == "image/jpeg"
    assert image.headers["cache-control"] == FRAME_CACHE_CONTROL
    assert image.content == jpeg + b"\x01"
    cached = await api.client.get(
        f"/api/v1/frames/{frames[0].id}", headers={"If-None-Match": image.headers["etag"]}
    )
    assert cached.status_code == 304
    problem(await api.client.get("/api/v1/frames/missing"), 404)


async def test_ws_ticket_uses_the_sessions_table(api: Api) -> None:
    session = await api.session()
    assert (await api.client.post(f"/api/v1/sessions/{session.id}/ws-ticket")).status_code == 201
    problem(await api.client.post("/api/v1/sessions/missing/ws-ticket"), 404)


async def test_health_checks_the_database(api: Api) -> None:
    api.deps.readiness = {"database": api.deps._db_ping}
    body = (await api.client.get("/api/v1/health")).json()
    assert body == {"status": "ok", "version": body["version"], "checks": {"database": True}}
