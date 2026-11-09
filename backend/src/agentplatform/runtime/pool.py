"""AgentPool and AgentWorker (SPEC §12 rules 1, 4, 5, 7).

The pool owns the agents of one session: it creates them (deterministic names and
colors), boots their sandboxes concurrently with a limit, builds one
``AgentRuntime`` per agent through a ``RuntimeFactory``, runs one ``AgentWorker``
per agent, recovers agents after failures (reconnect the sandbox, else re-create
it), parks idle sandboxes (pause, else kill) and shuts everything down.

``AgentWorker`` is the agent side of addressed delivery (rule 4). It consumes only
its own queue ``agent_queue(agent_id)``; for each ``task.assigned`` it:

1. asks the scheduler (``WorkSource.work_order``) whether the assignment is still
   current and gets the task plus its resolved upstream inputs;
2. confirms it still holds the task lease (``LeaseStore.renew``): a stale or
   reassigned assignment is dropped here, because its lease owner is different;
3. emits ``task.assignment_acked``, pre-fetches upstream artifacts
   (``ArtifactPrefetch``), emits ``task.started`` and calls ``run_task``;
4. turns the ``TaskOutcome`` into ``task.result_submitted`` or ``task.failed``
   (``will_retry`` from the scheduler's retry policy) and releases the lease.
   On ``lease_lost`` it emits nothing: the task belongs to someone else now.

Event ownership (docs/agent-loop.md): the loop emits ``agent.step`` and the status
changes during a run; the pool emits ``agent.created``, ``agent.sandbox_*``, the
status changes outside a run (booting, idle, error, stopped), ``agent.error`` for
boot/recovery failures and ``agent.stopped``. The pool shares ``AgentState`` with
the runtime, so both agree on the previous status.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol, runtime_checkable

import structlog

from agentplatform.bus.base import EventBus, LeaseStore, agent_queue
from agentplatform.contracts.agent import Agent, SandboxRef, agent_identity
from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.common import Actor, AgentStatus, new_id, utcnow
from agentplatform.contracts.events import (
    AgentCreated,
    AgentError,
    AgentSandboxBooting,
    AgentSandboxReady,
    AgentStatusChanged,
    AgentStopped,
    EventDraft,
    EventPayload,
    TaskAssignedEvent,
    TaskAssignmentAcked,
    TaskFailed,
    TaskResultSubmitted,
    TaskStarted,
)
from agentplatform.contracts.sandbox import SandboxSpec
from agentplatform.contracts.session import Session
from agentplatform.contracts.task import Task
from agentplatform.errors import NotSupportedError
from agentplatform.runtime.agent.context import UpstreamInputs
from agentplatform.runtime.agent.loop import (
    AgentRuntime,
    AgentState,
    BudgetMonitor,
    EventSink,
    FailureReason,
    LeaseRenewer,
    LocalControlState,
    TaskOutcome,
)
from agentplatform.sandboxes.base import SandboxHandle, SandboxProvider

__all__ = [
    "AgentPool",
    "AgentWorker",
    "ArtifactPrefetch",
    "NoPrefetch",
    "PoolConfig",
    "RuntimeDeps",
    "RuntimeFactory",
    "WorkOrder",
    "WorkSource",
]

log = structlog.get_logger(__name__)

ParkState = Literal["paused", "killed"]


# -- interfaces ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkOrder:
    """Everything a worker needs to run one assignment."""

    task: Task
    """The task with ``status="running"``, ``attempt`` and ``assigned_agent_id`` set."""
    upstream: UpstreamInputs
    attempt: int
    lease_key: str
    lease_owner: str
    assigned_event_id: str


@runtime_checkable
class WorkSource(Protocol):
    """Implemented by the ``Scheduler``. Both calls are synchronous reads of the
    scheduler's in-process state."""

    def work_order(self, assigned: TaskAssignedEvent) -> WorkOrder | None:
        """The work order for ``assigned`` if it is still the current assignment of
        its task, else ``None`` (stale, reassigned, cancelled or session stopping)."""
        ...

    def will_retry(self, order: WorkOrder, reason: str) -> bool:
        """Retry policy for a failed attempt (SPEC §12.6)."""
        ...


@runtime_checkable
class ArtifactPrefetch(Protocol):
    """Copies upstream artifacts into an agent's sandbox before it starts a task.
    Implemented by T15; failures are logged and never block the task."""

    async def prefetch(
        self, agent: Agent, sandbox: SandboxHandle, artifacts: Sequence[Artifact]
    ) -> None: ...


