"""Command handling of the runtime with fakes (T13): every command type, causation
ids, pause/resume, takeover of an idle agent, stop, duplicates, uploads, budgets."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping, Sequence
from typing import Any

import pytest
from structlog.testing import capture_logs

from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.common import Actor, new_id, utcnow
from agentplatform.contracts.events import (
    AgentStep,
    ArtifactPublished,
    EventDraft,
    SessionApproved,
    SessionBudgetExceeded,
    SessionBudgetWarning,
    SessionFailed,
    SessionModelUsage,
    SessionPlanProposed,
    SessionPlanUpdated,
    SessionStopped,
    TaskAccepted,
    TaskCreated,
    UserSteer,
)
from agentplatform.contracts.session import Budgets
from agentplatform.errors import ConfigError
from agentplatform.testing import make_tasks

from .support import Behavior, World, payload, until


@pytest.fixture
async def world() -> AsyncIterator[World]:
    w = World()
    yield w
    await w.close()


async def test_plan_approve_and_complete(world: World) -> None:
    rt = await world.runtime()
    session = await world.create_session()
    plan_cmd = await world.send("command.plan_session", session.id)
    proposed = await world.wait_event(session.id, "session.plan_proposed")
    assert proposed.causation_id == plan_cmd.id
    started = world.events(session.id, "session.planning_started")
    assert [e.causation_id for e in started] == [plan_cmd.id]
    assert session.id in rt.hosts

    approve = await world.send("command.approve_plan", session.id, plan_version=1)
    approved = await world.wait_event(session.id, "session.approved")
    assert approved.causation_id == approve.id
    await world.wait_event(session.id, "session.completed")
    accepted = world.events(session.id, "task.accepted")
    assert [e.actor.type for e in accepted] == ["supervisor"]  # supervised review
    seqs = [e.seq for e in world.events(session.id)]
    assert seqs == list(range(1, len(seqs) + 1))


async def ready_session(world: World, edges: Mapping[str, Sequence[str]], **kw: Any) -> str:
    """A session planned with ``edges`` and approved; returns its id."""
    world.models.planner.plans = [make_tasks(dict(edges))]
    session = await world.create_session(**kw)
    await world.send("command.plan_session", session.id)
    await world.wait_event(session.id, "session.plan_proposed")
    await world.send("command.approve_plan", session.id, plan_version=1)
    await world.wait_event(session.id, "session.approved")
    return session.id


async def test_planning_failure_carries_the_command_id(world: World) -> None:
    world.models.planner.fail = True
    await world.runtime()
    session = await world.create_session()
    cmd = await world.send("command.plan_session", session.id)
    failed = await world.wait_event(session.id, "session.planning_failed")
    assert failed.causation_id == cmd.id
    assert not world.events(session.id, "session.plan_proposed")
    assert world.directory.known[session.id].session.status == "planning"


async def test_refine_edit_and_stale_edit(world: World) -> None:
    world.models.planner.plans = [make_tasks({"T1": []}), make_tasks({"T1": [], "T2": ["T1"]})]
    await world.runtime()
    session = await world.create_session()
    await world.send("command.plan_session", session.id)
    await world.wait_event(session.id, "session.plan_proposed")

    refine = await world.send("command.refine_plan", session.id, instruction="add a check")
    v2 = await world.wait_event(
        session.id, "session.plan_proposed", lambda e: e.causation_id == refine.id
    )
    p2 = payload(v2, SessionPlanProposed)
    assert p2.plan_version == 2
    assert p2.diff is not None
    assert [t.key for t in p2.diff.added] == ["T2"]

    edited = make_tasks({"T1": [], "T2": ["T1"], "T3": ["T2"]})
    edit = await world.send("command.update_plan", session.id, tasks=edited, base_version=2)
    v3 = await world.wait_event(session.id, "session.plan_updated")
    p3 = payload(v3, SessionPlanUpdated)
    assert (v3.causation_id, p3.plan_version, p3.source) == (edit.id, 3, "user")

    stale = await world.send("command.update_plan", session.id, tasks=edited, base_version=2)
    await until(lambda: stale.id in world.command_log.ids)
    assert len(world.events(session.id, "session.plan_updated")) == 1  # rejected, no event

    wrong = await world.send("command.approve_plan", session.id, plan_version=2)
    await until(lambda: wrong.id in world.command_log.ids)
    assert not world.events(session.id, "session.approved")
    await world.send("command.approve_plan", session.id, plan_version=3)
    await world.wait_event(session.id, "session.completed")
    created = world.events(session.id, "task.created")
    assert {payload(e, TaskCreated).task.key for e in created} == {"T1", "T2", "T3"}


async def test_followup_extends_a_completed_session(world: World) -> None:
    world.models.planner.plans = [make_tasks({"T1": []}), make_tasks({"T2": ["T1"]})]
    await world.runtime()
    sid = await ready_session(world, {"T1": []})
    world.models.planner.plans = [make_tasks({"T2": ["T1"]})]
    world.models.planner.calls = 0
    await world.wait_event(sid, "session.completed")
    followup = await world.send("command.followup", sid, prompt="also do T2")
    proposed = await world.wait_event(
        sid, "session.plan_proposed", lambda e: e.causation_id == followup.id
    )
    assert payload(proposed, SessionPlanProposed).plan_version == 2
    await world.send("command.approve_plan", sid, plan_version=2)
    await world.wait_event(
        sid,
        "task.accepted",
        lambda e: payload(e, TaskAccepted).task_key == "T2",
    )
    await until(lambda: len(world.events(sid, "session.completed")) == 2)


async def test_followup_auto_approved_by_policy(world: World) -> None:
    world.policies = world.policies.model_copy(update={"auto_approve_followups": True})
    await world.runtime()
    sid = await ready_session(world, {"T1": []})
    await world.wait_event(sid, "session.completed")
    world.models.planner.plans = [make_tasks({"T2": ["T1"]})]
    world.models.planner.calls = 0
    followup = await world.send("command.followup", sid, prompt="also do T2")
    approved = await world.wait_event(
        sid, "session.approved", lambda e: e.causation_id == followup.id
    )
    assert payload(approved, SessionApproved).plan_version == 2


async def test_pause_holds_every_agent_until_resume(world: World) -> None:
    world.models.agent.behaviors = {"T1": Behavior(steps=8, latency_s=0.02)}
    rt = await world.runtime()
    sid = await ready_session(world, {"T1": [], "T2": ["T1"]})
    await world.wait_event(sid, "task.started")
    pause = await world.send("command.pause", sid)
    paused = await world.wait_event(sid, "session.paused")
    assert paused.causation_id == pause.id
    host = rt.hosts[sid]
    agent_id = world.agent_ids(sid)[0]
    assert host.rt.controls.mode(agent_id) == "pause"
    await until(lambda: world.agent_status(sid, agent_id) == "paused_by_user")
    steps = len(world.events(sid, "agent.step"))
    await asyncio.sleep(0.3)
    assert len(world.events(sid, "agent.step")) == steps  # nothing moves while paused
    assert world.directory.known[sid].session.status == "paused"

    resume = await world.send("command.resume", sid)
    resumed = await world.wait_event(sid, "session.resumed")
    assert resumed.causation_id == resume.id
    await world.wait_event(sid, "session.completed")


async def test_takeover_of_an_idle_agent_shows_paused_by_user(world: World) -> None:
    await world.runtime()
    sid = await ready_session(world, {"T1": []}, agent_count=2)
    await world.wait_event(sid, "session.completed")
    _a, b = world.agent_ids(sid)
    await until(lambda: world.agent_status(sid, b) == "idle")
    await world.send("command.takeover", sid, agent_id=b)
    await world.wait_event(sid, "user.takeover_started")
    await until(lambda: world.agent_status(sid, b) == "paused_by_user")
    await world.send("command.release", sid, agent_id=b)
    await world.wait_event(sid, "user.takeover_ended")
    await until(lambda: world.agent_status(sid, b) == "idle")
    assert world.control_store.data[sid][b] == '{"holds": [], "stopped": false}'


async def test_steer_board_post_and_unknown_approval(world: World) -> None:
    await world.runtime()
    sid = await ready_session(world, {"T1": []})
    await world.send("command.steer", sid, text="Use the EU pricing page")
    steer = await world.wait_event(sid, "user.steer")
    entry_id = payload(steer, UserSteer).entry_id
    assert entry_id is not None
    entry = world.board.entries[entry_id]
    assert (entry.kind, entry.author.type) == ("note", "user")

    draft = {"kind": "decision", "title": "Use euros", "body": "All prices in EUR"}
    await world.send("command.post_board", sid, entry=draft)
    await until(lambda: any(e.title == "Use euros" for e in world.board.entries.values()))

    bogus = await world.send("command.decide_approval", sid, approval_id="nope", decision="approve")
    await until(lambda: bogus.id in world.command_log.ids)  # rejected, not retried forever
    assert not world.events(sid, "approval.decided")


async def test_stop_ends_the_session_within_five_seconds(world: World) -> None:
    world.models.agent.behaviors = {"T1": Behavior(steps=500, latency_s=0.02)}
    rt = await world.runtime()
    sid = await ready_session(world, {"T1": []}, agent_count=2)
    await world.wait_event(sid, "agent.step")
    loop = asyncio.get_running_loop()
    started = loop.time()
    stop = await world.send("command.stop", sid)
    await world.wait_event(sid, "session.stopped", limit_s=5.0)
    assert loop.time() - started < 5.0
    await until(lambda: sid not in rt.hosts or rt.hosts[sid].done.is_set())
    assert world.directory.known[sid].owner is None  # lease released
    assert all(s.killed for s in world.provider.sandboxes.values())
    assert stop.id in world.command_log.ids


async def test_duplicate_delivery_is_applied_once(world: World) -> None:
    await world.runtime()
    session = await world.create_session()
    await world.send("command.plan_session", session.id)
    await world.wait_event(session.id, "session.plan_proposed")
    approve = world.command("command.approve_plan", session.id, plan_version=1)
    queue = f"session:{session.id}:commands"
    await world.bus.enqueue(queue, approve)
    await world.bus.enqueue(queue, approve)
    await world.wait_event(session.id, "session.completed")
    assert len(world.events(session.id, "session.approved")) == 1


async def test_user_upload_becomes_an_artifact_board_entry(world: World) -> None:
    await world.runtime()
    sid = await ready_session(world, {"T1": []})
    artifact = Artifact(
        id=new_id(),
        session_id=sid,
        name="brief.pdf",
        mime="application/pdf",
        size_bytes=4096,
        sha256="a" * 64,
        produced_by=Actor.user(),
        storage_key="k",
        created_at=utcnow(),
    )
    world.metadata.artifacts[artifact.id] = artifact
    await world.recorder.record(
        sid, EventDraft(payload=ArtifactPublished(artifact=artifact), actor=Actor.user())
    )
    await until(lambda: any(e.artifact_id == artifact.id for e in world.board.entries.values()))
    entry = next(e for e in world.board.entries.values() if e.artifact_id == artifact.id)
    assert (entry.kind, entry.author.type) == ("artifact", "user")
    assert "brief.pdf" in entry.title


async def test_supervisor_usage_counts_toward_the_session_budget(world: World) -> None:
    world.models.supervisor.cost_usd = 0.6
    await world.runtime()
    sid = await ready_session(world, {"T1": [], "T2": ["T1"]}, budgets=Budgets(max_cost_usd=1.0))
    warning = await world.wait_event(sid, "session.budget_warning")
    assert payload(warning, SessionBudgetWarning).metric == "cost_usd"
    exceeded = await world.wait_event(sid, "session.budget_exceeded")
    assert payload(exceeded, SessionBudgetExceeded).metric == "cost_usd"
    stopped = await world.wait_event(sid, "session.stopped")
    assert payload(stopped, SessionStopped).reason == "budget"


async def test_unhostable_new_session_fails_fast_with_a_safe_reason(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A permanent config error while hosting a new session (e.g. a model key that is
    not set) fails the session at once, with a reason free of secrets, instead of
    leaving it in ``planning`` while the command is redelivered forever."""
    secret = "tp-secret-123456789"  # gitleaks:allow

    def broken(profile: str, role: str) -> Any:
        raise ConfigError(f"environment variable TEST_PROVIDER_KEY is not set ({secret})")

    monkeypatch.setattr(world.models, "client", broken)
    rt = await world.runtime()
    rt.container.secrets.append(secret)
    session = await world.create_session()
    with capture_logs() as logs:
        await world.send("command.plan_session", session.id)
        failed = await world.wait_event(session.id, "session.failed", limit_s=5.0)
    reason = payload(failed, SessionFailed).reason
    assert "TEST_PROVIDER_KEY is not set" in reason
    assert secret not in reason
    known = world.directory.known[session.id]
    assert known.session.status == "failed"
    assert known.owner is None  # lease released
    logged = [e for e in logs if e["event"] == "new_session_failed"]
    assert logged
    assert logged[0]["reason"] == reason
    assert all(secret not in str(e) for e in logs)
    await asyncio.sleep(0.3)  # acked: not redelivered, failed once, never planned
    assert len(world.events(session.id, "session.failed")) == 1
    assert not world.events(session.id, "session.planning_started")


