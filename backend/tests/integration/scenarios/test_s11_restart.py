"""Scenario 11 (SPEC §21): the runtime dies mid-session; a new runtime resumes from
the store: the interrupted tasks are re-leased with ``attempt + 1``, the sandboxes are
reconnected (none re-created, none leaked), finished work is not redone and the log
stays gap-free. (``runtime/test_runtime_recovery.py`` covers the single-agent case;
``test_resilience_restart.py`` covers five other crash points.)"""

from __future__ import annotations

from collections import Counter

from runtime.support import Behavior, payload, until

from agentplatform.contracts.events import TaskAccepted, TaskStarted
from scenarios.harness import ScenarioWorld


async def test_restart_mid_session_resumes_from_the_store(world: ScenarioWorld) -> None:
    m = world.agent_model
    m.behaviors = {
        "T1": Behavior(steps=4),
        "T2": Behavior(steps=60, latency_s=0.02),
        "T3": Behavior(steps=60, latency_s=0.02),
        "T4": Behavior(steps=4),
    }
    first = await world.runtime("rt-a")
    sid = await world.approved_session(
        {"T1": [], "T2": ["T1"], "T3": [], "T4": ["T2", "T3"]}, agent_count=2
    )
    await world.wait_event(
        sid, "task.accepted", lambda e: payload(e, TaskAccepted).task_key == "T1", limit_s=10
    )
    await until(lambda: {"T2", "T3"} <= {k for _, k, _ in m.requests})
    sandboxes = set(world.provider.sandboxes)
    creates = len(world.provider.specs)

    await first.crash()
    world.directory.expire(sid)
    m.behaviors.update({"T2": Behavior(steps=4), "T3": Behavior(steps=4)})
    second = await world.runtime("rt-b")
    assert sid in second.hosts
    await world.wait_event(sid, "session.completed", limit_s=30)

    started = Counter(
        (payload(e, TaskStarted).task_key, payload(e, TaskStarted).attempt)
        for e in world.events(sid, "task.started")
    )
    assert started == Counter(
        {("T1", 1): 1, ("T2", 1): 1, ("T2", 2): 1, ("T3", 1): 1, ("T3", 2): 1, ("T4", 1): 1}
    )
    accepted = Counter(
        payload(e, TaskAccepted).task_key for e in world.events(sid, "task.accepted")
    )
    assert accepted == Counter({"T1": 1, "T2": 1, "T3": 1, "T4": 1})
    assert set(world.provider.sandboxes) == sandboxes
    assert len(world.provider.specs) == creates
    assert not any(s.killed for s in world.provider.sandboxes.values())
    seqs = [e.seq for e in world.events(sid)]
    assert seqs == list(range(1, len(seqs) + 1))
