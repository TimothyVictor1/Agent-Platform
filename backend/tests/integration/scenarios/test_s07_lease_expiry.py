"""Scenario 7 (SPEC §21): a task lease expires (the run stopped renewing it) → the
task is requeued with ``attempt + 1``, the agent recovers and the task completes
exactly once."""

from __future__ import annotations

import time

from runtime.support import Behavior, payload, until

from agentplatform.contracts.events import TaskAssigned, TaskFailed, TaskStarted
from agentplatform.testing import InMemoryLeaseStore
from scenarios.harness import ScenarioWorld


async def test_lease_expiry_requeues_with_the_next_attempt() -> None:
    skew = [0.0]
    world = ScenarioWorld(leases=InMemoryLeaseStore(clock=lambda: time.time() + skew[0]))
    try:
        world.agent_model.behaviors = {"T1": Behavior(steps=40, latency_s=0.02)}
        await world.runtime()
        sid = await world.approved_session({"T1": []})
        await world.wait_event(sid, "agent.step")
        world.agent_model.behaviors = {"T1": Behavior(steps=4)}
        skew[0] = 30.0  # every lease (TTL 1 s) is now long expired

        failed = await world.wait_event(sid, "task.failed", limit_s=10)
        await world.wait_event(sid, "session.completed", limit_s=20)

        f = payload(failed, TaskFailed)
        assert f.reason.startswith("lease_expired")
        assert (f.task_key, f.attempt, f.will_retry) == ("T1", 1, True)
        assigned = [payload(e, TaskAssigned).attempt for e in world.events(sid, "task.assigned")]
        assert assigned == [1, 2]
        started = [payload(e, TaskStarted).attempt for e in world.events(sid, "task.started")]
        assert started == [1, 2]
        assert len(world.events(sid, "task.result_submitted")) == 1
        assert len(world.events(sid, "task.accepted")) == 1
        # the agent was recovered on its sandbox (reconnected, not re-created)
        assert len(world.provider.specs) == 1
        agent_id = world.agent_ids(sid)[0]
        await until(lambda: world.agent_status(sid, agent_id) == "idle")
    finally:
        await world.close()
