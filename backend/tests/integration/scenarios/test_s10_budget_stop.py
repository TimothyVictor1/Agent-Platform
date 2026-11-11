"""Scenario 10 (SPEC §21, §12.8): the session's step budget is exceeded → warning at
80 %, ``session.budget_exceeded``, running tasks fail with reason ``budget`` (no
retry), the rest is cancelled, sandboxes are killed and the session stops cleanly."""

from __future__ import annotations

import asyncio

from runtime.support import Behavior, payload, until

from agentplatform.contracts.events import SessionBudgetWarning, SessionStopped, TaskFailed
from agentplatform.contracts.session import Budgets
from scenarios.harness import ScenarioWorld


async def test_budget_exceeded_stops_the_session_cleanly(world: ScenarioWorld) -> None:
    m = world.agent_model
    m.behaviors = {k: Behavior(steps=100, latency_s=0.01) for k in ("T1", "T2", "T3")}
    await world.runtime()
    sid = await world.approved_session(
        {"T1": [], "T2": [], "T3": ["T1", "T2"]},
        agent_count=2,
        budgets=Budgets(max_total_steps=10),
    )
    stopped = await world.wait_event(sid, "session.stopped", limit_s=20)

    assert payload(stopped, SessionStopped).reason == "budget"
    warnings = [
        payload(e, SessionBudgetWarning) for e in world.events(sid, "session.budget_warning")
    ]
    assert [w.metric for w in warnings] == ["total_steps"]
    assert len(world.events(sid, "session.budget_exceeded")) == 1
    failed = [payload(e, TaskFailed) for e in world.events(sid, "task.failed")]
    assert failed
    assert all(f.reason.startswith("budget") and not f.will_retry for f in failed)
    assert {f.task_key for f in failed} == {"T1", "T2"}
    cancelled = {getattr(e.payload, "task_key", None) for e in world.events(sid, "task.cancelled")}
    assert "T3" in cancelled
    assert not world.events(sid, "session.failed")
    assert not world.events(sid, "session.completed")

    steps = len(world.events(sid, "agent.step"))
    assert steps <= 10 + 2  # at most one in-flight step per agent past the limit
    await until(lambda: all(s.killed for s in world.provider.sandboxes.values()), limit_s=5)
    await asyncio.sleep(0.3)
    assert len(world.events(sid, "agent.step")) == steps  # nothing runs after the stop
    exceeded_seq = world.events(sid, "session.budget_exceeded")[0].seq
    assert stopped.seq > exceeded_seq
