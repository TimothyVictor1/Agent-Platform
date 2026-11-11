"""Agent control state and desktop plumbing for the runtime wiring (T13).

**One control path** (SPEC §10.7, §15.2). Three parties pause agents: the user
(takeover/release, session pause/resume), the supervisor (``pause_agent``/
``resume_agent``) and the kill switch. ``AgentControls`` keeps, per agent, a set of
*holds* and a stop flag and derives one mode from them::

    stop  >  takeover  >  pause (session_pause | supervisor_pause | user_pause)  >  run

so a supervisor resume never ends a user takeover, and a session resume never
resumes an agent the supervisor paused. The derived mode is written through to the
agent's ``LocalControlState`` (owned by the pool; the loop and the scheduler read
it) and mirrored to Redis (``ControlStore``), so a restarted runtime restores a
takeover or pause and the API can read the same state.

* ``AgentControls.adapter(agent_id)`` is the ``SettableControl`` handed to
  ``HitlController.bind_control``: ``set("takeover")`` adds the takeover hold,
  ``set("run")`` removes it, ``set("stop")`` stops the agent.
* ``AgentControls`` itself implements the supervisor's ``AgentControl`` port (it
  replaces ``PoolAgentControl``, injected by the container).
* A ``stop`` set on the local state by the pool (``stop_all``) is never overridden.

**Desktop plumbing.** ``RedisControlTokenStore`` keeps each ``docker_local``
sandbox's control token with a TTL matching the sandbox's expiry, so a restarted
runtime can ``connect()``. ``GuardedProvider`` wraps every sandbox provider: it
refuses specs that carry a configured secret, caps E2B lifetimes and tells the token
store the sandbox's lifetime.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import structlog
from redis.asyncio import Redis

from agentplatform.contracts.common import HealthStatus, SandboxProviderName
from agentplatform.contracts.sandbox import CommandResult, FileInfo, SandboxSpec, StreamUrls
from agentplatform.errors import ConflictError, NotFoundError, SandboxError
from agentplatform.runtime.agent.loop import ControlMode, LocalControlState
from agentplatform.runtime.policies import check_secret_hygiene, contains_secret
from agentplatform.sandboxes.base import (
    AgentSandboxSweeper,
    MouseButton,
    SandboxHandle,
    SandboxProvider,
    ScrollDirection,
)

__all__ = [
    "HOLDS",
    "AgentControls",
    "ControlRecord",
    "ControlStore",
    "GuardedProvider",
    "HoldReason",
    "InMemoryControlStore",
    "RedisControlStore",
    "RedisControlTokenStore",
    "SecretGuardHandle",
    "effective_mode",
]

log = structlog.get_logger(__name__)

HoldReason = Literal["takeover", "session_pause", "supervisor_pause", "user_pause"]
HOLDS: tuple[HoldReason, ...] = ("takeover", "session_pause", "supervisor_pause", "user_pause")
_PAUSES = frozenset({"session_pause", "supervisor_pause", "user_pause"})
CONTROL_TTL_S = 7 * 24 * 3600
"""Redis TTL of a session's control hash, refreshed on every write."""


@dataclass(slots=True)
class ControlRecord:
    """Persisted control state of one agent."""

    holds: set[HoldReason] = field(default_factory=set)
    stopped: bool = False

    @property
    def mode(self) -> ControlMode:
        return effective_mode(self.holds, self.stopped)

    def to_json(self) -> str:
        return json.dumps({"holds": sorted(self.holds), "stopped": self.stopped})

    @classmethod
    def from_json(cls, text: str) -> ControlRecord:
        raw = json.loads(text)
        holds: set[HoldReason] = {h for h in HOLDS if h in raw.get("holds", [])}
        return cls(holds=holds, stopped=bool(raw.get("stopped", False)))


def effective_mode(holds: Iterable[str], stopped: bool) -> ControlMode:
    if stopped:
        return "stop"
    held = set(holds)
    if "takeover" in held:
        return "takeover"
    if held & _PAUSES:
        return "pause"
    return "run"


# -- persistence --------------------------------------------------------------------------


