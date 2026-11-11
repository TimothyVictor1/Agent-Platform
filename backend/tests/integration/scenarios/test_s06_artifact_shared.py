"""Scenario 6 (SPEC §21): an artifact published by agent A is in agent B's
``~/Shared/`` before B's first step (upstream pre-fetch)."""

from __future__ import annotations

from agentplatform.contracts.events import ArtifactPublished, TaskStarted

from runtime.support import Behavior, payload
from scenarios.harness import AgentCall, ScenarioWorld, multi_tool_response, shell

REPORT = b"vendor,price\nAlder,4\n"


async def test_published_artifact_is_in_the_other_agents_shared_folder(
    world: ScenarioWorld,
) -> None:
    m = world.agent_model
    m.behaviors = {"T1": Behavior(steps=4), "T2": Behavior(steps=4), "T3": Behavior(steps=4)}
    holder: dict[str, str] = {}
    shared_at_first_step: dict[str, dict[str, bytes]] = {}

    def write_and_publish(call: AgentCall) -> object:
        sid = holder["sid"]
        sandbox = world.sandbox_of(sid, world.agent_named(sid, call.agent))
        sandbox.files["/home/agent/report.csv"] = REPORT
        return multi_tool_response(
            ("publish_artifact", {"sandbox_path": "/home/agent/report.csv"}),
            ("run_command", {"cmd": "echo published"}),
        )

    def snapshot(task: str):  # type: ignore[no-untyped-def]
        def step(call: AgentCall) -> object:
            sid = holder["sid"]
            sandbox = world.sandbox_of(sid, world.agent_named(sid, call.agent))
            shared_at_first_step[call.agent] = {
                p: d for p, d in sandbox.files.items() if p.startswith("/home/agent/Shared/")
            }
            return shell(f"{task}-1")

        return step

    m.script("T1", 2, write_and_publish)  # type: ignore[arg-type]
    m.script("T2", 1, snapshot("T2"))
    m.script("T3", 1, snapshot("T3"))
    await world.runtime()
    world.models.planner.plans = []
    sid = await _start(world, holder)
    await world.wait_event(sid, "session.completed", limit_s=20)

    published = [payload(e, ArtifactPublished) for e in world.events(sid, "artifact.published")]
    assert len(published) == 1, m.of("T1")[2].tool_results
    publisher = m.of("T1")[0].agent
    starts = {payload(e, TaskStarted).task_key for e in world.events(sid, "task.started")}
    assert {"T2", "T3"} <= starts
    others = [a for a in shared_at_first_step if a != publisher]
    assert others, "both downstream tasks ran on the publisher"
    for agent in others:
        assert shared_at_first_step[agent] == {"/home/agent/Shared/report.csv": REPORT}


async def _start(world: ScenarioWorld, holder: dict[str, str]) -> str:
    from agentplatform.testing import make_tasks

    world.models.planner.plans = [make_tasks({"T1": [], "T2": ["T1"], "T3": ["T1"]})]
    session = await world.create_session(agent_count=2)
    holder["sid"] = session.id
    await world.send("command.plan_session", session.id)
    await world.wait_event(session.id, "session.plan_proposed")
    await world.send("command.approve_plan", session.id, plan_version=1)
    return session.id
