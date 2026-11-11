"""Scenario 3 (SPEC §21): T3 waits for T1 and T2 and receives their outputs in its
context (the "Upstream inputs" block of its system prompt)."""

from __future__ import annotations

from agentplatform.contracts.events import TaskAccepted, TaskStarted

from runtime.support import Behavior, payload
from scenarios.harness import ScenarioWorld, submit


async def test_t3_waits_for_t1_and_t2_and_sees_their_outputs(world: ScenarioWorld) -> None:
    m = world.agent_model
    m.behaviors = {"T1": Behavior(steps=8, latency_s=0.02), "T2": Behavior(steps=4)}
    m.script("T1", 8, submit("T1", result="Obsidian costs 4 USD"))
    m.script("T2", 4, submit("T2", result="Logseq is free"))
    await world.runtime()
    sid = await world.approved_session({"T1": [], "T2": [], "T3": ["T1", "T2"]}, agent_count=3)
    await world.wait_event(sid, "session.completed", limit_s=20)

    accepted = {
        payload(e, TaskAccepted).task_key: e.seq for e in world.events(sid, "task.accepted")
    }
    started = [(payload(e, TaskStarted).task_key, e.seq) for e in world.events(sid, "task.started")]
    t3_start = [seq for key, seq in started if key == "T3"]
    assert len(t3_start) == 1
    assert t3_start[0] > accepted["T1"]
    assert t3_start[0] > accepted["T2"]
    # T1 and T2 ran in parallel on different agents
    assert {c.agent for c in m.of("T1")} != {c.agent for c in m.of("T2")}

    system = m.of("T3")[0].system
    upstream = system.split("## Upstream inputs", 1)[1].split("\n## ", 1)[0]
    assert 'T1.result = "Obsidian costs 4 USD"' in upstream
    assert 'T2.result = "Logseq is free"' in upstream
    # earlier tasks had no upstream values
    assert "T1.result" not in m.of("T2")[0].system
