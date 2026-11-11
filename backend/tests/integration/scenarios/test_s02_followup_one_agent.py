"""Scenario 2 (SPEC §21, regression): a follow-up with 3 idle agents is run by
exactly one agent; no agent or sandbox is re-created."""

from __future__ import annotations

from agentplatform.contracts.events import SessionPlanProposed, TaskAssigned, TaskStarted
from agentplatform.testing import make_tasks

from runtime.support import payload, until
from scenarios.harness import ScenarioWorld


async def test_followup_with_three_idle_agents_runs_on_exactly_one(world: ScenarioWorld) -> None:
    await world.runtime()
    sid = await world.approved_session({"T1": []}, agent_count=3)
    await world.wait_event(sid, "session.completed", limit_s=15)
    agents_before = world.agent_ids(sid)
    sandboxes_before = set(world.provider.sandboxes)
    assert len(agents_before) == 3

    world.models.planner.plans = [make_tasks({"T2": ["T1"]})]
    world.models.planner.calls = 0
    followup = await world.send("command.followup", sid, prompt="now do T2")
    proposed = await world.wait_event(
        sid, "session.plan_proposed", lambda e: e.causation_id == followup.id
    )
    version = payload(proposed, SessionPlanProposed).plan_version
    await world.send("command.approve_plan", sid, plan_version=version)
    await until(lambda: len(world.events(sid, "session.completed")) == 2, limit_s=15)

    assigned = [payload(e, TaskAssigned) for e in world.events(sid, "task.assigned")]
    t2_assigned = [a for a in assigned if a.task_key == "T2"]
    assert len(t2_assigned) == 1
    started = [payload(e, TaskStarted) for e in world.events(sid, "task.started")]
    assert [s.task_key for s in started].count("T2") == 1
    assert {c.agent for c in world.agent_model.of("T2")} == {
        c.agent for c in world.agent_model.of("T2")[:1]
    }  # every T2 step came from one agent
    assert world.agent_model.runs_of("T2") == 1
    assert world.agent_ids(sid) == agents_before
    assert set(world.provider.sandboxes) == sandboxes_before