async def test_planner_and_supervisor_costs_reach_session_usage_and_budget(world: World) -> None:
    """First light (HIGH): session cost equalled agent steps only. Every planner and
    supervisor call is recorded as ``session.model_usage`` and counts toward usage."""
    world.models.planner.cost_usd = 0.25
    world.models.supervisor.cost_usd = 0.1
    rt = await world.runtime()
    sid = await ready_session(world, {"T1": []})
    await world.wait_event(sid, "session.completed", limit_s=10)
    usages = [payload(e, SessionModelUsage) for e in world.events(sid, "session.model_usage")]
    roles = {u.role for u in usages}
    assert {"planner", "supervisor"} <= roles
    planner = [u for u in usages if u.role == "planner"]
    assert planner[0].usage.cost_usd == pytest.approx(0.25)
    assert planner[0].model == "planner-model"
    assert all(u.usage.steps == 0 for u in usages)
    steps = [payload(e, AgentStep).usage for e in world.events(sid, "agent.step")]
    expected = sum(u.usage.cost_usd for u in usages) + sum(s.cost_usd for s in steps)
    state = rt.hosts[sid].rt.scheduler.state
    assert state.usage.cost_usd == pytest.approx(expected)
    assert state.usage.cost_usd > sum(s.cost_usd for s in steps)  # not agent steps only
    assert rt.hosts[sid].rt.budget.usage.cost_usd == pytest.approx(expected)


