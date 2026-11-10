"""Test harness for the scheduler: FakeSandbox provider, a task-aware fake model,
in-memory event store/bus/leases and helpers to open sessions and wait for events.

No network, no Docker, no real model. Timeouts are scaled down (ack 0.3 s, lease
1 s, heartbeat 0.05 s, tick 0.02 s) so failure paths run in well under a second.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentplatform.bus.base import Delivery, EventBus, QueueItem
from agentplatform.contracts.agent import Agent
from agentplatform.contracts.common import Actor, HealthStatus, SessionMode, new_id
from agentplatform.contracts.events import (
    EphemeralMessage,
    Event,
    EventDraft,
    EventPayload,
    SessionApproved,
    SessionCreated,
    SessionPlanProposed,
)
from agentplatform.contracts.model_io import ModelRequest, ModelResponse, ToolSpec
from agentplatform.contracts.plan import PlanProposal
from agentplatform.contracts.session import Budgets, Session
from agentplatform.contracts.task import TaskDraft
from agentplatform.db.event_store import EventStore
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.loop import AgentRuntime, AgentRuntimeConfig, build_agent_registry
from agentplatform.runtime.agent.tools.registry import ToolResult
from agentplatform.runtime.pool import AgentPool, PoolConfig, RuntimeDeps
from agentplatform.runtime.scheduler import Scheduler, SchedulerConfig, SessionBudget
from agentplatform.sandboxes.base import SandboxHandle
from agentplatform.testing import (
    FakeSandboxProvider,
    InMemoryArtifactStore,
    InMemoryBus,
    InMemoryEventStore,
    InMemoryLeaseStore,
    make_session,
    make_tasks,
    tool_response,
)

FAST_SCHEDULER = SchedulerConfig(tick_s=0.02, ack_timeout_s=0.3, lease_ttl_s=1.0, stop_grace_s=2.0)
FAST_POOL = PoolConfig(
    fence_grace_s=0.2, recovery_backoff_s=0.01, stop_grace_s=2.0, dequeue_timeout_s=0.05
)

_AGENT_RE = re.compile(r"\bYou are ([A-Z][A-Za-z]+)")
_TASK_RE = re.compile(r"## Your task: Task (\S+)")
_STEP_RE = re.compile(r"Step (\d+) of max")


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


async def until(check: Callable[[], Awaitable[bool]], limit_s: float = 5.0) -> None:
    """Poll ``check`` every 10 ms until it is true; ``TimeoutError`` after ``limit_s``."""
    async with asyncio.timeout(limit_s):
        for _ in itertools.count():  # polling a fake: nothing to wait on
            if await check():
                return
            await asyncio.sleep(0.01)


# -- model --------------------------------------------------------------------------------


@dataclass
class Behavior:
    """How the fake model acts on one task: ``steps`` steps (``run_command``),
    then ``submit_result`` (or ``fail_task`` if ``fail``). ``hang_agents`` never
    answer (simulates a hung runtime)."""

    steps: int = 4
    fail: bool = False
    hang_agents: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ModelCall:
    agent: str
    task_key: str
    step: int
    tools: tuple[str, ...]
    system: str


class TaskModel:
    """``ModelClient`` that answers per task key (parsed from the system prompt)."""

    def __init__(self, behaviors: dict[str, Behavior] | None = None) -> None:
        self.behaviors = dict(behaviors or {})
        self.calls: list[ModelCall] = []

    @property
    def name(self) -> str:
        return "task-model"

    def tasks_run_by(self, agent: str) -> set[str]:
        return {c.task_key for c in self.calls if c.agent == agent}

    def agents_for(self, task_key: str) -> set[str]:
        return {c.agent for c in self.calls if c.task_key == task_key}

    async def complete(self, req: ModelRequest) -> ModelResponse:
        system = req.messages[0].text_content()
        agent_m, task_m = _AGENT_RE.search(system), _TASK_RE.search(system)
        assert agent_m is not None, "not an agent request"
        assert task_m is not None, "no task in the system prompt"
        agent, key = agent_m.group(1), task_m.group(1)
        users = [m for m in req.messages if m.role == "user"]
        step_m = _STEP_RE.search(users[-1].text_content())
        step = int(step_m.group(1)) if step_m else 1
        self.calls.append(
            ModelCall(agent, key, step, tuple(t.name for t in req.tools or []), system)
        )
        b = self.behaviors.get(key, Behavior())
        if agent in b.hang_agents:
            await asyncio.sleep(3600)
        if b.fail:
            return tool_response("fail_task", reason=f"{key} cannot be done")
        if step < b.steps:
            return tool_response("run_command", cmd=f"echo {key}-{step}")
        return tool_response(
            "submit_result", summary=f"{key} done", outputs={"result": f"{key} by {agent}"}
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=0)

    async def aclose(self) -> None:
        return None


# -- bus wrapper --------------------------------------------------------------------------


class DeafBus:
    """Delegates to an ``InMemoryBus`` but never delivers the queues in ``deaf``
    (an agent whose consumer is hung)."""

    def __init__(self, inner: InMemoryBus) -> None:
        self.inner = inner
        self.deaf: set[str] = set()

    async def publish(self, event: Event) -> str:
        return await self.inner.publish(event)

    async def publish_ephemeral(self, message: EphemeralMessage) -> None:
        await self.inner.publish_ephemeral(message)

    def subscribe(
        self, session_id: str, from_id: str | None = None, *, include_ephemeral: bool = True
    ) -> Any:
        return self.inner.subscribe(session_id, from_id, include_ephemeral=include_ephemeral)

    async def enqueue(self, queue: str, item: QueueItem) -> str:
        return await self.inner.enqueue(queue, item)

    async def dequeue(self, queue: str, consumer: str, *, timeout_s: float) -> Delivery | None:
        if queue in self.deaf:
            await asyncio.sleep(timeout_s)
            return None
        return await self.inner.dequeue(queue, consumer, timeout_s=timeout_s)

    async def ack(self, delivery: Delivery) -> None:
        await self.inner.ack(delivery)

    async def aclose(self) -> None:
        await self.inner.aclose()


# -- world --------------------------------------------------------------------------------


def add_fake_post_tool(registry: Any) -> None:
    async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
        return ToolResult(text="posted")

    registry.register(
        "coordination",
        ToolSpec(
            name="post",
            description="post to the board",
            parameters={"type": "object", "properties": {}, "additionalProperties": True},
        ),
        handler,
        kind="coordination",
    )


@dataclass
class World:
    session: Session
    store: EventStore
    bus: InMemoryBus
    queue_bus: EventBus
    leases: InMemoryLeaseStore
    recorder: EventRecorder
    provider: FakeSandboxProvider
    model: TaskModel
    budget: SessionBudget
    pool: AgentPool
    scheduler: Scheduler
    scheduler_config: SchedulerConfig
    pool_config: PoolConfig
    no_renew: set[str] = field(default_factory=set)
    runtimes: list[AgentRuntime] = field(default_factory=list)
    run_task: asyncio.Task[None] | None = None

    # -- events ---------------------------------------------------------------------

    async def events(self, type_: str | None = None) -> list[Event]:
        out: list[Event] = []
        after = 0
        while True:
            batch = await self.store.read(self.session.id, after, limit=5000)
            if not batch:
                break
            out.extend(batch)
            after = batch[-1].seq
        return [e for e in out if type_ is None or e.type == type_]

    async def payloads(self, type_: str) -> list[Any]:
        return [e.payload for e in await self.events(type_)]

    async def record(self, payload: EventPayload) -> Event:
        return await self.recorder.record(
            self.session.id, EventDraft(payload=payload, actor=Actor.user("local"))
        )

    async def wait_for(
        self, type_: str, pred: Callable[[Any], bool] = lambda p: True, limit_s: float = 15.0
    ) -> Any:
        """The first payload of ``type_`` matching ``pred`` (polls the store)."""
        found: list[Any] = []

        async def check() -> bool:
            found[:] = [p for p in await self.payloads(type_) if pred(p)]
            return bool(found)

        await until(check, limit_s)
        return found[0]

    # -- lifecycle ------------------------------------------------------------------

    async def approve(self, drafts: Sequence[TaskDraft], version: int) -> None:
        await self.record(
            SessionPlanProposed(
                plan_version=version, proposal=PlanProposal(tasks=list(drafts), rationale="test")
            )
        )
        await self.record(SessionApproved(plan_version=version))

    def start(self) -> None:
        self.run_task = asyncio.create_task(self.scheduler.run())

    async def stop(self) -> None:
        if self.run_task is None:
            return
        if not self.scheduler.finished:
            async with asyncio.timeout(15):
                await self.scheduler.stop("user")
        async with asyncio.timeout(15):
            await self.run_task

    async def finished(self) -> None:
        """Wait until ``Scheduler.run`` returns (session stopped or failed)."""
        assert self.run_task is not None
        async with asyncio.timeout(15):
            await self.run_task

    async def crash(self) -> None:
        """Cancel the scheduler like a runtime crash (sandboxes stay alive)."""
        assert self.run_task is not None
        self.run_task.cancel()
        await asyncio.gather(self.run_task, return_exceptions=True)


def build_world(
    *,
    session: Session | None = None,
    store: EventStore | None = None,
    bus: InMemoryBus | None = None,
    leases: InMemoryLeaseStore | None = None,
    provider: FakeSandboxProvider | None = None,
    model: TaskModel | None = None,
    scheduler_config: SchedulerConfig = FAST_SCHEDULER,
    pool_config: PoolConfig = FAST_POOL,
    deaf_agents: Sequence[str] = (),
    no_renew: Sequence[str] = (),
    pass_lease_ttl: bool = True,
    agent_ids: Sequence[str] | None = None,
) -> World:
    session = session or make_session(agent_count=3, status="awaiting_approval")
    store = store or InMemoryEventStore(strict=True)
    bus = bus or InMemoryBus()
    leases = leases or InMemoryLeaseStore()
    provider = provider or FakeSandboxProvider()
    model = model or TaskModel()
    recorder = EventRecorder(store, bus)
    queue_bus: EventBus = bus
    deaf = DeafBus(bus) if deaf_agents else None
    if deaf is not None:
        queue_bus = deaf
    budget = SessionBudget(session.budgets)
    frames = InMemoryArtifactStore()
    runtimes: list[AgentRuntime] = []
    no_renew_set = set(no_renew)

    def factory(
        session: Session, agent: Agent, sandbox: SandboxHandle, deps: RuntimeDeps
    ) -> AgentRuntime:
        if deaf is not None and agent.name in deaf_agents:
            deaf.deaf.add(f"agent:{agent.id}")
        registry = build_agent_registry()
        add_fake_post_tool(registry)
        config = AgentRuntimeConfig(error_backoff_s=0.0, heartbeat_interval_s=0.05)
        if pass_lease_ttl:
            config = AgentRuntimeConfig(
                error_backoff_s=0.0, heartbeat_interval_s=0.05, lease_ttl_s=deps.lease_ttl_s
            )
        runtime = AgentRuntime(
            session=session,
            agent=agent,
            sandbox=sandbox,
            model=model,
            events=recorder,
            bus=bus,
            frames=frames,
            registry=registry,
            config=config,
            state=deps.state,
            control=deps.control,
            budget=deps.budget,
            renew_lease=None if agent.name in no_renew_set else deps.renew_lease,
            sleep=fast_sleep,
        )
        runtimes.append(runtime)
        return runtime

    pool = AgentPool(
        session=session,
        provider=provider,
        factory=factory,
        events=recorder,
        bus=queue_bus,
        leases=leases,
        budget=budget,
        lease_ttl_s=scheduler_config.lease_ttl_s,
        config=pool_config,
        sleep=fast_sleep,
    )
    scheduler = Scheduler(
        session=session,
        events=recorder,
        store=store,
        bus=bus,
        leases=leases,
        pool=pool,
        budget=budget,
        config=scheduler_config,
    )
    return World(
        session=session,
        store=store,
        bus=bus,
        queue_bus=queue_bus,
        leases=leases,
        recorder=recorder,
        provider=provider,
        model=model,
        budget=budget,
        pool=pool,
        scheduler=scheduler,
        scheduler_config=scheduler_config,
        pool_config=pool_config,
        no_renew=no_renew_set,
        runtimes=runtimes,
    )


async def open_session(
    *,
    edges: Mapping[str, Sequence[str]] | None = None,
    drafts: Sequence[TaskDraft] | None = None,
    agent_count: int = 3,
    mode: SessionMode = "coordinated",
    budgets: Budgets | None = None,
    start: bool = True,
    **kwargs: Any,
) -> World:
    """A world whose session is created, has plan v1 approved and (by default) a
    running scheduler."""
    session = make_session(
        session_id=new_id(),
        agent_count=agent_count,
        mode=mode,
        budgets=budgets,
        status="awaiting_approval",
    )
    world = build_world(session=session, **kwargs)
    await world.record(SessionCreated(session=session))
    await world.approve(drafts if drafts is not None else make_tasks(dict(edges or {"T1": []})), 1)
    if start:
        world.start()
    return world


def task_key_of(event: Event) -> str | None:
    key = getattr(event.payload, "task_key", None)
    return key if isinstance(key, str) else None


def order_of(events: Sequence[Event], type_: str) -> list[str]:
    """Task keys of events of ``type_`` in seq order."""
    return [k for e in events if e.type == type_ and (k := task_key_of(e)) is not None]


def seq_of(events: Sequence[Event], type_: str, key: str) -> int:
    return next(e.seq for e in events if e.type == type_ and task_key_of(e) == key)