class ControlStore(Protocol):
    """Where control records live between runtime processes."""

    async def load(self, session_id: str) -> dict[str, ControlRecord]: ...

    async def save(self, session_id: str, agent_id: str, record: ControlRecord) -> None: ...

    async def clear(self, session_id: str) -> None: ...


class InMemoryControlStore:
    """``ControlStore`` for tests and single-process runs."""

    def __init__(self) -> None:
        self.data: dict[str, dict[str, str]] = {}

    async def load(self, session_id: str) -> dict[str, ControlRecord]:
        return {a: ControlRecord.from_json(t) for a, t in self.data.get(session_id, {}).items()}

    async def save(self, session_id: str, agent_id: str, record: ControlRecord) -> None:
        self.data.setdefault(session_id, {})[agent_id] = record.to_json()

    async def clear(self, session_id: str) -> None:
        self.data.pop(session_id, None)


class RedisControlStore:
    """``ControlStore`` as one Redis hash per session: ``{prefix}:control:{session}``,
    field = agent id, value = ``{"holds": [...], "stopped": bool}``."""

    def __init__(self, redis: Redis, *, prefix: str = "agentplatform") -> None:
        self._redis = redis
        self._prefix = prefix

    def key(self, session_id: str) -> str:
        return f"{self._prefix}:control:{session_id}"

    async def load(self, session_id: str) -> dict[str, ControlRecord]:
        raw = await self._redis.hgetall(self.key(session_id))
        out: dict[str, ControlRecord] = {}
        for key, text in raw.items():
            agent_id = key.decode() if isinstance(key, bytes) else str(key)
            try:
                value = text.decode() if isinstance(text, bytes) else text
                out[agent_id] = ControlRecord.from_json(value)
            except (ValueError, TypeError, AttributeError):
                log.warning("control_record_unreadable", session_id=session_id, agent_id=agent_id)
        return out

    async def save(self, session_id: str, agent_id: str, record: ControlRecord) -> None:
        key = self.key(session_id)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hset(key, agent_id, record.to_json())
            pipe.expire(key, CONTROL_TTL_S)
            await pipe.execute()

    async def clear(self, session_id: str) -> None:
        await self._redis.delete(self.key(session_id))


# -- the control hub ----------------------------------------------------------------------

ChangeHook = Callable[[str, ControlMode], Awaitable[None]]


class _UserControl:
    """``SettableControl`` for ``HitlController.bind_control``: the user's side."""

    def __init__(self, hub: AgentControls, agent_id: str) -> None:
        self._hub = hub
        self._agent_id = agent_id

    @property
    def mode(self) -> ControlMode:
        return self._hub.mode(self._agent_id)

    async def wait_for_change(self, current: ControlMode) -> ControlMode:
        return await self._hub.local(self._agent_id).wait_for_change(current)

    def set(self, mode: ControlMode) -> None:
        hub, agent_id = self._hub, self._agent_id
        if mode == "stop":
            hub.stop(agent_id)
        elif mode == "takeover":
            hub.add_hold(agent_id, "takeover")
        elif mode == "pause":
            hub.add_hold(agent_id, "user_pause")
        else:  # run: end the user's takeover/pause; other holds stay
            hub.remove_hold(agent_id, "takeover")
            hub.remove_hold(agent_id, "user_pause")


