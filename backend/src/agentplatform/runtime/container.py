"""Dependency wiring of the runtime process (T13; SPEC §5.1).

``Container`` holds the process-wide services: event store (projecting read models),
bus, leases, the session directory, artifact storage, the board, the approval gate,
model clients, sandbox providers and the policy. ``build_container(settings)`` builds
the production ones (Postgres, Redis, configured providers); tests construct a
``Container`` directly from fakes.

``SessionRuntime`` is everything one session needs, built from the container:
``AgentPool`` + ``Scheduler`` (supervised), ``Supervisor``, ``HitlController``,
``AgentControls`` (one control path) and the ``RuntimeFactory`` that builds each
agent's ``AgentRuntime`` with the full tool registry (desktop, control,
coordination, artifacts; ``type_text`` guarded by the policy), the inbox digest,
the board, the approval gate, a per-agent budget view and the approval rules.

It also runs the session's *watcher*: user uploads (``artifact.published`` by the
user) become ``artifact`` board entries, and an idle agent that is held (paused or
taken over) shows ``paused_by_user`` so the user can release it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import timedelta
from typing import Any, Protocol

import structlog

from agentplatform.artifacts.base import ArtifactStore
from agentplatform.artifacts.service import ArtifactService
from agentplatform.bus.base import EventBus, LeaseStore
from agentplatform.config import (
    ModelsConfig,
    PoliciesConfig,
    RoleConfig,
    RoleName,
    Settings,
    load_models_config,
    load_policies_config,
)
from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.common import Actor, HealthStatus, utcnow
from agentplatform.contracts.events import (
    AgentStatusChanged,
    ArtifactPublished,
    BoardEntryPosted,
    Event,
    EventEnvelope,
    SessionPaused,
    SessionResumed,
)
from agentplatform.contracts.model_io import ModelRequest, ModelResponse
from agentplatform.contracts.sandbox import SandboxSpec
from agentplatform.contracts.session import Session, Usage
from agentplatform.db.event_store import EventStore
from agentplatform.db.repositories import SessionRepository
from agentplatform.db.session import as_session
from agentplatform.eventing import EventRecorder
from agentplatform.models.base import ModelClient
from agentplatform.models.fallback_parser import parse_text_tool_calls
from agentplatform.runtime.agent.context import ModelSummarizer, RosterEntry
from agentplatform.runtime.agent.loop import (
    AgentRuntime,
    AgentRuntimeConfig,
    ControlMode,
    build_agent_registry,
)
from agentplatform.runtime.agent.tools.artifacts import register_artifact_tools
from agentplatform.runtime.agent.tools.coordination import register_coordination_tools
from agentplatform.runtime.agent.tools.registry import ToolRegistry
from agentplatform.runtime.budgets import AgentBudget, effective_budgets
from agentplatform.runtime.controls import AgentControls, ControlStore, GuardedProvider
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.runtime.coordination.digest import DigestBuilder, TaskLister
from agentplatform.runtime.coordination.prefetch import StoreArtifactPrefetch
from agentplatform.runtime.hitl import HitlApprovalGate, HitlController
from agentplatform.runtime.planner import Planner
from agentplatform.runtime.policies import Policy, approval_rules_text, guard_registry
from agentplatform.runtime.pool import AgentPool, PoolConfig, RuntimeDeps
from agentplatform.runtime.readmodels import SessionDirectory, project_read_models
from agentplatform.runtime.scheduler import Scheduler, SchedulerConfig, SessionBudget
from agentplatform.runtime.supervisor import (
    Supervisor,
    SupervisorConfig,
    supervised_scheduler_config,
)
from agentplatform.sandboxes.base import SandboxHandle, SandboxProvider

__all__ = [
    "BudgetedModelClient",
    "CommandLog",
    "Container",
    "InMemoryCommandLog",
    "ModelSource",
    "PrefetchAdapter",
    "ProfileModels",
    "RedisCommandLog",
    "RuntimeKnobs",
    "SessionRuntime",
    "build_container",
    "default_runtime_id",
    "model_env",
]

log = structlog.get_logger(__name__)

UPLOAD_TAG = "user-upload"
_HELD: frozenset[ControlMode] = frozenset({"pause", "takeover"})


def default_runtime_id() -> str:
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"


@dataclass(frozen=True, slots=True)
class RuntimeKnobs:
    """Timing and behaviour of the runtime process (defaults are production values)."""

    runtime_id: str = field(default_factory=default_runtime_id)
    session_lease_ttl_s: float = 30.0
    session_lease_renew_s: float = 10.0
    command_dequeue_timeout_s: float = 1.0
    planning_sweep_s: float = 60.0
    """Age of a ``planning`` session with no plan before the sweep plans it."""
    sweep_interval_s: float = 15.0
    orphan_sweep_s: float = 600.0
    adopt_completed_s: float = 24 * 3600.0
    scheduler: SchedulerConfig = field(default_factory=SchedulerConfig)
    pool: PoolConfig = field(default_factory=PoolConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    supervisor_enabled: bool = True
    agent_config: Mapping[str, Any] = field(default_factory=dict)
    """``AgentRuntimeConfig`` overrides (tests: fast heartbeats, no back-off)."""
    agent_sleep: Callable[[float], Awaitable[None]] | None = None
    """Injected ``sleep`` for agent runtimes (tests)."""

    @classmethod
    def from_settings(cls, settings: Settings, **overrides: Any) -> RuntimeKnobs:
        return cls(
            runtime_id=settings.app_runtime_id or default_runtime_id(),
            planning_sweep_s=settings.app_planning_sweep_s,
            orphan_sweep_s=settings.app_orphan_sweep_min * 60,
            adopt_completed_s=settings.app_adopt_completed_hours * 3600,
            **overrides,
        )


# -- ports --------------------------------------------------------------------------------


class ModelSource(Protocol):
    """Model clients per (profile, role)."""

    def client(self, profile: str, role: RoleName) -> ModelClient: ...

    def role(self, profile: str, role: RoleName) -> RoleConfig | None:
        """The role config (``None`` for test sources without one)."""
        ...


class CommandLog(Protocol):
    """Ids of commands already handled (at-least-once queues redeliver)."""

    async def seen(self, command_id: str) -> bool: ...

    async def mark(self, command_id: str) -> None: ...


class InMemoryCommandLog:
    def __init__(self) -> None:
        self.ids: set[str] = set()

    async def seen(self, command_id: str) -> bool:
        return command_id in self.ids

    async def mark(self, command_id: str) -> None:
        self.ids.add(command_id)


class RedisCommandLog:
    """``{prefix}:cmd-done:{id}`` keys with a 7-day TTL."""

    def __init__(self, redis: Any, *, prefix: str = "agentplatform", ttl_s: int = 7 * 86400):
        self._redis = redis
        self._prefix = prefix
        self._ttl_s = ttl_s

    async def seen(self, command_id: str) -> bool:
        return bool(await self._redis.exists(f"{self._prefix}:cmd-done:{command_id}"))

    async def mark(self, command_id: str) -> None:
        await self._redis.set(f"{self._prefix}:cmd-done:{command_id}", "1", ex=self._ttl_s)


OrphanCleaner = Callable[[set[str]], Awaitable[list[str]]]


@dataclass(slots=True)
class Container:
    """Process-wide services (see the module docstring)."""

    store: EventStore
    bus: EventBus
    leases: LeaseStore
    recorder: EventRecorder
    directory: SessionDirectory
    frames: ArtifactStore
    board: BoardService
    task_lister: TaskLister
    approvals: HitlApprovalGate
    control_store: ControlStore
    command_log: CommandLog
    models: ModelSource
    providers: dict[str, SandboxProvider]
    policy: Policy
    policies: PoliciesConfig
    knobs: RuntimeKnobs
    artifacts: ArtifactService | None = None
    orphan_cleaners: list[OrphanCleaner] = field(default_factory=list)
    secrets: list[str] = field(default_factory=list)
    closers: list[Callable[[], Awaitable[Any]]] = field(default_factory=list)
    planner_factory: Callable[[ModelClient], Planner] = Planner

    def provider(self, name: str) -> SandboxProvider:
        try:
            return self.providers[name]
        except KeyError:
            raise RuntimeError(f"sandbox provider {name!r} is not configured") from None

    def planner(self, session: Session) -> Planner:
        return self.planner_factory(self.models.client(session.model_profile, "planner"))

    async def aclose(self) -> None:
        for close in reversed(self.closers):
            try:
                await close()
            except Exception as exc:
                log.warning("container_close_failed", error=type(exc).__name__)


# -- model plumbing -----------------------------------------------------------------------

_KEY_FIELDS = {
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "GEMINI_API_KEY": "gemini_api_key",
    "NEBIUS_API_KEY": "nebius_api_key",
    "LOCAL_LLM_KEY": "local_llm_key",
}


def model_env(
    settings: Settings,
    environ: Mapping[str, str] | None = None,
    *,
    models: ModelsConfig | None = None,
) -> dict[str, str]:
    """``os.environ`` plus the model keys ``Settings`` read from ``.env`` (pydantic does
    not export them), so ``api_key_env`` names resolve either way: the keys Settings has
    fields for, and (with ``models``) every other variable a profile names."""
    env = dict(os.environ if environ is None else environ)
    for name, attr in _KEY_FIELDS.items():
        secret = getattr(settings, attr)
        if secret is not None and secret.get_secret_value() and not env.get(name):
            env[name] = secret.get_secret_value()
    if models is not None:
        env = settings.model_env(models, env)
    return env


class RoleLookup(Protocol):
    def role(self, profile: str, role: RoleName) -> RoleConfig | None: ...


def supervisor_frames(
    models: RoleLookup, profile: str, frames: ArtifactStore
) -> ArtifactStore | None:
    """The frame store the supervisor may attach screenshots from: only when its model
    reads images. A text-only model (e.g. GLM-5.3 on Nebius) rejects any image with
    HTTP 400, so with ``vision: false`` the supervisor works from text alone. A source
    that does not describe its roles (test fakes) keeps the frames."""
    role = models.role(profile, "supervisor")
    return frames if role is None or role.vision else None


class ProfileModels:
    """``ModelSource`` over ``models.yaml``: one wrapped client per (profile, role),
    sharing one ``SharedModelState`` (semaphores) per process."""

    def __init__(self, config: ModelsConfig, env: Mapping[str, str]) -> None:
        from agentplatform.models.shared import SharedModelState

        self._config = config
        self._env = dict(env)
        self._shared = SharedModelState()
        self._clients: dict[tuple[str, str], ModelClient] = {}

    def role(self, profile: str, role: RoleName) -> RoleConfig | None:
        return self._config.profile(profile).role(role)

    def client(self, profile: str, role: RoleName) -> ModelClient:
        from agentplatform.models.factory import build_client

        key = (profile, role)
        if key not in self._clients:
            cfg = self._config.profile(profile).role(role)
            self._clients[key] = build_client(
                cfg, self._config.prices, shared=self._shared, env=self._env
            )
        return self._clients[key]

    async def aclose(self) -> None:
        for client in self._clients.values():
            with suppress(Exception):
                await client.aclose()


class BudgetedModelClient:
    """Adds every response's usage to a ``SessionBudget`` (with ``steps=0``), so the
    supervisor's model use counts toward the session budget warning and stop."""

    def __init__(self, inner: ModelClient, budget: SessionBudget) -> None:
        self.inner = inner
        self._budget = budget

    @property
    def name(self) -> str:
        return self.inner.name

    async def complete(self, req: ModelRequest) -> ModelResponse:
        resp = await self.inner.complete(req)
        u = resp.usage
        self._budget.record(
            Usage(
                input_tokens=u.input_tokens,
                output_tokens=u.output_tokens,
                images=u.images,
                cost_usd=u.cost_usd,
            )
        )
        return resp

    async def health(self) -> HealthStatus:
        return await self.inner.health()

    async def aclose(self) -> None:
        return None  # the inner client is shared and closed by its source