class NoPrefetch:
    """Default ``ArtifactPrefetch``: does nothing (agents can ``fetch_artifact``)."""

    async def prefetch(
        self, agent: Agent, sandbox: SandboxHandle, artifacts: Sequence[Artifact]
    ) -> None:
        return None


@dataclass(frozen=True, slots=True)
class RuntimeDeps:
    """What the pool hands a ``RuntimeFactory``. The factory must pass every field
    to ``AgentRuntime`` (``control=``, ``budget=``, ``renew_lease=``, ``state=``).
    The pool enforces ``config.lease_ttl_s == lease_ttl_s`` after construction."""

    control: LocalControlState
    budget: BudgetMonitor
    renew_lease: LeaseRenewer
    lease_ttl_s: float
    state: AgentState
    """Survives sandbox re-creation (notebook, digest cursor, total steps)."""


class RuntimeFactory(Protocol):
    """Builds the ``AgentRuntime`` of one agent (T13 wires model, registry, digest,
    board, approvals; tests wire ``FakeModel``)."""

    def __call__(
        self, session: Session, agent: Agent, sandbox: SandboxHandle, deps: RuntimeDeps
    ) -> AgentRuntime: ...


@dataclass(frozen=True, slots=True)
class PoolConfig:
    boot_concurrency: int = 3
    """Max sandboxes created at the same time."""
    boot_attempts: int = 2
    """``provider.create`` attempts per boot before the boot fails."""
    max_recoveries: int = 3
    """Consecutive failed recovery attempts before an agent is given up (dead)."""
    recovery_backoff_s: float = 2.0
    fence_grace_s: float = 5.0
    """How long a fenced run may take to notice its lost lease before it is cancelled."""
    stop_grace_s: float = 30.0
    """How long a stopped run may take to exit before it is cancelled."""
    dequeue_timeout_s: float = 1.0
    screen: tuple[int, int] = (1280, 800)
    network: Literal["open", "allowlist"] = "open"
    allowlist: tuple[str, ...] = ()


# -- members ------------------------------------------------------------------------------


@dataclass(slots=True, eq=False)
class _Member:
    agent: Agent
    index: int
    control: LocalControlState
    state: AgentState
    sandbox: SandboxHandle | None = None
    runtime: AgentRuntime | None = None
    worker: asyncio.Task[None] | None = None
    order: WorkOrder | None = None
    run: asyncio.Task[TaskOutcome] | None = None
    recovering: asyncio.Task[None] | None = None
    parked: ParkState | None = None
    dead: bool = False
    stopped: bool = False
    boot_failures: int = 0
    log: structlog.typing.FilteringBoundLogger = field(default_factory=lambda: log)


