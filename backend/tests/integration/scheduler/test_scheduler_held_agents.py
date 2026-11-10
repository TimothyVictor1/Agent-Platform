"""Scheduler (T18): agents paused or taken over by the user get no new tasks until
they are released."""

from __future__ import annotations

import asyncio

import pytest

from agentplatform.contracts.events import UserTakeoverEnded, UserTakeoverStarted
from agentplatform.runtime.agent.loop import ControlMode, LocalControlState
from agentplatform.testing import make_task_draft

from .harness import open_session


@pytest.mark.parametrize("mode", ["takeover", "pause"])
async def test_held_agent_gets_no_new_task_until_released(mode: ControlMode) -> None:
    first = [make_task_draft("T1")]
    w = await open_session(drafts=first, agent_count=1)
    try:
        await w.wait_for("session.completed")
        await w.wait_for("agent.status_changed", lambda p: p.status == "idle")
        agent_id = w.pool.agent_ids[0]
        runtime = w.pool.runtime(agent_id)
        assert runtime is not None
        control = runtime.control
        assert isinstance(control, LocalControlState)
        control.set(mode)
        await w.record(UserTakeoverStarted(agent_id=agent_id))
        await w.approve([*first, make_task_draft("T2", depends_on=["T1"])], 2)
        await w.wait_for("task.ready", lambda p: p.task_key == "T2")
        await asyncio.sleep(0.5)  # many scheduler ticks
        assert [p.task_key for p in await w.payloads("task.assigned")] == ["T1"]

        control.set("run")
        await w.record(UserTakeoverEnded(agent_id=agent_id))
        await w.wait_for("task.accepted", lambda p: p.task_key == "T2")
    finally:
        await w.stop()
    assigned = await w.payloads("task.assigned")
    assert [(p.task_key, p.agent_id) for p in assigned] == [("T1", agent_id), ("T2", agent_id)]