class AgentControls:
    """The control state of one session's agents (see the module docstring).

    Synchronous changes (``add_hold``/``remove_hold``/``stop``) apply to the local
    states at once and are persisted in the background in order; ``flush`` waits for
    the writes. ``on_change`` (optional) is awaited after each persisted change of an
    agent's mode, e.g. to update the status of an idle agent.
    """

    def __init__(
        self,
        session_id: str,
        store: ControlStore,
        *,
        on_change: ChangeHook | None = None,
    ) -> None:
        self.session_id = session_id
        self._store = store
        self._on_change = on_change
        self._records: dict[str, ControlRecord] = {}
        self._locals: dict[str, LocalControlState] = {}
        self._session_paused = False
        self._lock = asyncio.Lock()
        self._writes: set[asyncio.Task[None]] = set()
        self._log = log.bind(session_id=session_id)

    # -- lifecycle ------------------------------------------------------------------

    async def restore(self) -> None:
        """Load the persisted records (call before the agents are attached)."""
        self._records.update(await self._store.load(self.session_id))
        self._session_paused = any("session_pause" in r.holds for r in self._records.values())
        if self._records:
            self._log.info(
                "control_restored",
                held={a: r.mode for a, r in self._records.items() if r.mode != "run"},
            )

    def attach(self, agent_id: str, local: LocalControlState) -> _UserControl:
        """Connect an agent's local state (from ``RuntimeDeps.control``); applies the
        current holds at once. Returns the user's ``SettableControl``."""
        record = self._records.setdefault(agent_id, ControlRecord())
        if self._session_paused:
            record.holds.add("session_pause")
        self._locals[agent_id] = local
        self._apply(agent_id)
        return self.adapter(agent_id)

    def adapter(self, agent_id: str) -> _UserControl:
        return _UserControl(self, agent_id)

    async def flush(self) -> None:
        while self._writes:
            await asyncio.gather(*list(self._writes), return_exceptions=True)

    async def clear(self) -> None:
        """Forget the persisted state (session ended)."""
        await self.flush()
        await self._store.clear(self.session_id)

    # -- queries --------------------------------------------------------------------

    def known(self, agent_id: str) -> bool:
        return agent_id in self._locals

    def local(self, agent_id: str) -> LocalControlState:
        local = self._locals.get(agent_id)
        if local is None:
            raise NotFoundError(f"agent {agent_id} does not exist or has no desktop yet")
        return local

    def mode(self, agent_id: str) -> ControlMode:
        record = self._records.get(agent_id)
        return "run" if record is None else record.mode

    def holds(self, agent_id: str) -> frozenset[HoldReason]:
        record = self._records.get(agent_id)
        return frozenset() if record is None else frozenset(record.holds)

    @property
    def session_paused(self) -> bool:
        return self._session_paused

    # -- changes --------------------------------------------------------------------

    def add_hold(self, agent_id: str, hold: HoldReason) -> None:
        record = self._records.setdefault(agent_id, ControlRecord())
        if hold not in record.holds:
            record.holds.add(hold)
            self._changed(agent_id)

    def remove_hold(self, agent_id: str, hold: HoldReason) -> None:
        record = self._records.get(agent_id)
        if record is not None and hold in record.holds:
            record.holds.discard(hold)
            self._changed(agent_id)

    def stop(self, agent_id: str) -> None:
        record = self._records.setdefault(agent_id, ControlRecord())
        if not record.stopped:
            record.stopped = True
            self._changed(agent_id)

    def pause_session(self) -> None:
        self._session_paused = True
        for agent_id in list(self._records):
            self.add_hold(agent_id, "session_pause")

    def resume_session(self) -> None:
        self._session_paused = False
        for agent_id in list(self._records):
            self.remove_hold(agent_id, "session_pause")

    # -- supervisor port (AgentControl) ---------------------------------------------

    async def pause(self, agent_id: str, reason: str) -> None:
        self._require(agent_id)
        mode = self.mode(agent_id)
        if mode in ("takeover", "stop"):
            raise ConflictError(f"the agent is in {mode} mode")
        self.add_hold(agent_id, "supervisor_pause")
        self._log.info("agent_paused_by_supervisor", agent_id=agent_id, reason=reason[:200])

    async def resume(self, agent_id: str, note: str) -> None:
        self._require(agent_id)
        if "supervisor_pause" not in self.holds(agent_id):
            raise ConflictError(
                f"the agent is not paused by the supervisor (mode {self.mode(agent_id)})"
            )
        self.remove_hold(agent_id, "supervisor_pause")
        self._log.info("agent_resumed_by_supervisor", agent_id=agent_id)

    # -- internals ------------------------------------------------------------------

    def _require(self, agent_id: str) -> None:
        if agent_id not in self._locals:
            raise NotFoundError(f"agent {agent_id} is not in this session")

    def _apply(self, agent_id: str) -> None:
        local = self._locals.get(agent_id)
        if local is None:
            return
        mode = self.mode(agent_id)
        if local.mode == "stop" and mode != "stop":
            return  # stopped by the pool (session end); never undone from here
        local.set(mode)

    def _changed(self, agent_id: str) -> None:
        self._apply(agent_id)
        task = asyncio.get_running_loop().create_task(
            self._persist(agent_id), name=f"control-save-{agent_id}"
        )
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)

    async def _persist(self, agent_id: str) -> None:
        async with self._lock:
            # the snapshot is taken under the lock: the last writer always writes
            # the latest state, whatever order the tasks were created in
            record = self._records[agent_id]
            snapshot = ControlRecord(holds=set(record.holds), stopped=record.stopped)
            try:
                await self._store.save(self.session_id, agent_id, snapshot)
            except Exception as exc:
                self._log.warning(
                    "control_persist_failed", agent_id=agent_id, error=type(exc).__name__
                )
            if self._on_change is not None:
                try:
                    await self._on_change(agent_id, snapshot.mode)
                except Exception as exc:
                    self._log.warning(
                        "control_hook_failed", agent_id=agent_id, error=type(exc).__name__
                    )