class AgentPool:
    """The agents of one session. See the module docstring.

    All methods are called from the scheduler's task, except the worker loops,
    which run as their own asyncio tasks. Boot and recovery run in background
    tasks; their progress is visible only through events.
    """

    def __init__(
        self,
        *,
        session: Session,
        provider: SandboxProvider,
        factory: RuntimeFactory,
        events: EventSink,
        bus: EventBus,
        leases: LeaseStore,
        budget: BudgetMonitor,
        lease_ttl_s: float,
        prefetch: ArtifactPrefetch | None = None,
        config: PoolConfig | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.session = session
        self.config = config or PoolConfig()
        self.lease_ttl_s = lease_ttl_s
        self._provider = provider
        self._factory = factory
        self._events = events
        self._bus = bus
        self._leases = leases
        self._budget = budget
        self._prefetch: ArtifactPrefetch = prefetch or NoPrefetch()
        self._sleep = sleep
        self._source: WorkSource | None = None
        self._members: dict[str, _Member] = {}
        self._boot_limit = asyncio.Semaphore(max(1, self.config.boot_concurrency))
        self._background: set[asyncio.Task[None]] = set()
        self.stopping = False
        self._log = log.bind(session_id=session.id)

    # -- wiring ---------------------------------------------------------------------

    def attach(self, source: WorkSource) -> None:
        """Connect the scheduler (must be called before ``start``/``adopt``)."""
        self._source = source

    @property
    def source(self) -> WorkSource:
        if self._source is None:
            raise RuntimeError("AgentPool.attach(scheduler) was not called")
        return self._source

    # -- queries --------------------------------------------------------------------

    @property
    def agent_ids(self) -> list[str]:
        return list(self._members)

    def agent(self, agent_id: str) -> Agent:
        return self._members[agent_id].agent

    def runtime(self, agent_id: str) -> AgentRuntime | None:
        return self._members[agent_id].runtime

    def sandbox(self, agent_id: str) -> SandboxHandle | None:
        return self._members[agent_id].sandbox

    def parked(self, agent_id: str) -> ParkState | None:
        return self._members[agent_id].parked

    def has(self, agent_id: str) -> bool:
        return agent_id in self._members

    def available(self, agent_id: str) -> bool:
        """Can take an assignment now (from the pool's side): booted, worker alive,
        not recovering, not dead, not killed by the idle timeout, not running."""
        m = self._members.get(agent_id)
        return (
            m is not None
            and not self.stopping
            and not m.dead
            and not m.stopped
            and m.runtime is not None
            and m.recovering is None
            and m.parked != "killed"
            and m.order is None
            and m.worker is not None
            and not m.worker.done()
        )

    def revivable(self, agent_id: str) -> bool:
        """Killed by the idle timeout and can be booted again for new work."""
        m = self._members.get(agent_id)
        return (
            m is not None
            and m.parked == "killed"
            and m.recovering is None
            and not m.dead
            and not self.stopping
        )

    def recoverable(self, agent_id: str) -> bool:
        m = self._members.get(agent_id)
        return (
            m is not None
            and not m.dead
            and not m.stopped
            and m.recovering is None
            and not self.stopping
        )

    def dead(self, agent_id: str) -> bool:
        m = self._members.get(agent_id)
        return m is None or m.dead

    def running(self, agent_id: str) -> bool:
        m = self._members.get(agent_id)
        return m is not None and m.order is not None

    def any_running(self) -> bool:
        return any(m.order is not None for m in self._members.values())

    # -- lifecycle ------------------------------------------------------------------

    async def start(self, count: int) -> None:
        """Create ``count`` agents (``agent.created``) and boot their sandboxes in
        the background, at most ``boot_concurrency`` at a time."""
        start = len(self._members)
        for index in range(start, start + count):
            name, color = agent_identity(index)
            agent = Agent(id=new_id(), session_id=self.session.id, name=name, color=color)
            member = self._add(agent, index)
            await self._emit(AgentCreated(agent=agent))
            self._spawn(self._boot(member), f"boot-{agent.name}")

    async def adopt(self, agents: Sequence[Agent]) -> None:
        """Take over agents created by an earlier runtime (restart): reconnect
        their sandboxes or re-create them, in the background."""
        for agent in agents:
            if agent.id in self._members:
                continue
            index = len(self._members)
            member = self._add(agent, index)
            member.state.status = agent.status
            if agent.status == "stopped":
                # killed by the idle timeout (or stopped by the user): new work boots
                # a fresh sandbox through ``revive``
                member.parked = "killed"
                continue
            member.recovering = self._spawn(
                self._recover(member, "runtime restarted"), f"adopt-{agent.name}"
            )

    def recover(self, agent_id: str, reason: str) -> None:
        """Fence the agent's current run, then reconnect or re-create its sandbox
        and return it to ``idle`` (background, idempotent)."""
        m = self._members.get(agent_id)
        if m is None or not self.recoverable(agent_id):
            return
        m.recovering = self._spawn(self._recover(m, reason), f"recover-{m.agent.name}")

    def revive(self, agent_id: str) -> None:
        """Boot a new sandbox for an agent whose sandbox was killed when idle."""
        m = self._members.get(agent_id)
        if m is None or not self.revivable(agent_id):
            return
        m.recovering = self._spawn(
            self._recover(m, "new work after idle"), f"revive-{m.agent.name}"
        )

    async def unpark(self, agent_id: str) -> None:
        """Resume a sandbox paused by the idle timeout (before an assignment)."""
        m = self._members[agent_id]
        if m.parked == "paused" and m.sandbox is not None:
            await m.sandbox.resume()
            m.parked = None

    async def park(self, agent_id: str, reason: str = "idle timeout") -> None:
        """Pause an idle agent's sandbox, or kill it if the provider cannot pause."""
        m = self._members.get(agent_id)
        if m is None or m.parked is not None or m.sandbox is None or m.order is not None:
            return
        try:
            await m.sandbox.pause()
            m.parked = "paused"
            m.log.info("sandbox_paused", reason=reason)
        except NotSupportedError:
            with suppress(Exception):
                await m.sandbox.kill()
            m.parked = "killed"
            m.runtime = None
            await self._set_status(m, "stopped", reason)
            await self._emit(AgentStopped(agent_id=m.agent.id, reason=reason))
        except Exception as exc:
            m.log.warning("sandbox_pause_failed", error=type(exc).__name__)

    async def stop_all(self, grace_s: float | None = None) -> None:
        """Stop every run (control ``stop``) and wait for them; runs still alive
        after ``grace_s`` are cancelled. No new work is taken afterwards."""
        self.stopping = True
        for m in self._members.values():
            m.control.set("stop")
        runs = [m.run for m in self._members.values() if m.run is not None and not m.run.done()]
        if runs:
            grace = self.config.stop_grace_s if grace_s is None else grace_s
            _, pending = await asyncio.wait(runs, timeout=grace)
            for run in pending:
                run.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def close(self) -> None:
        """Runtime shutdown without ending the session: cancel workers, runs and
        background work but leave sandboxes alive so another runtime can adopt them."""
        tasks: list[asyncio.Task[None] | asyncio.Task[TaskOutcome]] = [*self._background]
        for m in self._members.values():
            tasks.extend(t for t in (m.worker, m.run) if t is not None)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def shutdown(self, reason: str) -> None:
        """Stop runs, stop workers, kill sandboxes, emit ``agent.stopped``."""
        await self.stop_all()
        for t in list(self._background):
            t.cancel()
        workers = [m.worker for m in self._members.values() if m.worker is not None]
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, *self._background, return_exceptions=True)
        for m in self._members.values():
            if m.sandbox is not None and m.parked != "killed":
                try:
                    await m.sandbox.kill()
                except Exception as exc:
                    m.log.warning("sandbox_kill_failed", error=type(exc).__name__)
            m.runtime = None
            if not m.stopped:
                m.stopped = True
                await self._set_status(m, "stopped", reason)
                await self._emit(AgentStopped(agent_id=m.agent.id, reason=reason))

    async def set_status(self, agent_id: str, status: AgentStatus, reason: str | None) -> None:
        """Change an agent's status outside a run (emits ``agent.status_changed``)."""
        await self._set_status(self._members[agent_id], status, reason)

    async def report_error(self, agent_id: str, error: str, *, recoverable: bool = True) -> None:
        await self._emit(AgentError(agent_id=agent_id, error=error, recoverable=recoverable))

    # -- internals ------------------------------------------------------------------

    def _add(self, agent: Agent, index: int) -> _Member:
        member = _Member(
            agent=agent,
            index=index,
            control=LocalControlState(),
            state=AgentState(status=agent.status),
            log=self._log.bind(agent_id=agent.id),
        )
        self._members[agent.id] = member
        return member

    def _spawn(self, coro: Awaitable[None], name: str) -> asyncio.Task[None]:
        async def runner() -> None:
            await coro

        task = asyncio.create_task(runner(), name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task

    async def _emit(self, payload: EventPayload) -> None:
        await self._events.record(
            self.session.id, EventDraft(payload=payload, actor=Actor.system())
        )

    async def _set_status(self, m: _Member, status: AgentStatus, reason: str | None) -> None:
        previous = m.state.status
        if previous == status:
            return
        m.state.status = status
        await self._emit(
            AgentStatusChanged(agent_id=m.agent.id, status=status, previous=previous, reason=reason)
        )

    async def _create_sandbox(self, m: _Member) -> SandboxHandle:
        """``provider.create`` with ``boot_attempts`` tries, under the boot limit."""
        spec = SandboxSpec(
            screen=self.config.screen,
            timeout_minutes=self.session.budgets.max_wall_minutes,
            network=self.config.network,
            allowlist=list(self.config.allowlist),
            session_id=self.session.id,
            agent_id=m.agent.id,
        )
        await self._emit(AgentSandboxBooting(agent_id=m.agent.id, provider=self._provider.name))
        last: Exception | None = None
        for attempt in range(1, max(1, self.config.boot_attempts) + 1):
            try:
                async with self._boot_limit:
                    return await self._provider.create(spec)
            except Exception as exc:
                last = exc
                m.log.warning("sandbox_create_failed", attempt=attempt, error=type(exc).__name__)
        assert last is not None
        raise last

    async def _install(self, m: _Member, sandbox: SandboxHandle, *, announce: bool) -> None:
        """Build the runtime for ``sandbox``; emit ``agent.sandbox_ready`` when new."""
        if announce:
            urls = await sandbox.stream_urls()
            ref = SandboxRef(provider=sandbox.provider, sandbox_id=sandbox.id, created_at=utcnow())
            m.agent = m.agent.model_copy(
                update={
                    "sandbox": ref,
                    "stream_url": urls.view_only_url,
                    "interactive_stream_url": urls.interactive_url,
                }
            )
            await self._emit(
                AgentSandboxReady(
                    agent_id=m.agent.id,
                    sandbox=ref,
                    stream_url=urls.view_only_url,
                    interactive_stream_url=urls.interactive_url,
                )
            )
        m.sandbox = sandbox
        m.parked = None
        m.control.set("run")
        deps = RuntimeDeps(
            control=m.control,
            budget=self._budget,
            renew_lease=self._renewer(m),
            lease_ttl_s=self.lease_ttl_s,
            state=m.state,
        )
        runtime = self._factory(self.session, m.agent, sandbox, deps)
        if runtime.config.lease_ttl_s != self.lease_ttl_s:
            runtime.config = replace(runtime.config, lease_ttl_s=self.lease_ttl_s)
        m.runtime = runtime
        if m.worker is None or m.worker.done():
            worker = AgentWorker(self, m)
            m.worker = asyncio.create_task(worker.run(), name=f"worker-{m.agent.name}")

    async def _boot(self, m: _Member) -> None:
        try:
            sandbox = await self._create_sandbox(m)
            await self._install(m, sandbox, announce=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            m.boot_failures += 1
            await self.report_error(m.agent.id, f"sandbox boot failed: {type(exc).__name__}")
            await self._set_status(m, "error", "sandbox boot failed")
            return
        m.boot_failures = 0
        await self._set_status(m, "idle", "sandbox ready")

    def _renewer(self, m: _Member) -> LeaseRenewer:
        async def renew() -> bool:
            order = m.order
            if order is None:
                return False
            return await self._leases.renew(order.lease_key, order.lease_owner, self.lease_ttl_s)

        return renew

    async def _fence(self, m: _Member, grace_s: float) -> None:
        """Wait for the current run to end (it notices the lost lease within one
        heartbeat); cancel it after ``grace_s``."""
        run = m.run
        if run is None or run.done():
            return
        _, pending = await asyncio.wait({run}, timeout=grace_s)
        if pending:
            m.log.warning("run_fenced")
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
        # let the worker finish its bookkeeping for the fenced order
        for _ in range(100):
            if m.order is None:
                break
            await asyncio.sleep(0.01)

    async def _recover(self, m: _Member, reason: str) -> None:
        try:
            await self._fence(m, self.config.fence_grace_s)
            if m.order is None and m.worker is not None and not m.worker.done():
                # a fresh queue consumer, in case the old one is hung (ack timeout)
                m.worker.cancel()
                await asyncio.gather(m.worker, return_exceptions=True)
                m.worker = None
            if m.parked != "killed":
                await self._set_status(m, "error", reason)
            for attempt in range(1, max(1, self.config.max_recoveries) + 1):
                if self.stopping:
                    return
                try:
                    sandbox, new = await self._reconnect_or_create(m)
                    await self._install(m, sandbox, announce=new)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    m.log.warning("recovery_failed", attempt=attempt, error=type(exc).__name__)
                    await self.report_error(
                        m.agent.id, f"recovery failed: {type(exc).__name__}", recoverable=True
                    )
                    await self._sleep(self.config.recovery_backoff_s)
                    continue
                await self._set_status(m, "idle", "recovered")
                return
            m.dead = True
            await self.report_error(
                m.agent.id, "agent given up after failed recoveries", recoverable=False
            )
            await self._set_status(m, "error", "recovery failed")
        finally:
            m.recovering = None

    async def _reconnect_or_create(self, m: _Member) -> tuple[SandboxHandle, bool]:
        """Reattach to the known sandbox, else create a new one. Returns
        ``(handle, is_new)``."""
        ref_id = (
            m.sandbox.id
            if m.sandbox is not None
            else (m.agent.sandbox.sandbox_id if m.agent.sandbox is not None else None)
        )
        if ref_id is not None and m.parked != "killed":
            try:
                handle = await self._provider.connect(ref_id)
                await handle.resume()
                return handle, False
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                m.log.info("sandbox_reconnect_failed", error=type(exc).__name__)
                if m.sandbox is not None:
                    with suppress(Exception):
                        await m.sandbox.kill()
        return await self._create_sandbox(m), True


class AgentWorker:
    """Consumes one agent's assignment queue and runs its tasks (module docstring)."""

    def __init__(self, pool: AgentPool, member: _Member) -> None:
        self._pool = pool
        self._m = member
        self._queue = agent_queue(member.agent.id)
        self._actor = Actor.agent(member.agent.id, member.agent.name)
        self._log = member.log

    async def run(self) -> None:
        pool = self._pool
        while True:
            delivery = await pool._bus.dequeue(
                self._queue, self._m.agent.id, timeout_s=pool.config.dequeue_timeout_s
            )
            if delivery is None:
                continue
            await pool._bus.ack(delivery)
            item = delivery.item
            if not isinstance(item, TaskAssignedEvent) or item.payload.agent_id != self._m.agent.id:
                self._log.warning("unexpected_queue_item")
                continue
            try:
                await self._handle(item)
            except asyncio.CancelledError:
                raise
            except Exception:
                # e.g. the event store is down: the lease expires and the task is requeued
                self._log.exception("assignment_failed", task_key=item.payload.task_key)
            finally:
                self._m.order = None
                self._m.run = None

    async def _record(self, payload: EventPayload, order: WorkOrder) -> None:
        await self._pool._events.record(
            self._pool.session.id,
            EventDraft(payload=payload, actor=self._actor, correlation_id=order.task.id),
        )

    async def _holds(self, order: WorkOrder) -> bool:
        pool = self._pool
        return await pool._leases.renew(order.lease_key, order.lease_owner, pool.lease_ttl_s)

    async def _handle(self, assigned: TaskAssignedEvent) -> None:
        pool, m = self._pool, self._m
        p = assigned.payload
        log_ = self._log.bind(task_key=p.task_key)
        order = None if pool.stopping else pool.source.work_order(assigned)
        if order is None or not await self._holds(order):
            log_.info("stale_assignment_dropped", attempt=p.attempt)
            return
        runtime, sandbox = m.runtime, m.sandbox
        if runtime is None or sandbox is None:
            log_.warning("assignment_without_runtime")
            return
        m.order = order
        await pool._events.record(
            pool.session.id,
            EventDraft(
                payload=TaskAssignmentAcked(
                    task_id=p.task_id, task_key=p.task_key, agent_id=m.agent.id, attempt=p.attempt
                ),
                actor=self._actor,
                causation_id=assigned.id,
                correlation_id=p.task_id,
            ),
        )
        if order.upstream.artifacts:
            try:
                await pool._prefetch.prefetch(m.agent, sandbox, order.upstream.artifacts)
            except Exception as exc:
                log_.warning("artifact_prefetch_failed", error=type(exc).__name__)
        await self._record(
            TaskStarted(
                task_id=p.task_id, task_key=p.task_key, agent_id=m.agent.id, attempt=p.attempt
            ),
            order,
        )
        m.run = asyncio.create_task(
            runtime.run_task(order.task, order.upstream), name=f"run-{m.agent.name}-{p.task_key}"
        )
        await asyncio.wait({m.run})
        if m.run.cancelled():
            log_.warning("run_cancelled")
            return
        outcome = m.run.result()  # re-raises run_task failures (event store down)
        await self._finish(order, outcome)

    async def _finish(self, order: WorkOrder, outcome: TaskOutcome) -> None:
        pool, m = self._pool, self._m
        t = order.task
        if outcome.reason == "lease_lost":
            return  # the scheduler requeued the task; nothing of ours to report
        if outcome.status == "stopped" and pool.stopping:
            await pool._leases.release(order.lease_key, order.lease_owner)
            return  # the scheduler cancels tasks of a stopped session
        if not await self._holds(order):
            self._log.warning("lease_lost_before_report", task_key=t.key)
            return
        if outcome.status == "submitted" and outcome.result is not None:
            await self._record(
                TaskResultSubmitted(
                    task_id=t.id, task_key=t.key, agent_id=m.agent.id, result=outcome.result
                ),
                order,
            )
        else:
            reason: FailureReason | Literal["stopped"] = (
                "stopped" if outcome.status == "stopped" else (outcome.reason or "fail_task")
            )
            detail = f"{reason}: {outcome.detail}" if outcome.detail else reason
            await self._record(
                TaskFailed(
                    task_id=t.id,
                    task_key=t.key,
                    reason=detail[:1000],
                    attempt=order.attempt,
                    will_retry=pool.source.will_retry(order, reason),
                    agent_id=m.agent.id,
                ),
                order,
            )
            if reason in ("sandbox_lost", "sandbox_error"):
                await pool._set_status(m, "error", reason)
        await pool._leases.release(order.lease_key, order.lease_owner)
