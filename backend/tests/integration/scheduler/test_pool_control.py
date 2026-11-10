"""AgentPool control rules used by human-in-the-loop (T18): a user pause/takeover
survives a runtime reinstall, and a user stop cancels hung runs within 4 s."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from agentplatform.contracts.sandbox import CommandResult
from agentplatform.runtime.agent.loop import ControlMode, LocalControlState
from agentplatform.runtime.pool import MAX_STOP_GRACE_S, PoolConfig
from agentplatform.testing import FakeSandbox, FakeSandboxProvider

from .harness import FAST_POOL, Behavior, TaskModel, open_session


@pytest.mark.parametrize("mode", ["takeover", "pause"])
async def test_reinstall_keeps_a_user_takeover_or_pause(mode: ControlMode) -> None:
    w = await open_session(edges={"T1": []}, agent_count=1)
    try:
        await w.wait_for("session.completed")
        agent_id = w.pool.agent_ids[0]
        control = w.runtimes[-1].control
        assert isinstance(control, LocalControlState)
        control.set(mode)
        sandbox = w.pool.sandbox(agent_id)
        assert sandbox is not None
        await sandbox.kill()
        w.pool.recover(agent_id, "sandbox lost")
        await w.wait_for(
            "agent.status_changed", lambda p: (p.previous, p.status) == ("error", "idle")
        )
        assert len(w.runtimes) == 2  # a new runtime was installed
        assert w.runtimes[-1].control is control
        assert control.mode == mode
    finally:
        await w.stop()


async def test_reinstall_still_resets_other_modes_to_run() -> None:
    w = await open_session(edges={"T1": []}, agent_count=1)
    try:
        await w.wait_for("session.completed")
        agent_id = w.pool.agent_ids[0]
        sandbox = w.pool.sandbox(agent_id)
        assert sandbox is not None
        await sandbox.kill()
        w.pool.recover(agent_id, "sandbox lost")
        await w.wait_for(
            "agent.status_changed", lambda p: (p.previous, p.status) == ("error", "idle")
        )
        assert w.runtimes[-1].control.mode == "run"
    finally:
        await w.stop()


def test_default_stop_grace_is_capped() -> None:
    assert PoolConfig().stop_grace_s <= MAX_STOP_GRACE_S <= 4.0


class HangingSandbox(FakeSandbox):
    """``run_command`` never returns and ignores the stop signal."""

    async def run_command(self, cmd: str, timeout_s: int = 60) -> CommandResult:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


async def test_user_stop_cancels_hung_runs_within_the_cap() -> None:
    provider = FakeSandboxProvider(lambda spec: HangingSandbox(screen_size=spec.screen))
    w = await open_session(
        edges={"T1": [], "T2": []},
        agent_count=2,
        provider=provider,
        model=TaskModel({"T1": Behavior(steps=5), "T2": Behavior(steps=5)}),
        pool_config=replace(FAST_POOL, stop_grace_s=30.0),  # a large configured grace
    )
    try:
        await w.wait_for("task.started", lambda p: p.task_key == "T1")
        await w.wait_for("task.started", lambda p: p.task_key == "T2")
        await asyncio.sleep(0.3)  # both runs are now inside the hung run_command
        assert w.pool.any_running()
        started = time.monotonic()
        async with asyncio.timeout(10):
            await w.scheduler.stop("user")
        elapsed = time.monotonic() - started
    finally:
        await w.stop()
    assert elapsed < 5.0
    assert not w.pool.any_running()
    assert [p.reason for p in await w.payloads("session.stopped")] == ["user"]
    assert all(sb.killed for sb in provider.sandboxes.values())
