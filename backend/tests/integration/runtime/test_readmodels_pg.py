"""Read-model projection and the session lease over Postgres (marker ``infra``)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from agentplatform.api.routes._common import EDITABLE_PLAN, current_plan_version, require_status
from agentplatform.contracts.agent import SandboxRef
from agentplatform.contracts.artifact import Frame
from agentplatform.contracts.common import Actor, new_id, utcnow
from agentplatform.contracts.events import (
    AgentCreated,
    AgentSandboxReady,
    AgentStatusChanged,
    AgentStep,
    EventDraft,
    EventPayload,
    SessionApproved,
    SessionCompleted,
    SessionCreated,
    SessionModelUsage,
    SessionPaused,
    SessionPlanningFailed,
    SessionPlanProposed,
    SessionPlanUpdated,
    SessionResumed,
    TaskAccepted,
    TaskAssigned,
    TaskCreated,
    TaskFailed,
    TaskReady,
    TaskResultSubmitted,
    TaskStarted,
)
from agentplatform.contracts.plan import PlanProposal
from agentplatform.contracts.session import Session, Usage
from agentplatform.contracts.task import Task, TaskResult
from agentplatform.db.event_store import PostgresEventStore
from agentplatform.db.repositories import Repositories
from agentplatform.db.session import SessionMaker, transaction
from agentplatform.errors import ConflictError
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.readmodels import (
    PostgresSessionDirectory,
    ProjectingEventStore,
    project_read_models,
)
from agentplatform.testing import InMemoryBus, make_agent, make_plan, make_session, make_task

pytestmark = pytest.mark.infra


@pytest.fixture
def recorder(sessionmaker: SessionMaker) -> EventRecorder:
    store = ProjectingEventStore(PostgresEventStore(sessionmaker), project_read_models)
    return EventRecorder(store, InMemoryBus())


async def emit(rec: EventRecorder, session: Session, payload: EventPayload) -> None:
    await rec.record(session.id, EventDraft(payload=payload, actor=Actor.system()))


async def test_session_plan_task_and_agent_rows_follow_the_log(
    recorder: EventRecorder, sessionmaker: SessionMaker
) -> None:
    session = make_session(agent_count=1)
    await emit(recorder, session, SessionCreated(session=session))
    await emit(recorder, session, SessionPlanningFailed(reason="bad json"))
    plan = make_plan({"T1": []})
    await emit(recorder, session, SessionPlanProposed(plan_version=1, proposal=plan))
    async with transaction(sessionmaker) as tx:
        s = await Repositories(tx).sessions.get(session.id)
        assert s.status == "awaiting_approval"
        # the API's checks for refine/PUT plan/approve now pass
        require_status(s, EDITABLE_PLAN, "edit the plan")
        require_status(s, {"awaiting_approval"}, "approve the plan")
        assert await current_plan_version(tx, s) == 1

    await emit(recorder, session, SessionApproved(plan_version=1))
    agent = make_agent(0, session_id=session.id)
    await emit(recorder, session, AgentCreated(agent=agent))
    ref = SandboxRef(provider="docker_local", sandbox_id="sbx-1", created_at=utcnow())
    await emit(
        recorder,
        session,
        AgentSandboxReady(
            agent_id=agent.id, sandbox=ref, stream_url="http://v", interactive_stream_url="http://i"
        ),
    )
    await emit(recorder, session, AgentStatusChanged(agent_id=agent.id, status="idle"))
    task = make_task(plan.tasks[0], session_id=session.id)
    await emit(recorder, session, TaskCreated(task=task))
    ref_ = {"task_id": task.id, "task_key": task.key}
    await emit(recorder, session, TaskReady(**ref_))
    now = utcnow()
    await emit(
        recorder,
        session,
        TaskAssigned(**ref_, agent_id=agent.id, attempt=1, ack_deadline=now, lease_expires_at=now),
    )
    await emit(recorder, session, TaskStarted(**ref_, agent_id=agent.id, attempt=1))
    frame = Frame(
        id=new_id(),
        session_id=session.id,
        agent_id=agent.id,
        step=1,
        task_key=task.key,
        ts=utcnow(),
        storage_key_small=f"frames/{session.id}/{agent.id}/1.jpg",
        phash="0" * 16,
    )
    await emit(
        recorder,
        session,
        AgentStep(
            agent_id=agent.id,
            task_id=task.id,
            task_key=task.key,
            step=1,
            usage=Usage(steps=1, input_tokens=100, cost_usd=0.01),
            frame=frame,
        ),
    )
    async with transaction(sessionmaker) as tx:
        repos = Repositories(tx)
        s = await repos.sessions.get(session.id)
        assert (s.status, s.plan_version, s.usage.steps) == ("running", 1, 1)
        assert s.started_at is not None
        assert (await repos.plans.get(session.id, 1)).approved_at is not None
        a = await repos.agents.get(agent.id)
        assert (a.status, a.step, a.current_task_id) == ("idle", 1, task.id)
        assert a.sandbox == ref
        assert a.interactive_stream_url == "http://i"
        t = await repos.tasks.get(task.id)
        assert (t.status, t.attempt, t.assigned_agent_id) == ("running", 1, agent.id)
        assert await repos.frames.list(session.id) == [frame]

    # a stale failure from another agent does not touch the running task
    await emit(
        recorder,
        session,
        TaskFailed(**ref_, reason="old", attempt=1, will_retry=True, agent_id="someone-else"),
    )
    async with transaction(sessionmaker) as tx:
        assert (await Repositories(tx).tasks.get(task.id)).status == "running"
    result = TaskResult(summary="ok", outputs={"result": "Example Domain"})
    await emit(recorder, session, TaskResultSubmitted(**ref_, agent_id=agent.id, result=result))
    await emit(recorder, session, TaskAccepted(**ref_))
    await emit(recorder, session, SessionPaused())
    async with transaction(sessionmaker) as tx:
        repos = Repositories(tx)
        assert (await repos.sessions.get(session.id)).status == "paused"
        t2: Task = await repos.tasks.get(task.id)
        assert (t2.status, t2.result) == ("done", result)
        assert (await repos.agents.get(agent.id)).current_task_id is None
    await emit(recorder, session, SessionResumed())
    await emit(recorder, session, SessionCompleted())
    async with transaction(sessionmaker) as tx:
        s = await Repositories(tx).sessions.get(session.id)
        assert s.status == "completed"
        assert s.ended_at is not None

    # follow-up: proposal → awaiting approval, approval reopens the session
    await emit(recorder, session, SessionPlanProposed(plan_version=2, proposal=make_plan()))
    async with transaction(sessionmaker) as tx:
        assert (await Repositories(tx).sessions.get(session.id)).status == "awaiting_approval"
    await emit(recorder, session, SessionApproved(plan_version=2))
    async with transaction(sessionmaker) as tx:
        s = await Repositories(tx).sessions.get(session.id)
        assert (s.status, s.plan_version, s.ended_at) == ("running", 2, None)

    # a supervisor plan change keeps the session running
    await emit(
        recorder,
        session,
        SessionPlanUpdated(plan_version=3, proposal=PlanProposal(tasks=[]), source="supervisor"),
    )
    async with transaction(sessionmaker) as tx:
        s = await Repositories(tx).sessions.get(session.id)
        assert (s.status, s.plan_version) == ("running", 3)


async def test_failed_task_with_retry_returns_to_pending(
    recorder: EventRecorder, sessionmaker: SessionMaker
) -> None:
    session = make_session(agent_count=1)
    await emit(recorder, session, SessionCreated(session=session))
    agent = make_agent(0, session_id=session.id)
    await emit(recorder, session, AgentCreated(agent=agent))
    task = make_task(session_id=session.id)
    await emit(recorder, session, TaskCreated(task=task))
    ref_ = {"task_id": task.id, "task_key": task.key}
    await emit(recorder, session, TaskStarted(**ref_, agent_id=agent.id, attempt=1))
    await emit(
        recorder,
        session,
        TaskFailed(**ref_, reason="lease expired", attempt=1, will_retry=True, agent_id=agent.id),
    )
    async with transaction(sessionmaker) as tx:
        t = await Repositories(tx).tasks.get(task.id)
        assert (t.status, t.attempt, t.assigned_agent_id) == ("pending", 1, None)


async def test_planner_and_supervisor_usage_reach_the_session_row(
    recorder: EventRecorder, sessionmaker: SessionMaker
) -> None:
    """First light: the session's usage (what the API and UI show) counted agent steps
    only; ``session.model_usage`` is added too, without adding steps."""
    session = make_session(agent_count=1)
    await emit(recorder, session, SessionCreated(session=session))
    for role, cost in (("planner", 0.25), ("supervisor", 0.1)):
        await emit(
            recorder,
            session,
            SessionModelUsage(
                role=role,  # type: ignore[arg-type]
                model="m",
                usage=Usage(input_tokens=100, output_tokens=20, cost_usd=cost),
            ),
        )
    async with transaction(sessionmaker) as tx:
        s = await Repositories(tx).sessions.get(session.id)
    assert s.usage.steps == 0
    assert (s.usage.input_tokens, s.usage.output_tokens) == (200, 40)
    assert s.usage.cost_usd == pytest.approx(0.35)


async def test_session_lease_has_one_owner(
    recorder: EventRecorder, sessionmaker: SessionMaker
) -> None:
    directory = PostgresSessionDirectory(sessionmaker)
    session = make_session()
    await emit(recorder, session, SessionCreated(session=session))
    assert await directory.claim(session.id, "rt-a", 30)
    assert not await directory.claim(session.id, "rt-b", 30)
    assert await directory.claim(session.id, "rt-a", 30)  # re-claim = renew
    assert await directory.renew(session.id, "rt-a", 30)
    assert not await directory.renew(session.id, "rt-b", 30)
    await directory.release(session.id, "rt-b")  # not the owner: no effect
    assert not await directory.claim(session.id, "rt-b", 30)
    await directory.release(session.id, "rt-a")
    assert await directory.claim(session.id, "rt-b", 0.001)
    # an expired lease is free again, and its old owner cannot renew it
    expired = PostgresSessionDirectory(sessionmaker, now=lambda: utcnow() + timedelta(seconds=5))
    assert await expired.claim(session.id, "rt-c", 30)
    assert not await directory.renew(session.id, "rt-b", 30)


async def test_adoptable_and_stuck_planning(
    recorder: EventRecorder, sessionmaker: SessionMaker
) -> None:
    later = utcnow() + timedelta(minutes=5)
    directory = PostgresSessionDirectory(sessionmaker)
    stuck = make_session()
    planned = make_session()
    owned = make_session()
    for s in (stuck, planned, owned):
        await emit(recorder, s, SessionCreated(session=s))
    await emit(recorder, planned, SessionPlanningFailed(reason="x"))
    assert await directory.claim(owned.id, "rt-other", 600)

    stuck_ids = {s.id for s in await directory.stuck_planning(created_before=later)}
    assert stuck.id in stuck_ids
    assert planned.id not in stuck_ids  # it reached the planner
    assert owned.id not in stuck_ids  # someone holds it
    assert not await directory.stuck_planning(created_before=utcnow() - timedelta(minutes=5))

    adoptable = {s.id for s in await directory.adoptable("rt-me", completed_since=utcnow())}
    assert {stuck.id, planned.id} <= adoptable
    assert owned.id not in adoptable
    assert owned.id in {
        s.id for s in await directory.adoptable("rt-other", completed_since=utcnow())
    }
    alive = await directory.alive_ids(completed_since=utcnow())
    assert {stuck.id, planned.id, owned.id} <= alive


async def test_projection_failure_rolls_back_the_event(
    recorder: EventRecorder, sessionmaker: SessionMaker
) -> None:
    session = make_session()
    await emit(recorder, session, SessionCreated(session=session))
    agent = make_agent(0, session_id=session.id)
    await emit(recorder, session, AgentCreated(agent=agent))
    clash = make_agent(0, session_id=session.id)  # same name in the same session
    with pytest.raises(ConflictError):
        await emit(recorder, session, AgentCreated(agent=clash))
    async with transaction(sessionmaker) as tx:
        assert len(await Repositories(tx).agents.list(session.id)) == 1