async def test_planner_cost_alone_can_exceed_the_cost_budget(world: World) -> None:
    """The planner's cost is in the budget before any agent step: a plan that already
    cost more than ``max_cost_usd`` stops the session once it runs."""
    world.models.planner.cost_usd = 1.5
    await world.runtime()
    sid = await ready_session(world, {"T1": []}, budgets=Budgets(max_cost_usd=1.0))
    exceeded = await world.wait_event(sid, "session.budget_exceeded")
    assert payload(exceeded, SessionBudgetExceeded).metric == "cost_usd"
    assert payload(exceeded, SessionBudgetExceeded).used >= 1.5
    stopped = await world.wait_event(sid, "session.stopped")
    assert payload(stopped, SessionStopped).reason == "budget"


async def test_model_usage_survives_a_restart_without_double_counting(world: World) -> None:
    world.models.planner.cost_usd = 0.4
    world.models.agent.behaviors = {"T1": Behavior(steps=200, latency_s=0.02)}
    first = await world.runtime("rt-a")
    sid = await ready_session(world, {"T1": []}, budgets=Budgets(max_cost_usd=5.0))
    await world.wait_event(sid, "agent.step")
    await first.crash()
    world.directory.expire(sid)
    second = await world.runtime("rt-b")
    await until(lambda: second.hosts[sid].rt.budget.usage.cost_usd > 0)
    planned = sum(
        payload(e, SessionModelUsage).usage.cost_usd
        for e in world.events(sid, "session.model_usage")
        if payload(e, SessionModelUsage).role == "planner"
    )
    assert planned == pytest.approx(0.4)
    budget = second.hosts[sid].rt.budget.usage.cost_usd
    assert planned <= budget < 2 * planned
