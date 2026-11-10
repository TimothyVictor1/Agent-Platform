"""Kill switch end to end (SPEC §15.2, T18): real Scheduler + AgentPool + runtimes with
fakes. Every loop stops within 5 s, even a run hung in a sandbox command, pending
approvals expire, tasks are cancelled, sandboxes are killed, ``session.stopped``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from agentplatform.contracts.common import Actor
from agentplatform.contracts.sandbox import CommandResult, SandboxSpec
from agentplatform.runtime.agent.loop import LocalControlState
from agentplatform.runtime.hitl import HitlApprovalGate, HitlController, InMemoryApprovalStore
from agentplatform.testing import FakeSandbox, FakeSandboxProvider

from .harness import FAST_POOL, Behavior, TaskModel, open_session


class HangingSandbox(FakeSandbox):
    """``run_command`` never returns and ignores the stop signal."""

    entered = False

    async def run_command(self, cmd: str, timeout_s: int = 60) -> CommandResult:
        self.entered = True
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


async def test_kill_switch_stops_everything_within_5_seconds() -> None:
    sandboxes: list[FakeSandbox] = []

    def make(spec: SandboxSpec) -> FakeSandbox:
        # Ada's sandbox hangs in run_command; the others behave
        sb: FakeSandbox = HangingSandbox() if not sandboxes else FakeSandbox()
        sandboxes.append(sb)
        return sb

    provider = FakeSandboxProvider(make)
    model = TaskModel(
        {
            "T1": Behavior(steps=50),
            "T2": Behavior(steps=50, hang_agents=frozenset({"Brook", "Cleo"})),
            "T3": Behavior(steps=50, hang_agents=frozenset({"Brook", "Cleo"})),
        }
    )
    w = await open_session(
        edges={"T1": [], "T2": [], "T3": []},
        agent_count=3,
        provider=provider,
        model=model,
        pool_config=replace(FAST_POOL, stop_grace_s=30.0),  # capped at 4 s for stops
    )
    gate = HitlApprovalGate(events=w.recorder, store=InMemoryApprovalStore())
    ctrl = HitlController(session=w.session, events=w.recorder, gate=gate, scheduler=w.scheduler)
    try:
        for key in ("T1", "T2", "T3"):
            await w.wait_for("task.started", _for_task(key))
        assert len(w.runtimes) == 3
        for runtime in w.runtimes:
            assert isinstance(runtime.control, LocalControlState)
            ctrl.bind_control(runtime.agent.id, runtime.control, runtime)
        approval = asyncio.create_task(
            gate.request(
                session_id=w.session.id,
                agent_id=w.runtimes[0].agent.id,
                task_key="T1",
                action_summary="Send the report",
                risk="high",
                frame_id=None,
            )
        )
        await w.sleep(0.3)  # Ada is inside the hung command; the others in the model
        assert w.pool.any_running()
        hung = sandboxes[0]
        assert isinstance(hung, HangingSandbox)
        await w.until(lambda: _entered(hung))

        started = time.monotonic()
        report = await asyncio.wait_for(ctrl.stop(by=Actor.user("u1")), 10)
        elapsed = time.monotonic() - started
    finally:
        await w.stop()
        await gate.aclose()

    assert 3.5 < elapsed < 5.0  # the hung run was cancelled after the 4 s cap
    assert report.within_deadline
    assert report.approvals_expired == 1
    assert (await approval).status == "expired"
    assert not w.pool.any_running()
    assert [p.reason for p in await w.payloads("session.stopped")] == ["user"]
    cancelled = {p.task_key for p in await w.payloads("task.cancelled")}
    assert cancelled == {"T1", "T2", "T3"}
    assert all(sb.killed for sb in sandboxes)
    assert len(await w.payloads("agent.stopped")) == 3


def _for_task(key: str) -> Callable[[Any], bool]:
    return lambda p: bool(p.task_key == key)


async def _entered(sb: HangingSandbox) -> bool:
    return sb.entered