class PrefetchAdapter:
    """The pool's ``ArtifactPrefetch`` (agent, sandbox, artifacts) over T15's
    ``StoreArtifactPrefetch`` (ids). Copies upstream artifacts into ``~/Shared``."""

    def __init__(self, inner: StoreArtifactPrefetch) -> None:
        self._inner = inner

    async def prefetch(
        self, agent: Agent, sandbox: SandboxHandle, artifacts: Sequence[Artifact]
    ) -> None:
        await self._inner.prefetch(
            session_id=agent.session_id,
            agent_id=agent.id,
            sandbox=sandbox,
            artifact_ids=[a.id for a in artifacts],
        )


# -- per-session wiring -------------------------------------------------------------------


class SessionRuntime:
    """Pool, scheduler, supervisor, HITL controller and controls of one session."""

    def __init__(self, c: Container, session: Session) -> None:
        self.container = c
        self.session = session
        knobs = c.knobs
        self.budget = SessionBudget(effective_budgets(session.budgets, c.policies))
        self.controls = AgentControls(session.id, c.control_store, on_change=self._sync_status)
        sched_cfg = (
            supervised_scheduler_config(knobs.scheduler)
            if knobs.supervisor_enabled
            else knobs.scheduler
        )
        pool_cfg = replace(
            knobs.pool,
            network=c.policies.network.mode,
            allowlist=tuple(c.policies.network.allowlist),
        )
        prefetch = (
            PrefetchAdapter(StoreArtifactPrefetch(c.artifacts, c.frames))
            if c.artifacts is not None
            else None
        )
        self.pool = AgentPool(
            session=session,
            provider=c.provider(session.sandbox_provider),
            factory=self._build_agent,
            events=c.recorder,
            bus=c.bus,
            leases=c.leases,
            budget=self.budget,
            lease_ttl_s=sched_cfg.lease_ttl_s,
            prefetch=prefetch,
            config=pool_cfg,
        )
        self.scheduler = Scheduler(
            session=session,
            events=c.recorder,
            store=c.store,
            bus=c.bus,
            leases=c.leases,
            pool=self.pool,
            budget=self.budget,
            config=sched_cfg,
        )
        self.supervisor: Supervisor | None = None
        if knobs.supervisor_enabled:
            self.supervisor = Supervisor(
                session=session,
                model=BudgetedModelClient(
                    c.models.client(session.model_profile, "supervisor"), self.budget
                ),
                board=c.board,
                events=c.recorder,
                store=c.store,
                bus=c.bus,
                scheduler=self.scheduler,
                control=self.controls,
                frames=supervisor_frames(c.models, session.model_profile, c.frames),
                config=replace(
                    knobs.supervisor,
                    require_human_for_plan_changes=c.policies.require_human_for_plan_changes,
                ),
            )
        self.hitl = HitlController(
            session=session,
            events=c.recorder,
            gate=c.approvals,
            scheduler=self.scheduler,
            board=c.board,
            agent_ids=lambda: self.pool.agent_ids,
        )
        self.registry = self._registry()
        self.digest = DigestBuilder(c.board, c.task_lister)
        self._extra_rules = approval_rules_text(c.policy)
        self._upload_entries: set[str] = set()
        self._log = log.bind(session_id=session.id)

    # -- agent factory --------------------------------------------------------------

    def _registry(self) -> ToolRegistry:
        registry = build_agent_registry()
        register_coordination_tools(registry)
        if self.container.artifacts is not None:
            register_artifact_tools(registry, self.container.artifacts)
        return guard_registry(registry, self.container.policy)

    def _agent_config(self, deps: RuntimeDeps) -> AgentRuntimeConfig:
        c = self.container
        overrides = {**c.knobs.agent_config, "lease_ttl_s": deps.lease_ttl_s}
        keep = c.policies.frames.keep_full_resolution
        role = c.models.role(self.session.model_profile, "agent")
        if role is None:
            return AgentRuntimeConfig(keep_full_frames=keep, **overrides)
        return AgentRuntimeConfig.from_role(role, keep_full_frames=keep, **overrides)

    def _build_agent(
        self, session: Session, agent: Agent, sandbox: SandboxHandle, deps: RuntimeDeps
    ) -> AgentRuntime:
        c = self.container
        profile = session.model_profile
        user_control = self.controls.attach(agent.id, deps.control)
        extra: dict[str, Any] = {}
        if c.knobs.agent_sleep is not None:
            extra["sleep"] = c.knobs.agent_sleep
        runtime = AgentRuntime(
            session=session,
            agent=agent,
            sandbox=sandbox,
            model=c.models.client(profile, "agent"),
            events=c.recorder,
            bus=c.bus,
            frames=c.frames,
            registry=self.registry,
            config=self._agent_config(deps),
            state=deps.state,
            control=deps.control,
            digest=self.digest,
            approvals=c.approvals,
            board=c.board,
            budget=AgentBudget(self.budget, agent.id),
            roster=_Roster(self, agent.id),
            summarizer=ModelSummarizer(c.models.client(profile, "summarizer")),
            fallback_parser=parse_text_tool_calls,
            renew_lease=deps.renew_lease,
            extra_rules=self._extra_rules,
            **extra,
        )
        self.hitl.bind_control(agent.id, user_control, runtime)
        return runtime

    # -- held agents ----------------------------------------------------------------

    async def _sync_status(self, agent_id: str, mode: ControlMode) -> None:
        """An agent outside a run shows ``paused_by_user`` while held and goes back to
        ``idle`` when released (the loop does this itself during a run)."""
        pool = self.pool
        if not pool.has(agent_id) or pool.running(agent_id) or pool.stopping:
            return
        runtime = pool.runtime(agent_id)
        if runtime is None:
            return
        status = runtime.state.status
        if mode in _HELD and status == "idle":
            reason = "taken over by the user" if mode == "takeover" else "paused"
            await pool.set_status(agent_id, "paused_by_user", reason)
        elif mode == "run" and status == "paused_by_user":
            await pool.set_status(agent_id, "idle", "released")

    # -- watcher --------------------------------------------------------------------

    async def watch(self) -> None:
        """Follow the session's events: post user uploads to the board and keep held
        agents' status right. Starts with a catch-up from the store (restarts and
        missed bus messages), then follows the bus."""
        history: list[Event] = []
        while True:
            batch = await self.container.store.read(
                self.session.id, history[-1].seq if history else 0, 5000
            )
            if not batch:
                break
            history.extend(batch)
        for event in history:
            self._note_board(event)
        for event in history:
            await self._observe(event, catch_up=True)
        async for record in self.container.bus.subscribe(self.session.id, include_ephemeral=False):
            if isinstance(record.item, EventEnvelope):
                event = record.item
                self._note_board(event)
                await self._observe(event, catch_up=False)

    def _note_board(self, event: Event) -> None:
        p = event.payload
        if isinstance(p, BoardEntryPosted) and p.entry.artifact_id is not None:
            self._upload_entries.add(p.entry.artifact_id)

    async def _observe(self, event: Event, *, catch_up: bool) -> None:
        p = event.payload
        if isinstance(p, ArtifactPublished) and event.actor.type == "user":
            await self._post_upload(p.artifact, event.actor)
        elif isinstance(p, AgentStatusChanged) and not catch_up:
            mode = self.controls.mode(p.agent_id)
            if p.status == "idle" and mode in _HELD:
                await self._sync_status(p.agent_id, mode)

    async def _post_upload(self, artifact: Artifact, actor: Actor) -> None:
        if self.session.mode == "parallel_isolated" or artifact.id in self._upload_entries:
            return
        self._upload_entries.add(artifact.id)
        size_kb = max(1, artifact.size_bytes // 1024)
        draft = BoardEntryDraft(
            kind="artifact",
            title=f"File from the user: {artifact.name}"[:120],
            body=(
                f"{artifact.description or 'Uploaded by the user.'} "
                f"({artifact.mime}, {size_kb} KB). Use fetch_artifact with id "
                f"{artifact.id} to copy it into your Shared folder."
            )[:1500],
            tags=[UPLOAD_TAG],
            artifact_id=artifact.id,
        )
        try:
            await self.container.board.post(self.session.id, draft, author=actor)
            self._log.info("user_upload_posted", artifact_id=artifact.id)
        except Exception as exc:
            self._upload_entries.discard(artifact.id)
            self._log.warning(
                "user_upload_post_failed", artifact_id=artifact.id, error=type(exc).__name__
            )


@dataclass(slots=True)
class _Roster:
    """``RosterProvider``: teammates and their current task titles (scheduler state)."""

    rt: SessionRuntime
    agent_id: str

    async def roster(self) -> Sequence[RosterEntry]:
        st = self.rt.scheduler.state
        titles = {
            t.assigned_agent_id: t.title
            for t in st.tasks.values()
            if t.assigned_agent_id is not None and t.status in ("assigned", "running")
        }
        return [
            RosterEntry(name=a.name, task_title=titles.get(a.id))
            for a in st.agents.values()
            if a.id != self.agent_id and a.status != "stopped"
        ]


# -- production container -----------------------------------------------------------------


async def project_runtime_read_models(event: Event, tx: object) -> None:
    """Keep plan review visible when an execution control changes underneath it."""
    if isinstance(event.payload, SessionPaused | SessionResumed):
        session = await SessionRepository(as_session(tx)).find(event.session_id)
        if session is not None and session.status == "awaiting_approval":
            return  # the scheduler still applies the pause/resume event to execution
    await project_read_models(event, tx)


async def build_container(settings: Settings, knobs: RuntimeKnobs | None = None) -> Container:
    """Production wiring from ``settings`` (Postgres, Redis, configured providers)."""
    from redis.asyncio import Redis

    from agentplatform.artifacts import create_artifact_store
    from agentplatform.artifacts.service import DatabaseArtifactMetadata
    from agentplatform.bus.leases import RedisLeaseStore
    from agentplatform.bus.redis_bus import RedisEventBus, redis_prefix
    from agentplatform.db.event_store import PostgresEventStore
    from agentplatform.db.session import create_engine, create_sessionmaker
    from agentplatform.runtime.controls import RedisControlStore, RedisControlTokenStore
    from agentplatform.runtime.coordination.digest import postgres_task_lister
    from agentplatform.runtime.coordination.postgres_board import PostgresBoardService
    from agentplatform.runtime.hitl import PostgresApprovalStore
    from agentplatform.runtime.readmodels import (
        PostgresSessionDirectory,
        ProjectingEventStore,
    )
    from agentplatform.sandboxes.docker_local import DockerLocalProvider
    from agentplatform.sandboxes.e2b_provider import E2BProvider

    knobs = knobs or RuntimeKnobs.from_settings(settings)
    models_cfg = load_models_config(settings.resolve_path(settings.app_models_file))
    policies = load_policies_config(settings.resolve_path(settings.app_policies_file))
    engine = create_engine(settings)
    sm = create_sessionmaker(engine)
    redis = Redis.from_url(settings.effective_redis_url, decode_responses=True)
    prefix = redis_prefix(settings.app_worktree)
    bus = RedisEventBus.from_url(settings.effective_redis_url, prefix=prefix)
    store = ProjectingEventStore(PostgresEventStore(sm), project_runtime_read_models)
    recorder = EventRecorder(store, bus)
    frames = await create_artifact_store(settings)
    board = PostgresBoardService(sm, recorder)
    # model keys named by api_key_env count too, from the environment or .env
    secrets = settings.secret_values(models_cfg)

    docker = DockerLocalProvider.from_settings(
        settings,
        RedisControlTokenStore(redis, prefix=prefix),
        mem_limit=settings.app_docker_mem_limit,
    )
    providers: dict[str, SandboxProvider] = {
        "docker_local": GuardedProvider(docker, secrets=secrets)
    }
    if settings.e2b_api_key is not None and settings.e2b_api_key.get_secret_value():
        providers["e2b"] = GuardedProvider(
            E2BProvider.from_settings(settings),
            secrets=secrets,
            max_lifetime_min=settings.app_e2b_max_lifetime_min,
        )
    models = ProfileModels(models_cfg, model_env(settings, models=models_cfg))
    policy = Policy.from_config(policies)

    approvals = HitlApprovalGate(events=recorder, store=PostgresApprovalStore(sm), board=board)

    async def close_redis() -> None:
        await redis.aclose()

    return Container(
        store=store,
        bus=bus,
        leases=RedisLeaseStore(redis, prefix=prefix),
        recorder=recorder,
        directory=PostgresSessionDirectory(sm),
        frames=frames,
        board=board,
        task_lister=postgres_task_lister(sm),
        approvals=approvals,
        control_store=RedisControlStore(redis, prefix=prefix),
        command_log=RedisCommandLog(redis, prefix=prefix),
        models=models,
        providers=providers,
        policy=policy,
        policies=policies,
        knobs=knobs,
        artifacts=ArtifactService(DatabaseArtifactMetadata(sm), recorder),
        orphan_cleaners=[docker.cleanup_orphans],
        secrets=secrets,
        closers=[engine.dispose, bus.aclose, close_redis, models.aclose, approvals.aclose],
    )


def completed_cutoff(knobs: RuntimeKnobs) -> Any:
    return utcnow() - timedelta(seconds=knobs.adopt_completed_s)


def probe_spec() -> SandboxSpec:
    """The env every sandbox gets (``AgentPool`` never sets one): checked at startup."""
    return SandboxSpec(session_id="startup-check")


async def stop_quietly(task: asyncio.Task[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError, Exception):
        await task