# -- desktop tokens and provider guard ----------------------------------------------------

_SANDBOX_LIFETIME_S: ContextVar[float | None] = ContextVar("sandbox_lifetime_s", default=None)
TOKEN_TTL_MARGIN_S = 15 * 60.0
DEFAULT_TOKEN_TTL_S = 24 * 3600.0 + TOKEN_TTL_MARGIN_S
"""Used when the lifetime is unknown (24 h is the longest ``SandboxSpec`` timeout)."""


class RedisControlTokenStore:
    """``docker_local.ControlTokenStore`` in Redis: ``{prefix}:sandbox-token:{id}``.

    The TTL is the sandbox's lifetime plus a margin when it is created through
    ``GuardedProvider`` (which knows ``spec.timeout_minutes``), else 24 h + margin. The
    token is a credential for the in-sandbox control server: it is never logged."""

    def __init__(self, redis: Redis, *, prefix: str = "agentplatform") -> None:
        self._redis = redis
        self._prefix = prefix

    def key(self, sandbox_id: str) -> str:
        return f"{self._prefix}:sandbox-token:{sandbox_id}"

    @staticmethod
    def ttl_s() -> float:
        lifetime = _SANDBOX_LIFETIME_S.get()
        return DEFAULT_TOKEN_TTL_S if lifetime is None else lifetime + TOKEN_TTL_MARGIN_S

    async def put(self, sandbox_id: str, token: str) -> None:
        await self._redis.set(self.key(sandbox_id), token, ex=max(1, int(self.ttl_s())))

    async def get(self, sandbox_id: str) -> str | None:
        value = await self._redis.get(self.key(sandbox_id))
        return None if value is None else str(value)

    async def delete(self, sandbox_id: str) -> None:
        await self._redis.delete(self.key(sandbox_id))


