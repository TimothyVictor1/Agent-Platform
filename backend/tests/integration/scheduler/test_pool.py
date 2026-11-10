"""AgentPool (SPEC §12.1, §12.5, §12.7): identities, concurrent boot with a limit,
boot retries, idle parking (pause or kill), revive and sandbox re-creation."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from agentplatform.contracts.agent import AGENT_COLORS, AGENT_NAMES
from agentplatform.contracts.sandbox import SandboxSpec
from agentplatform.errors import NotSupportedError
from agentplatform.testing import FakeSandbox, FakeSandboxProvider, make_task_draft

from .harness import FAST_POOL, FAST_SCHEDULER, World, open_session, until


async def _count(w: World, type_: str, n: int) -> bool:
    return len(await w.payloads(type_)) >= n


async def _all_paused(provider: object) -> bool:
    assert isinstance(provider, FakeSandboxProvider)
    return all(sb.paused for sb in provider.sandboxes.values())


class SlowProvider(FakeSandboxProvider):
    """Creates take a while; records the peak number of concurrent creates."""

    def __init__(self, delay_s: float = 0.05) -> None:
        super().__init__()
        self.delay_s = delay_s
        self.active = 0
        self.peak = 0

    async def create(self, spec: SandboxSpec) -> FakeSandbox:
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay_s)
            return await super().create(spec)
        finally:
            self.active -= 1


class NoPauseSandbox(FakeSandbox):
    async def pause(self) -> None:
        raise NotSupportedError("this provider cannot pause")


async def test_agents_get_deterministic_names_and_colors_and_boot_concurrently() -> None:
    provider = SlowProvider()
    w = await open_session(
        edges={"T1": []},
        agent_count=5,
        provider=provider,
        pool_config=replace(FAST_POOL, boot_concurrency=2),
    )
    try:
        await w.wait_for("session.completed")
        await w.wait_for(
            "agent.status_changed",
            lambda p: p.status == "idle" and p.previous == "booting",
        )
        await until(lambda: _count(w, "agent.sandbox_ready", 5))
    finally:
        await w.stop()
    agents = [p.agent for p in await w.payloads("agent.created")]
    assert [a.name for a in agents] == list(AGENT_NAMES[:5])
    assert [a.color for a in agents] == list(AGENT_COLORS[:5])
    assert all(a.status == "booting" for a in agents)
    assert provider.peak == 2  # concurrent, but never more than the limit
    # every sandbox spec is labelled with its session/agent and carries no env at all
    assert {s.agent_id for s in provider.specs} == {a.id for a in agents}
    assert all(s.session_id == w.session.id and s.env == {} for s in provider.specs)
    booting = await w.payloads("agent.sandbox_booting")
    assert len(booting) == 5


async def test_boot_is_retried_once_then_succeeds() -> None:
    provider = FakeSandboxProvider()
    provider.fail_creates = 1
    w = await open_session(edges={"T1": []}, agent_count=1, provider=provider)
    try:
        await w.wait_for("session.completed")
    finally:
        await w.stop()
    assert len(provider.specs) == 2
    assert await w.payloads("agent.error") == []


async def test_agent_whose_boots_keep_failing_is_given_up_and_session_fails() -> None:
    provider = FakeSandboxProvider()
    provider.fail_creates = 100
    w = await open_session(edges={"T1": []}, agent_count=1, provider=provider)
    try:
        await w.wait_for("session.failed")
    finally:
        await w.stop()
    errors = await w.payloads("agent.error")
    assert errors[0].error == "sandbox boot failed: SandboxError"
    assert errors[-1].recoverable is False
    assert "no agent" in (await w.payloads("session.failed"))[0].reason


async def test_idle_agents_are_paused_and_resumed_for_a_followup() -> None:
    first = [make_task_draft("T1")]
    w = await open_session(
        drafts=first,
        agent_count=2,
        scheduler_config=replace(FAST_SCHEDULER, idle_timeout_s=0.2),
    )
    try:
        await w.wait_for("session.completed")
        await until(lambda: _all_paused(w.provider))
        await w.approve([*first, make_task_draft("T2", depends_on=["T1"])], 2)
        await w.wait_for("task.accepted", lambda p: p.task_key == "T2")
    finally:
        await w.stop()
    sandboxes = list(w.provider.sandboxes.values())
    assert len(sandboxes) == 2  # no re-creation
    t2_agent = next(p.agent_id for p in await w.payloads("task.started") if p.task_key == "T2")
    resumed = [sb for sb in sandboxes if "resume" in sb.action_names()]
    assert len(resumed) == 1
    t2_sandbox = w.pool.sandbox(t2_agent)
    assert t2_sandbox is not None
    assert resumed[0].id == t2_sandbox.id
    # paused, never stopped: the only agent.stopped events come from the final shutdown
    stopped = [p.reason for p in await w.payloads("agent.stopped")]
    assert stopped == ["session completed"] * 2


async def test_idle_sandbox_is_killed_when_pause_is_unsupported_and_revived_later() -> None:
    provider = FakeSandboxProvider(lambda spec: NoPauseSandbox(screen_size=spec.screen))
    first = [make_task_draft("T1")]
    w = await open_session(
        drafts=first,
        agent_count=1,
        provider=provider,
        scheduler_config=replace(FAST_SCHEDULER, idle_timeout_s=0.2),
    )
    try:
        await w.wait_for("session.completed")
        stopped = await w.wait_for("agent.stopped")
        assert stopped.reason == "idle timeout"
        assert all(sb.killed for sb in provider.sandboxes.values())
        await w.approve([*first, make_task_draft("T2", depends_on=["T1"])], 2)
        await w.wait_for("task.accepted", lambda p: p.task_key == "T2")
    finally:
        await w.stop()
    assert len(provider.specs) == 2  # a new sandbox for the follow-up
    statuses = [p.status for p in await w.payloads("agent.status_changed")]
    assert statuses[:3] == ["idle", "working", "idle"]
    assert "stopped" in statuses
    assert len(await w.payloads("agent.sandbox_ready")) == 2


async def test_recovery_recreates_a_lost_sandbox() -> None:
    w = await open_session(edges={"T1": []}, agent_count=1)
    try:
        await w.wait_for("session.completed")
        agent_id = w.pool.agent_ids[0]
        sandbox = w.pool.sandbox(agent_id)
        assert sandbox is not None
        await sandbox.kill()  # the sandbox is gone (provider expired it)
        w.pool.recover(agent_id, "sandbox lost")
        await w.wait_for(
            "agent.status_changed", lambda p: (p.previous, p.status) == ("error", "idle")
        )
    finally:
        await w.stop()
    assert len(w.provider.specs) == 2
    assert len(await w.payloads("agent.sandbox_ready")) == 2


async def test_background_task_exceptions_are_logged_and_cancelled_tasks_are_quiet() -> None:
    from structlog.testing import capture_logs

    from .harness import build_world

    w = build_world()
    error = RuntimeError("boot unexpectedly failed")

    async def fail() -> None:
        raise error

    async def hang() -> None:
        await asyncio.Event().wait()

    with capture_logs() as logs:
        task = w.pool._spawn(fail(), "boot-test-agent")
        await asyncio.gather(task, return_exceptions=True)
        cancelled = w.pool._spawn(hang(), "cancel-test-agent")
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.gather(cancelled, return_exceptions=True)
    failures = [e for e in logs if e["event"] == "pool_task_failed"]
    assert len(failures) == 1
    assert failures[0]["task_name"] == "boot-test-agent"
    assert failures[0]["session_id"] == w.session.id
    assert failures[0]["exc_info"][1] is error
    assert not w.pool._background
