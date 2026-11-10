"""AgentControls (one control path), the token store TTL and GuardedProvider."""

from __future__ import annotations

import pytest

from agentplatform.contracts.sandbox import SandboxSpec
from agentplatform.errors import ConfigError, ConflictError, NotFoundError
from agentplatform.runtime.agent.loop import ControlMode, LocalControlState
from agentplatform.runtime.controls import (
    DEFAULT_TOKEN_TTL_S,
    TOKEN_TTL_MARGIN_S,
    AgentControls,
    ControlRecord,
    GuardedProvider,
    InMemoryControlStore,
    RedisControlTokenStore,
    effective_mode,
)
from agentplatform.sandboxes.base import SandboxHandle
from agentplatform.testing import FakeSandboxProvider


def mode(local: LocalControlState) -> ControlMode:
    return local.mode  # defeats mypy's narrowing across state changes


def test_effective_mode_precedence() -> None:
    assert effective_mode([], False) == "run"
    assert effective_mode(["session_pause"], False) == "pause"
    assert effective_mode(["supervisor_pause", "takeover"], False) == "takeover"
    assert effective_mode(["takeover"], True) == "stop"


async def test_layered_holds_never_undo_each_other() -> None:
    store = InMemoryControlStore()
    hub = AgentControls("s1", store)
    local = LocalControlState()
    user = hub.attach("a1", local)

    await hub.pause("a1", "stuck")  # supervisor
    assert mode(local) == "pause"
    user.set("takeover")  # the user takes over a paused agent
    assert mode(local) == "takeover"
    with pytest.raises(ConflictError):
        await hub.pause("a1", "again")  # never over a takeover
    await hub.resume("a1", "go on")  # supervisor resume does not end the takeover
    assert mode(local) == "takeover"
    hub.pause_session()
    user.set("run")  # release: the session pause still holds
    assert mode(local) == "pause"
    hub.resume_session()
    assert mode(local) == "run"
    with pytest.raises(ConflictError):
        await hub.resume("a1", "not paused")
    await hub.flush()
    assert (await store.load("s1"))["a1"] == ControlRecord()


async def test_supervisor_port_rejects_unknown_agents() -> None:
    hub = AgentControls("s1", InMemoryControlStore())
    with pytest.raises(NotFoundError):
        await hub.pause("ghost", "x")


async def test_stop_wins_and_pool_stop_is_never_undone() -> None:
    hub = AgentControls("s1", InMemoryControlStore())
    local = LocalControlState()
    user = hub.attach("a1", local)
    local.set("stop")  # the pool stopped the runs (session end)
    hub.add_hold("a1", "takeover")
    assert local.mode == "stop"
    user.set("stop")
    hub.remove_hold("a1", "takeover")
    assert hub.mode("a1") == "stop"
    assert local.mode == "stop"


async def test_restore_reapplies_holds_and_session_pause() -> None:
    store = InMemoryControlStore()
    first = AgentControls("s1", store)
    first.attach("a1", LocalControlState()).set("takeover")
    first.attach("a2", LocalControlState())
    first.pause_session()
    await first.flush()

    second = AgentControls("s1", store)  # a restarted runtime
    await second.restore()
    a1, a2, a3 = LocalControlState(), LocalControlState(), LocalControlState()
    second.attach("a1", a1)
    second.attach("a2", a2)
    second.attach("a3", a3)  # created after the restart: the session is still paused
    assert (a1.mode, a2.mode, a3.mode) == ("takeover", "pause", "pause")
    assert second.session_paused
    second.resume_session()
    assert (a1.mode, a2.mode, a3.mode) == ("takeover", "run", "run")
    await second.clear()
    assert await store.load("s1") == {}


async def test_change_hook_ends_with_the_latest_mode() -> None:
    # the hook gets the state at persist time (latest wins), so it must be idempotent
    seen: list[tuple[str, ControlMode]] = []

    async def hook(agent_id: str, mode: ControlMode) -> None:
        seen.append((agent_id, mode))

    hub = AgentControls("s1", InMemoryControlStore(), on_change=hook)
    user = hub.attach("a1", LocalControlState())
    user.set("takeover")
    user.set("run")
    await hub.flush()
    assert len(seen) == 2
    assert seen[-1] == ("a1", "run")


async def test_token_ttl_follows_the_sandbox_lifetime() -> None:
    seen: list[float] = []

    class Recorder(FakeSandboxProvider):
        async def create(self, spec: SandboxSpec) -> SandboxHandle:  # type: ignore[override]
            seen.append(RedisControlTokenStore.ttl_s())
            return await super().create(spec)

    guarded = GuardedProvider(Recorder(), max_lifetime_min=60)
    await guarded.create(SandboxSpec(timeout_minutes=30))
    await guarded.create(SandboxSpec(timeout_minutes=120))  # capped at 60
    assert seen == [30 * 60 + TOKEN_TTL_MARGIN_S, 60 * 60 + TOKEN_TTL_MARGIN_S]
    assert RedisControlTokenStore.ttl_s() == DEFAULT_TOKEN_TTL_S  # outside create


def test_lifetime_cap_logs_a_warning(capsys: pytest.CaptureFixture[str]) -> None:
    guarded = GuardedProvider(FakeSandboxProvider(), max_lifetime_min=60)
    spec = SandboxSpec(timeout_minutes=240, session_id="s1")
    assert guarded.capped(spec).timeout_minutes == 60
    assert "sandbox_lifetime_capped" in capsys.readouterr().out
    assert GuardedProvider(FakeSandboxProvider()).capped(spec).timeout_minutes == 240


async def test_guard_refuses_secrets_in_sandbox_env() -> None:
    secret = "sk-live-abcdef123456"  # gitleaks:allow
    guarded = GuardedProvider(FakeSandboxProvider(), secrets=[secret])
    with pytest.raises(ConfigError):
        await guarded.create(SandboxSpec(env={"TOKEN": secret}))
    handle = await guarded.create(SandboxSpec(env={"LANG": "C"}))
    assert guarded.name == handle.provider