class GuardedProvider:
    """``SandboxProvider`` wrapper used for every provider the runtime builds.

    * refuses a spec whose env carries a configured secret (``ConfigError``);
    * caps ``timeout_minutes`` at ``max_lifetime_min`` (E2B), with a warning;
    * makes the sandbox lifetime known to ``RedisControlTokenStore`` during
      ``create`` (context variable), so the token expires with the sandbox.
    """

    def __init__(
        self,
        inner: SandboxProvider,
        *,
        secrets: Sequence[str] = (),
        max_lifetime_min: int | None = None,
    ) -> None:
        self.inner = inner
        self._secrets = [s for s in secrets if s]
        self._max_lifetime_min = max_lifetime_min

    @property
    def name(self) -> SandboxProviderName:
        return self.inner.name

    def capped(self, spec: SandboxSpec) -> SandboxSpec:
        cap = self._max_lifetime_min
        if cap is None or spec.timeout_minutes <= cap:
            return spec
        log.warning(
            "sandbox_lifetime_capped",
            provider=self.inner.name,
            requested_min=spec.timeout_minutes,
            cap_min=cap,
            session_id=spec.session_id,
            agent_id=spec.agent_id,
        )
        return spec.model_copy(update={"timeout_minutes": cap})

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        check_secret_hygiene(self._secrets, [spec])
        spec = self.capped(spec)
        token = _SANDBOX_LIFETIME_S.set(spec.timeout_minutes * 60.0)
        try:
            return self._guard(await self.inner.create(spec))
        finally:
            _SANDBOX_LIFETIME_S.reset(token)

    async def connect(self, sandbox_id: str) -> SandboxHandle:
        return self._guard(await self.inner.connect(sandbox_id))

    def _guard(self, handle: SandboxHandle) -> SandboxHandle:
        return SecretGuardHandle(handle, self._secrets) if self._secrets else handle

    async def health(self) -> HealthStatus:
        return await self.inner.health()

    async def remove_agent_sandboxes(
        self, session_id: str, agent_id: str, *, keep: Collection[str] = ()
    ) -> list[str]:
        """``AgentSandboxSweeper`` of the inner provider; ``[]`` if it has none."""
        if not isinstance(self.inner, AgentSandboxSweeper):
            return []
        return await self.inner.remove_agent_sandboxes(session_id, agent_id, keep=keep)


class SecretGuardHandle:
    """``SandboxHandle`` that never sends a configured secret into the sandbox
    (AGENTS.md §5: no API keys in sandbox env, files or commands). ``run_command``,
    ``type_text`` and ``write_file`` whose argument carries a secret raise
    ``SandboxError`` before anything is sent; the message never contains the secret.
    Everything else is passed through (provider-specific extras via ``__getattr__``).
    """

    def __init__(self, inner: SandboxHandle, secrets: Sequence[str]) -> None:
        self.inner = inner
        self._secrets = [s for s in secrets if s]

    def _refuse_secret(self, what: str, value: str | bytes) -> None:
        if contains_secret(value, self._secrets):
            log.warning("sandbox_secret_refused", call=what, sandbox_id=self.inner.id)
            raise SandboxError(f"refused: the {what} contains a configured secret")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    @property
    def id(self) -> str:
        return self.inner.id

    @property
    def provider(self) -> SandboxProviderName:
        return self.inner.provider

    @property
    def screen_size(self) -> tuple[int, int]:
        return self.inner.screen_size

    async def screenshot(self) -> bytes:
        return await self.inner.screenshot()

    async def click(self, x: int, y: int, button: MouseButton = "left", count: int = 1) -> None:
        await self.inner.click(x, y, button, count)

    async def move(self, x: int, y: int) -> None:
        await self.inner.move(x, y)

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        await self.inner.drag(x1, y1, x2, y2)

    async def scroll(self, x: int, y: int, direction: ScrollDirection, amount: int) -> None:
        await self.inner.scroll(x, y, direction, amount)

    async def type_text(self, text: str, interval_ms: int = 10) -> None:
        self._refuse_secret("typed text", text)
        await self.inner.type_text(text, interval_ms)

    async def key(self, combo: str) -> None:
        await self.inner.key(combo)

    async def run_command(self, cmd: str, timeout_s: int = 60) -> CommandResult:
        self._refuse_secret("command", cmd)
        return await self.inner.run_command(cmd, timeout_s)

    async def write_file(self, path: str, data: bytes) -> None:
        self._refuse_secret("file", data)
        self._refuse_secret("file path", path)
        await self.inner.write_file(path, data)

    async def read_file(self, path: str, max_bytes: int = 50_000_000) -> bytes:
        return await self.inner.read_file(path, max_bytes)

    async def list_dir(self, path: str) -> list[FileInfo]:
        return await self.inner.list_dir(path)

    async def stream_urls(self) -> StreamUrls:
        return await self.inner.stream_urls()

    async def pause(self) -> None:
        await self.inner.pause()

    async def resume(self) -> None:
        await self.inner.resume()

    async def kill(self) -> None:
        await self.inner.kill()
