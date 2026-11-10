"""Scheduler (SPEC §12): ready set, assignment, acks, leases, retries, completion,
budgets and follow-ups for one session. See docs/coordination-protocol.md
("Scheduling") for the protocol; this docstring covers the implementation.

**State is a fold over the session's events.** ``SchedulerState.apply`` rebuilds
everything the scheduler needs (tasks, agents, assignments, affinity, artifacts,
usage, which budget warnings were sent) from persisted events, so a restarted
runtime calls ``Scheduler.run`` again and continues where the log ends. Events are
applied strictly in ``seq`` order: the scheduler applies the events it records
itself immediately, the rest arrive from the bus stream, and any gap (or a quiet
bus) is filled from ``EventStore.read``. The scheduler never emits
``session.created``; the session must exist in the store.

**One decision loop.** ``run`` handles one input at a time (an event, a stop
request or a tick), then calls ``_reconcile``, which looks at the state and emits
whatever is due. Every rule is idempotent, so repeating ``_reconcile`` is harmless.
Worker tasks (``pool.AgentWorker``) never touch the state; they talk to the
scheduler only through events and the two read-only ``WorkSource`` calls.

**Tasks are created by the scheduler**: when it sees ``session.approved`` for plan
version ``v`` it emits ``task.created`` for every task of that version whose key
does not exist yet (the first plan, and new tasks of follow-up/extended plans;
rule 9). Changes to existing keys in later versions are ignored.

**Review**: with ``review="auto"`` (until T17) the scheduler accepts every
submitted result (``task.accepted``) itself. With ``review="supervisor"`` it waits
for the Supervisor's ``task.accepted`` / ``task.rework_requested``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

import structlog

from agentplatform.bus.base import EventBus, LeaseInfo, LeaseStore, agent_queue
from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.common import Actor, SessionStatus, TaskCreator, new_id, utcnow
from agentplatform.contracts.events import (
    AgentCreated,
    AgentSandboxReady,
    AgentStatusChanged,
    AgentStep,
    AgentStopped,
    ArtifactPublished,
    BudgetMetric,
    Event,
    EventDraft,
    EventEnvelope,
    EventPayload,
    SessionApproved,
    SessionBudgetExceeded,
    SessionBudgetWarning,
    SessionCompleted,
    SessionCreated,
    SessionFailed,
    SessionPaused,
    SessionPlanProposed,
    SessionPlanUpdated,
    SessionResumed,
    SessionStopped,
    SupervisorTriggered,
    TaskAccepted,
    TaskAssigned,
    TaskAssignedEvent,
    TaskAssignmentAcked,
    TaskCancelled,
    TaskCreated,
    TaskFailed,
    TaskReady,
    TaskResultSubmitted,
    TaskReviewStarted,
    TaskReworkRequested,
    TaskStarted,
    TaskUpdated,
)
from agentplatform.contracts.session import Budgets, Session, Usage
from agentplatform.contracts.task import Task, TaskDraft
from agentplatform.db.event_store import EventStore
from agentplatform.errors import ConflictError, InvalidRequestError, NotFoundError
from agentplatform.runtime.agent.context import UpstreamInputs, UpstreamValue
from agentplatform.runtime.agent.loop import EventSink
from agentplatform.runtime.pool import AgentPool, WorkOrder

__all__ = [
    "FINISHED_TASK_STATUSES",
    "Assignment",
    "ReassignOutcome",
    "Scheduler",
    "SchedulerConfig",
    "SchedulerState",
    "SessionBudget",
    "StopReason",
    "descendant_counts",
    "lease_key",
    "lease_owner",
    "pick_assignments",
    "ready_keys",
    "resolve_upstream",
    "retry_decision",
    "stalled",
]

log = structlog.get_logger(__name__)

StopReason = Literal["user", "budget", "idle", "error"]
ReassignOutcome = Literal["requeued", "preferred", "unchanged"]
FINISHED_TASK_STATUSES = frozenset({"done", "cancelled"})
_ACTIVE_TASK_STATUSES = frozenset({"ready", "assigned", "running", "in_review"})
_NO_RETRY_REASONS = frozenset({"budget"})
_HELD_MODES = frozenset({"pause", "takeover"})


@dataclass(frozen=True, slots=True)
class SchedulerConfig:
    ack_timeout_s: float = 10.0
    """``task.assignment_acked`` must arrive within this time or the task is reassigned."""
    max_reassignments: int = 3
    """Consecutive ack timeouts before failing an assignment through the retry policy."""
    lease_ttl_s: float = 45.0
    """Task lease TTL; agents renew it with every heartbeat (10 s)."""
    tick_s: float = 1.0
    """Period of the time-based checks (acks, leases, idle, budgets, store catch-up)."""
    idle_timeout_s: float = 300.0
    """Idle agents with no unfinished work are parked (paused, else killed) after this."""
    stop_grace_s: float = 30.0
    """Budget stop: how long running tasks get to fail with ``budget`` themselves."""
    budget_warning_ratio: float = 0.8
    review: Literal["auto", "supervisor"] = "auto"
    """``auto``: the scheduler accepts submitted results itself (until T17)."""
    supervisor_enabled: bool = False
    """``False``: a DAG stalled by failed tasks ends in ``session.failed``. ``True``:
    the scheduler waits for the Supervisor (replan, cancel, ask the user)."""


# -- pure helpers -------------------------------------------------------------------------


def lease_key(session_id: str, task_id: str) -> str:
    return f"task:{session_id}:{task_id}"


def lease_owner(agent_id: str, assigned_event_id: str) -> str:
    """Unique per assignment, so a stale assignment can never renew a newer one."""
    return f"{agent_id}:{assigned_event_id}"


def ready_keys(tasks: Mapping[str, Task]) -> list[str]:
    """Pending tasks whose dependencies are all ``done`` (SPEC §6.2), in plan order."""
    return [
        t.key
        for t in tasks.values()
        if t.status == "pending"
        and all(dep in tasks and tasks[dep].status == "done" for dep in t.depends_on)
    ]


def descendant_counts(tasks: Mapping[str, Task]) -> dict[str, int]:
    """Number of unfinished tasks that (transitively) depend on each task."""
    children: dict[str, list[str]] = {k: [] for k in tasks}
    for t in tasks.values():
        for dep in t.depends_on:
            if dep in children:
                children[dep].append(t.key)
    counts: dict[str, int] = {}
    for key in tasks:
        seen: set[str] = set()
        stack = list(children[key])
        while stack:
            child = stack.pop()
            if child in seen:
                continue
            seen.add(child)
            stack.extend(children[child])
        counts[key] = sum(1 for k in seen if tasks[k].status not in FINISHED_TASK_STATUSES)
    return counts


def pick_assignments(
    ready: Sequence[Task],
    agents: Sequence[str],
    *,
    done_by: Mapping[str, str],
    descendants: Mapping[str, int],
    preferred: Mapping[str, str] | None = None,
    avoid: Mapping[str, Iterable[str]] | None = None,
) -> list[tuple[Task, str]]:
    """Pair idle agents with ready tasks (SPEC §12.3).

    Greedy over all (task, agent) pairs, best first by: not avoided (the agent
    timed out on this task before), preferred agent (rework), affinity (number of
    the task's dependencies this agent completed), critical path (unfinished
    descendants), plan order, agent order. ``ready`` must be in plan order.
    """
    preferred = preferred or {}
    avoid_sets = {k: set(v) for k, v in (avoid or {}).items()}
    task_rank = {t.key: i for i, t in enumerate(ready)}
    agent_rank = {a: i for i, a in enumerate(agents)}

    def score(pair: tuple[Task, str]) -> tuple[int, int, int, int, int, int]:
        task, agent = pair
        return (
            0 if agent in avoid_sets.get(task.key, set()) else 1,
            1 if preferred.get(task.key) == agent else 0,
            sum(1 for dep in task.depends_on if done_by.get(dep) == agent),
            descendants.get(task.key, 0),
            -task_rank[task.key],
            -agent_rank[agent],
        )

    tasks_left, agents_left = list(ready), list(agents)
    out: list[tuple[Task, str]] = []
    while tasks_left and agents_left:
        task, agent = max(((t, a) for t in tasks_left for a in agents_left), key=score)
        out.append((task, agent))
        tasks_left.remove(task)
        agents_left.remove(agent)
    return out


def resolve_upstream(
    task: Task, tasks: Mapping[str, Task], artifacts: Mapping[str, Artifact]
) -> UpstreamInputs:
    """Values of the task's ``InputRef``s from upstream results, plus the artifacts
    upstream tasks (dependencies and input sources) produced."""
    values: list[UpstreamValue] = []
    for ref in task.inputs:
        up = tasks.get(ref.from_task)
        if up is not None and up.result is not None and ref.output_key in up.result.outputs:
            values.append(
                UpstreamValue(ref.from_task, ref.output_key, up.result.outputs[ref.output_key])
            )
    ids: list[str] = []
    for key in dict.fromkeys([*task.depends_on, *(r.from_task for r in task.inputs)]):
        up = tasks.get(key)
        if up is not None and up.result is not None:
            ids.extend(up.result.artifact_ids)
    return UpstreamInputs(
        values=values, artifacts=[artifacts[i] for i in dict.fromkeys(ids) if i in artifacts]
    )


def retry_decision(task: Task, attempt: int, reason: str) -> bool:
    """Retry a failed attempt? (SPEC §12.6). Reworked runs do not count against
    ``max_attempts``; ``budget`` failures are never retried."""
    if reason.split(":", 1)[0] in _NO_RETRY_REASONS:
        return False
    failures = max(attempt, task.attempt) - task.rework_count
    return failures < task.max_attempts


def stalled(tasks: Mapping[str, Task]) -> bool:
    """True if unfinished tasks exist but none can ever make progress: nothing is
    ready/assigned/running/in review and every pending task depends (transitively)
    on a failed or missing task."""
    unfinished = [t for t in tasks.values() if t.status not in FINISHED_TASK_STATUSES]
    if not unfinished or any(t.status in _ACTIVE_TASK_STATUSES for t in unfinished):
        return False
    dead: dict[str, bool] = {}

    def is_dead(key: str, path: frozenset[str]) -> bool:
        if key in dead:
            return dead[key]
        t = tasks.get(key)
        if t is None or t.status in ("failed", "cancelled") or key in path:
            result = True
        elif t.status in FINISHED_TASK_STATUSES:
            result = False
        else:
            result = any(is_dead(d, path | {key}) for d in t.depends_on)
        dead[key] = result
        return result

    return all(t.status == "failed" or is_dead(t.key, frozenset()) for t in unfinished)


# -- budgets ------------------------------------------------------------------------------


class SessionBudget:
    """Session-wide ``BudgetMonitor`` shared by every agent runtime (SPEC §12.8).
    Runtimes call ``record`` after each step and stop when ``exceeded`` is set; the
    scheduler emits the warning/exceeded events from ``metrics``."""

    def __init__(
        self,
        budgets: Budgets,
        *,
        usage: Usage | None = None,
        started_at: datetime | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self.budgets = budgets
        self.usage = usage or Usage()
        self.started_at = started_at
        self._now = now

    def record(self, usage: Usage) -> None:
        self.usage = self.usage.plus(usage)

    def metrics(self) -> list[tuple[BudgetMetric, float, float]]:
        b, u = self.budgets, self.usage
        out: list[tuple[BudgetMetric, float, float]] = [
            ("total_steps", float(u.steps), float(b.max_total_steps))
        ]
        if b.max_tokens is not None:
            out.append(("tokens", float(u.input_tokens + u.output_tokens), float(b.max_tokens)))
        if b.max_cost_usd is not None:
            out.append(("cost_usd", u.cost_usd, b.max_cost_usd))
        if self.started_at is not None:
            minutes = (self._now() - self.started_at).total_seconds() / 60
            out.append(("wall_minutes", minutes, float(b.max_wall_minutes)))
        return out

    def exceeded(self) -> str | None:
        for metric, used, limit in self.metrics():
            if used >= limit:
                return f"{metric} budget ({limit:g}) reached"
        return None


# -- state --------------------------------------------------------------------------------


@dataclass(slots=True)
class Assignment:
    agent_id: str
    attempt: int
    event_id: str
    ack_deadline: datetime
    acked: bool = False

    @property
    def owner(self) -> str:
        return lease_owner(self.agent_id, self.event_id)


@dataclass(slots=True)
class SchedulerState:
    """Everything the scheduler knows, rebuilt from events (``apply``)."""

    session: Session
    status: SessionStatus = "planning"
    approved_at: datetime | None = None
    last_seq: int = 0
    tasks: dict[str, Task] = field(default_factory=dict)
    key_by_id: dict[str, str] = field(default_factory=dict)
    proposals: dict[int, tuple[list[TaskDraft], TaskCreator]] = field(default_factory=dict)
    approved_version: int = 0
    agents: dict[str, Agent] = field(default_factory=dict)
    idle_since: dict[str, datetime] = field(default_factory=dict)
    assignments: dict[str, Assignment] = field(default_factory=dict)
    last_agent: dict[str, str] = field(default_factory=dict)
    done_by: dict[str, str] = field(default_factory=dict)
    preferred: dict[str, str] = field(default_factory=dict)
    avoid: dict[str, set[str]] = field(default_factory=dict)
    ack_timeouts: dict[str, int] = field(default_factory=dict)
    artifacts: dict[str, Artifact] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    warned: set[str] = field(default_factory=set)
    exceeded: set[str] = field(default_factory=set)
    triggered: set[str] = field(default_factory=set)
    """Task keys for which ``supervisor.triggered(task_failed)`` was emitted."""
    completed: bool = False

    @classmethod
    def for_session(cls, session: Session) -> SchedulerState:
        return cls(session=session, status=session.status, approved_at=session.started_at)

    def task_by_id(self, task_id: str) -> Task | None:
        key = self.key_by_id.get(task_id)
        return self.tasks.get(key) if key is not None else None

    def busy_agents(self) -> set[str]:
        return {a.agent_id for a in self.assignments.values()}

    def _set_task(self, key: str, **changes: object) -> None:
        self.tasks[key] = self.tasks[key].model_copy(update=changes)

    def _current(self, key: str, agent_id: str | None, attempt: int | None = None) -> bool:
        a = self.assignments.get(key)
        if a is None or (agent_id is not None and a.agent_id != agent_id):
            return False
        return attempt is None or a.attempt == attempt

    def apply(self, event: Event) -> None:
        self.last_seq = max(self.last_seq, event.seq)
        p = event.payload
        # -- session
        if isinstance(p, SessionCreated):
            self.session = p.session
            self.status = p.session.status
        elif isinstance(p, SessionPlanProposed):
            self.proposals[p.plan_version] = (list(p.proposal.tasks), "planner")
        elif isinstance(p, SessionPlanUpdated):
            self.proposals[p.plan_version] = (list(p.proposal.tasks), p.source)
        elif isinstance(p, SessionApproved):
            self.status = "running"
            self.approved_version = max(self.approved_version, p.plan_version)
            if self.approved_at is None:
                self.approved_at = event.ts
        elif isinstance(p, SessionPaused):
            self.status = "paused"
        elif isinstance(p, SessionResumed):
            self.status = "running"
        elif isinstance(p, SessionCompleted):
            self.status = "completed"
            self.completed = True
        elif isinstance(p, SessionFailed):
            self.status = "failed"
        elif isinstance(p, SessionStopped):
            self.status = "stopped"
        elif isinstance(p, SessionBudgetWarning):
            self.warned.add(p.metric)
        elif isinstance(p, SessionBudgetExceeded):
            self.exceeded.add(p.metric)
        # -- tasks
        elif isinstance(p, TaskCreated):
            if p.task.key not in self.tasks:
                self.tasks[p.task.key] = p.task
                self.key_by_id[p.task.id] = p.task.key
                self.completed = False
        elif isinstance(p, TaskUpdated):
            if p.task.key in self.tasks:
                self.tasks[p.task.key] = p.task
        else:
            self._apply_task_or_agent(event)

    def _apply_task_or_agent(self, event: Event) -> None:
        p = event.payload
        if isinstance(p, TaskReady):
            key = p.task_key
            if key in self.tasks:
                stale = self.assignments.pop(key, None)
                if stale is not None and not stale.acked:
                    self.avoid.setdefault(key, set()).add(stale.agent_id)
                    if event.causation_id == stale.event_id:
                        self.ack_timeouts[key] = self.ack_timeouts.get(key, 0) + 1
                self._set_task(key, status="ready", assigned_agent_id=None)
        elif isinstance(p, TaskAssigned):
            if p.task_key in self.tasks:
                self.assignments[p.task_key] = Assignment(
                    agent_id=p.agent_id,
                    attempt=p.attempt,
                    event_id=event.id,
                    ack_deadline=p.ack_deadline,
                )
                self._set_task(p.task_key, status="assigned", assigned_agent_id=p.agent_id)
        elif isinstance(p, TaskAssignmentAcked):
            if self._current(p.task_key, p.agent_id, p.attempt):
                self.assignments[p.task_key].acked = True
        elif isinstance(p, TaskStarted):
            if self._current(p.task_key, p.agent_id, p.attempt):
                self.assignments[p.task_key].acked = True
                self.last_agent[p.task_key] = p.agent_id
                self._set_task(p.task_key, status="running", attempt=p.attempt)
        elif isinstance(p, TaskResultSubmitted):
            if self._current(p.task_key, p.agent_id):
                self.assignments.pop(p.task_key)
                self._set_task(p.task_key, status="in_review", result=p.result)
        elif isinstance(p, TaskReviewStarted):
            if p.task_key in self.tasks:
                self._set_task(p.task_key, status="in_review")
        elif isinstance(p, TaskAccepted):
            if p.task_key in self.tasks:
                self._set_task(p.task_key, status="done")
                agent = self.last_agent.get(p.task_key)
                if agent is not None:
                    self.done_by[p.task_key] = agent
                self.ack_timeouts.pop(p.task_key, None)
                self.preferred.pop(p.task_key, None)
                self.avoid.pop(p.task_key, None)
        elif isinstance(p, TaskReworkRequested):
            if p.task_key in self.tasks:
                self.ack_timeouts.pop(p.task_key, None)
                self.assignments.pop(p.task_key, None)
                agent = p.agent_id or self.last_agent.get(p.task_key)
                if agent is not None:
                    self.preferred[p.task_key] = agent
                self._set_task(
                    p.task_key,
                    status="pending",
                    assigned_agent_id=None,
                    rework_count=p.rework_count,
                )
        elif isinstance(p, TaskFailed):
            key = p.task_key
            if key in self.tasks and (
                self._current(key, p.agent_id, p.attempt) or key not in self.assignments
            ):
                if p.reason == "assignment_not_acked":
                    self.ack_timeouts[key] = self.ack_timeouts.get(key, 0) + 1
                self.assignments.pop(key, None)
                task = self.tasks[key]
                self._set_task(
                    key,
                    status="pending" if p.will_retry else "failed",
                    assigned_agent_id=None,
                    attempt=max(task.attempt, p.attempt),
                )
        elif isinstance(p, TaskCancelled):
            if p.task_key in self.tasks:
                self.assignments.pop(p.task_key, None)
                self._set_task(p.task_key, status="cancelled", assigned_agent_id=None)
        # -- agents
        elif isinstance(p, AgentCreated):
            self.agents[p.agent.id] = p.agent
        elif isinstance(p, AgentSandboxReady):
            if p.agent_id in self.agents:
                self.agents[p.agent_id] = self.agents[p.agent_id].model_copy(
                    update={
                        "sandbox": p.sandbox,
                        "stream_url": p.stream_url,
                        "interactive_stream_url": p.interactive_stream_url,
                    }
                )
        elif isinstance(p, AgentStatusChanged):
            if p.agent_id in self.agents:
                self.agents[p.agent_id] = self.agents[p.agent_id].model_copy(
                    update={"status": p.status}
                )
                if p.status == "idle":
                    self.idle_since[p.agent_id] = event.ts
                else:
                    self.idle_since.pop(p.agent_id, None)
        elif isinstance(p, AgentStopped):
            if p.agent_id in self.agents:
                self.agents[p.agent_id] = self.agents[p.agent_id].model_copy(
                    update={"status": "stopped"}
                )
                self.idle_since.pop(p.agent_id, None)
        elif isinstance(p, AgentStep):
            self.usage = self.usage.plus(p.usage)
        elif isinstance(p, ArtifactPublished):
            self.artifacts[p.artifact.id] = p.artifact
        elif isinstance(p, SupervisorTriggered):
            if p.reason == "task_failed" and p.task_key is not None:
                self.triggered.add(p.task_key)


# -- scheduler ----------------------------------------------------------------------------


@dataclass(slots=True)
class _StopRequest:
    reason: StopReason
    detail: str | None
    done: asyncio.Future[None]


@dataclass(slots=True)
class _ReassignRequest:
    task_key: str
    agent_id: str | None
    done: asyncio.Future[ReassignOutcome]


@dataclass(slots=True)
class _Stopping:
    reason: StopReason
    detail: str | None
    deadline: float
    announce: bool
    """Emit ``session.stopped`` (False when the session already ended elsewhere)."""


class Scheduler:
    """Runs one session's DAG on an ``AgentPool``. See the module docstring.

    ``run()`` returns once the session has stopped or failed (and the pool is shut
    down). A completed session keeps running: it waits for follow-up tasks and
    parks idle agents after ``idle_timeout_s``. Cancelling ``run()`` (runtime
    shutdown) closes the workers without killing sandboxes, so a restarted runtime
    can adopt them.
    """

    def __init__(
        self,
        *,
        session: Session,
        events: EventSink,
        store: EventStore,
        bus: EventBus,
        leases: LeaseStore,
        pool: AgentPool,
        budget: SessionBudget,
        config: SchedulerConfig | None = None,
        now: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config or SchedulerConfig()
        self.state = SchedulerState.for_session(session)
        self.budget = budget
        self.pool = pool
        self._events = events
        self._store = store
        self._bus = bus
        self._leases = leases
        self._now = now
        self._monotonic = monotonic
        self._inbox: asyncio.Queue[Event | _StopRequest | _ReassignRequest] = asyncio.Queue()
        self._stopping: _Stopping | None = None
        self._stop_waiters: list[asyncio.Future[None]] = []
        self._finished = asyncio.Event()
        self._last_tick = float("-inf")
        self._started_pool = False
        self._running = False
        self._actor = Actor.system()
        self._log = log.bind(session_id=session.id)
        pool.attach(self)

    @property
    def session_id(self) -> str:
        return self.state.session.id

    @property
    def finished(self) -> bool:
        return self._finished.is_set()

    async def wait_finished(self) -> None:
        await self._finished.wait()

    # -- public API -----------------------------------------------------------------

    async def run(self) -> None:
        if self._running:
            raise RuntimeError("Scheduler.run() is already running")
        self._running = True
        feeder = asyncio.create_task(self._feed(), name=f"scheduler-feed-{self.session_id}")
        try:
            await self._catch_up()
            self.budget.usage = self.state.usage
            self.budget.started_at = self.state.approved_at
            while not self._finished.is_set():
                await self._reconcile()
                if self._finished.is_set():
                    break
                await self._next_input()
        finally:
            self._running = False
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
            if not self._finished.is_set():
                await self.pool.close()
            for fut in self._stop_waiters:
                if not fut.done():
                    fut.set_result(None)
            while not self._inbox.empty():
                item = self._inbox.get_nowait()
                if isinstance(item, _ReassignRequest) and not item.done.done():
                    item.done.set_exception(ConflictError("the scheduler is not running"))

    async def stop(self, reason: StopReason = "user", detail: str | None = None) -> None:
        """Stop the session: stop every run, cancel unfinished tasks, emit
        ``session.stopped`` and shut the pool down. Returns when done."""
        if self._finished.is_set():
            return
        if not self._running:
            raise RuntimeError("Scheduler.stop() needs a running Scheduler.run()")
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._stop_waiters.append(fut)
        await self._inbox.put(_StopRequest(reason, detail, fut))
        await fut

    async def reassign(self, task_key: str, agent_id: str | None = None) -> ReassignOutcome:
        """Move a task to another agent (used by the Supervisor's ``reassign_task``).

        Runs inside the decision loop, so it never races assignment. ``agent_id``
        is a preference (like a rework's): the task goes to that agent when it is
        idle, otherwise to the best idle agent. With ``agent_id=None`` the current
        agent is avoided for this task.

        * ``pending`` / ``ready`` task: only the preference is recorded
          (``"preferred"``).
        * ``assigned`` / ``running`` task: emits ``task.ready``, releases the
          current lease and fences the current agent's run through
          ``AgentPool.recover`` (it reconnects its sandbox and returns to idle)
          (``"requeued"``). A started run counts as an attempt.
        * Already with ``agent_id``: nothing happens (``"unchanged"``).

        Preferences live in memory and do not survive a runtime restart. Raises
        ``NotFoundError`` (unknown task), ``InvalidRequestError`` (unknown or dead
        agent) or ``ConflictError`` (task in review, finished or failed; session
        stopping).
        """
        if not self._running:
            raise RuntimeError("Scheduler.reassign() needs a running Scheduler.run()")
        fut: asyncio.Future[ReassignOutcome] = asyncio.get_running_loop().create_future()
        await self._inbox.put(_ReassignRequest(task_key, agent_id, fut))
        return await fut

    def notify(self, event: Event) -> None:
        """Feed an event that did not come through the bus (optional)."""
        self._inbox.put_nowait(event)

    # -- WorkSource -----------------------------------------------------------------

    def work_order(self, assigned: TaskAssignedEvent) -> WorkOrder | None:
        p = assigned.payload
        a = self.state.assignments.get(p.task_key)
        task = self.state.tasks.get(p.task_key)
        if self._stopping is not None or a is None or task is None or a.event_id != assigned.id:
            return None
        if task.status != "assigned":
            return None
        running = task.model_copy(
            update={"status": "running", "attempt": a.attempt, "assigned_agent_id": a.agent_id}
        )
        return WorkOrder(
            task=running,
            upstream=resolve_upstream(task, self.state.tasks, self.state.artifacts),
            attempt=a.attempt,
            lease_key=lease_key(self.session_id, task.id),
            lease_owner=a.owner,
            assigned_event_id=assigned.id,
        )

    def will_retry(self, order: WorkOrder, reason: str) -> bool:
        if self._stopping is not None:
            return False
        task = self.state.tasks.get(order.task.key)
        return task is not None and retry_decision(task, order.attempt, reason)

    # -- input ----------------------------------------------------------------------

    async def _feed(self) -> None:
        async for record in self._bus.subscribe(self.session_id, include_ephemeral=False):
            item = record.item
            if isinstance(item, EventEnvelope):
                await self._inbox.put(item)

    async def _next_input(self) -> None:
        timeout = max(0.0, self._last_tick + self.config.tick_s - self._monotonic())
        try:
            item = await asyncio.wait_for(self._inbox.get(), timeout=timeout)
        except TimeoutError:
            return
        items = [item]
        while not self._inbox.empty():
            items.append(self._inbox.get_nowait())
        for it in items:
            if isinstance(it, _StopRequest):
                if self._stopping is None:
                    await self._begin_stop(it.reason, it.detail, announce=True)
            elif isinstance(it, _ReassignRequest):
                try:
                    outcome = await self._reassign(it.task_key, it.agent_id)
                except Exception as exc:
                    if not it.done.done():
                        it.done.set_exception(exc)
                else:
                    if not it.done.done():
                        it.done.set_result(outcome)
            else:
                await self._absorb(it)

    async def _absorb(self, event: Event) -> None:
        if event.session_id != self.session_id or event.seq <= self.state.last_seq:
            return
        if event.seq == self.state.last_seq + 1:
            self.state.apply(event)
        else:
            await self._catch_up()

    async def _catch_up(self) -> None:
        while True:
            batch = await self._store.read(self.session_id, self.state.last_seq, limit=500)
            if not batch:
                return
            for event in batch:
                if event.seq > self.state.last_seq:
                    self.state.apply(event)

    async def _emit(
        self,
        payload: EventPayload,
        *,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> Event:
        event = await self._events.record(
            self.session_id,
            EventDraft(
                payload=payload,
                actor=self._actor,
                correlation_id=correlation_id,
                causation_id=causation_id,
            ),
        )
        await self._absorb(event)
        return event

    # -- the decision loop ----------------------------------------------------------

    async def _reconcile(self) -> None:
        st = self.state
        periodic = self._monotonic() - self._last_tick >= self.config.tick_s
        if periodic:
            self._last_tick = self._monotonic()
            await self._catch_up()
            self.budget.started_at = st.approved_at
        if self._stopping is None and st.status in ("failed", "stopped"):
            await self._begin_stop("error", None, announce=False)
        if self._stopping is not None:
            await self._progress_stop()
            return
        if st.status not in ("running", "paused", "completed"):
            return  # not approved yet
        await self._ensure_agents()
        await self._create_approved_tasks()
        # Every time, not only on ticks: a runtime that hit the budget fails its task
        # with reason "budget", and that must end in a budget stop, not a stall.
        if await self._check_budget():
            return
        if periodic:
            await self._check_acks()
            await self._check_leases()
        await self._recover_agents()
        await self._accept_results()
        await self._mark_ready()
        if st.status == "running":
            await self._assign()
        await self._trigger_failed()
        await self._check_end(periodic)

    async def _ensure_agents(self) -> None:
        st = self.state
        if self._started_pool:
            return
        self._started_pool = True
        if st.agents:
            await self.pool.adopt(list(st.agents.values()))
        else:
            count = 1 if st.session.mode == "solo" else st.session.agent_count
            await self.pool.start(count)

    async def _create_approved_tasks(self) -> None:
        st = self.state
        proposal = st.proposals.get(st.approved_version)
        if proposal is None:
            return
        drafts, creator = proposal
        for draft in drafts:
            if draft.key in st.tasks:
                continue
            task = Task(
                **draft.model_dump(),
                id=new_id(),
                session_id=self.session_id,
                plan_version=st.approved_version,
                status="pending",
                created_by=creator,
            )
            await self._emit(TaskCreated(task=task), correlation_id=task.id)

    async def _mark_ready(self) -> None:
        if self._stopping is not None:
            return
        for key in ready_keys(self.state.tasks):
            t = self.state.tasks[key]
            await self._emit(TaskReady(task_id=t.id, task_key=key), correlation_id=t.id)

    async def _accept_results(self) -> None:
        if self.config.review != "auto":
            return
        for t in list(self.state.tasks.values()):
            if t.status == "in_review" and t.result is not None:
                await self._emit(
                    TaskAccepted(
                        task_id=t.id, task_key=t.key, note="auto-accepted (no supervisor review)"
                    ),
                    correlation_id=t.id,
                )

    def _idle_agents(self) -> list[str]:
        st, busy = self.state, self.state.busy_agents()
        return [
            a
            for a in self.pool.agent_ids
            if a in st.agents
            and st.agents[a].status == "idle"
            and a not in busy
            and self.pool.available(a)
            and not self._held(a)
        ]

    def _held(self, agent_id: str) -> bool:
        """Paused or taken over (control ``pause``/``takeover``): no new work until
        the user releases (or resumes) the agent."""
        runtime = self.pool.runtime(agent_id)
        return runtime is not None and runtime.control.mode in _HELD_MODES

    async def _assign(self) -> None:
        st = self.state
        ready = [t for t in st.tasks.values() if t.status == "ready"]
        if not ready:
            return
        idle = self._idle_agents()
        if len(ready) > len(idle):
            for agent_id in self.pool.agent_ids:
                if self.pool.revivable(agent_id):
                    self.pool.revive(agent_id)
        pairs = pick_assignments(
            ready,
            idle,
            done_by=st.done_by,
            descendants=descendant_counts(st.tasks),
            preferred=st.preferred,
            avoid=st.avoid,
        )
        for task, agent_id in pairs:
            await self._assign_one(task, agent_id)

    async def _assign_one(self, task: Task, agent_id: str) -> None:
        try:
            await self.pool.unpark(agent_id)
        except Exception as exc:
            self._log.warning("unpark_failed", agent_id=agent_id, error=type(exc).__name__)
            await self.pool.set_status(agent_id, "error", "sandbox resume failed")
            return
        now = self._now()
        cfg = self.config
        event = await self._emit(
            TaskAssigned(
                task_id=task.id,
                task_key=task.key,
                agent_id=agent_id,
                attempt=task.attempt + 1,
                ack_deadline=now + timedelta(seconds=cfg.ack_timeout_s),
                lease_expires_at=now + timedelta(seconds=cfg.lease_ttl_s),
            ),
            correlation_id=task.id,
        )
        key = lease_key(self.session_id, task.id)
        owner = lease_owner(agent_id, event.id)
        if not await self._leases.acquire(key, owner, cfg.lease_ttl_s):
            # someone still holds it: no delivery; the ack timeout reassigns
            self._log.warning("lease_busy", task_key=task.key)
            return
        await self._bus.enqueue(agent_queue(agent_id), event)
        self._log.info("task_assigned", task_key=task.key, agent_id=agent_id)

    async def _reassign(self, key: str, agent_id: str | None) -> ReassignOutcome:
        await self._catch_up()
        st = self.state
        task = st.tasks.get(key)
        if task is None:
            raise NotFoundError(f"task {key} does not exist")
        if self._stopping is not None:
            raise ConflictError("the session is stopping")
        if task.status not in ("pending", "ready", "assigned", "running"):
            raise ConflictError(f"task {key} is {task.status} and cannot be reassigned")
        if agent_id is not None and (agent_id not in st.agents or self.pool.dead(agent_id)):
            raise InvalidRequestError(f"agent {agent_id} is not a live agent of this session")
        current = st.assignments.get(key)
        if current is not None and current.agent_id == agent_id:
            return "unchanged"
        if agent_id is not None:
            st.preferred[key] = agent_id
            st.avoid.get(key, set()).discard(agent_id)
        else:
            st.preferred.pop(key, None)
            previous = current.agent_id if current is not None else st.last_agent.get(key)
            if previous is not None:
                st.avoid.setdefault(key, set()).add(previous)
        if current is None:
            return "preferred"
        await self._emit(TaskReady(task_id=task.id, task_key=key), correlation_id=task.id)
        await self._leases.release(lease_key(self.session_id, task.id), current.owner)
        if current.acked or self.pool.running(current.agent_id):
            self.pool.recover(current.agent_id, f"task {key} reassigned")
        self._log.info("task_reassigned", task_key=key, from_agent=current.agent_id)
        return "requeued"

    async def _check_acks(self) -> None:
        now = self._now()
        for key, a in list(self.state.assignments.items()):
            if a.acked or now < a.ack_deadline:
                continue
            task = self.state.tasks[key]
            await self._leases.release(lease_key(self.session_id, task.id), a.owner)
            if self.pool.has(a.agent_id):
                await self.pool.report_error(
                    a.agent_id,
                    f"no task.assignment_acked for {key} within {self.config.ack_timeout_s:g} s",
                )
            payload: EventPayload
            if self.state.ack_timeouts.get(key, 0) + 1 >= self.config.max_reassignments:
                reason = "assignment_not_acked"
                payload = TaskFailed(
                    task_id=task.id,
                    task_key=key,
                    reason=reason,
                    attempt=a.attempt,
                    agent_id=a.agent_id,
                    will_retry=retry_decision(task, a.attempt, reason),
                )
            else:
                payload = TaskReady(task_id=task.id, task_key=key)
            await self._emit(payload, correlation_id=task.id, causation_id=a.event_id)
            self._log.warning("ack_timeout", task_key=key, agent_id=a.agent_id)
            self.pool.recover(a.agent_id, "assignment not acknowledged")

    async def _check_leases(self) -> None:
        expired: list[LeaseInfo] = await self._leases.expired(f"task:{self.session_id}:")
        for lease in expired:
            task = self.state.task_by_id(lease.key.rsplit(":", 1)[-1])
            a = self.state.assignments.get(task.key) if task is not None else None
            if task is not None and a is not None and a.owner == lease.owner:
                await self._expire(task, a)
            await self._leases.forget(lease.key)

    async def _expire(self, task: Task, a: Assignment) -> None:
        reason = f"lease_expired: no heartbeat within {self.config.lease_ttl_s:g} s"
        await self._emit(
            TaskFailed(
                task_id=task.id,
                task_key=task.key,
                reason=reason,
                attempt=a.attempt,
                will_retry=retry_decision(task, a.attempt, reason),
                agent_id=a.agent_id,
            ),
            correlation_id=task.id,
        )
        if self.pool.has(a.agent_id):
            await self.pool.report_error(a.agent_id, "task lease expired")
        self._log.warning("lease_expired", task_key=task.key, agent_id=a.agent_id)
        self.pool.recover(a.agent_id, "task lease expired")

    async def _recover_agents(self) -> None:
        for agent_id, agent in self.state.agents.items():
            if agent.status == "error" and self.pool.recoverable(agent_id):
                self.pool.recover(agent_id, "agent in error")

    async def _trigger_failed(self) -> None:
        st = self.state
        for t in st.tasks.values():
            if t.status == "failed" and t.key not in st.triggered:
                await self._emit(
                    SupervisorTriggered(
                        reason="task_failed",
                        detail=f"task {t.key} failed after {t.attempt} attempt(s)",
                        task_key=t.key,
                        agent_id=st.last_agent.get(t.key),
                    ),
                    correlation_id=t.id,
                )

    async def _check_end(self, periodic: bool) -> None:
        st = self.state
        tasks = st.tasks
        if (
            st.status == "running"
            and st.approved_version in st.proposals
            and all(t.status in FINISHED_TASK_STATUSES for t in tasks.values())
            and not st.completed
        ):
            done = sum(1 for t in tasks.values() if t.status == "done")
            await self._emit(SessionCompleted(summary=f"{done} of {len(tasks)} tasks done"))
            return
        if st.status == "running" and not self.config.supervisor_enabled and stalled(tasks):
            blocked = [t.key for t in tasks.values() if t.status in ("failed", "cancelled")]
            await self._emit(
                SessionFailed(reason=f"tasks failed or cancelled: {', '.join(blocked)}")
            )
            return
        if (
            st.status == "running"
            and st.agents
            and all(self.pool.dead(a) for a in st.agents)
            and any(t.status not in FINISHED_TASK_STATUSES for t in tasks.values())
        ):
            await self._emit(SessionFailed(reason="no agent could be started or recovered"))
            return
        if periodic and not any(t.status not in FINISHED_TASK_STATUSES for t in tasks.values()):
            await self._park_idle()

    async def _park_idle(self) -> None:
        limit = timedelta(seconds=self.config.idle_timeout_s)
        now = self._now()
        for agent_id, since in list(self.state.idle_since.items()):
            if now - since >= limit and self.pool.has(agent_id) and not self.pool.parked(agent_id):
                await self.pool.park(agent_id, "idle timeout")

    async def _check_budget(self) -> bool:
        """Warning at 80 %, stop at 100 % (SPEC §12.8). True if stopping."""
        for metric, used, limit in self.budget.metrics():
            if limit <= 0:
                continue
            if used >= self.config.budget_warning_ratio * limit and metric not in self.state.warned:
                await self._emit(SessionBudgetWarning(metric=metric, used=used, limit=limit))
            if used >= limit and self.state.status in ("running", "paused"):
                if metric not in self.state.exceeded:
                    await self._emit(SessionBudgetExceeded(metric=metric, used=used, limit=limit))
                await self._begin_stop("budget", f"{metric} budget reached", announce=True)
                return True
        return False

    # -- stopping -------------------------------------------------------------------

    async def _begin_stop(self, reason: StopReason, detail: str | None, *, announce: bool) -> None:
        self._log.info("session_stopping", reason=reason)
        self._stopping = _Stopping(
            reason=reason,
            detail=detail,
            deadline=self._monotonic() + self.config.stop_grace_s,
            announce=announce,
        )
        if reason != "budget":
            # Runtimes stop on their own for a budget stop (BudgetMonitor) and fail
            # their task with reason "budget"; everything else stops now.
            await self.pool.stop_all()

    async def _progress_stop(self) -> None:
        stop = self._stopping
        assert stop is not None
        if self.pool.any_running():
            if self._monotonic() < stop.deadline:
                return
            await self.pool.stop_all(grace_s=1.0)
        await asyncio.sleep(0)
        await self._catch_up()
        for t in list(self.state.tasks.values()):
            if t.status not in ("done", "cancelled", "failed"):
                a = self.state.assignments.get(t.key)
                if a is not None:
                    await self._leases.release(lease_key(self.session_id, t.id), a.owner)
                await self._emit(
                    TaskCancelled(task_id=t.id, task_key=t.key, reason=f"session {stop.reason}"),
                    correlation_id=t.id,
                )
        if stop.announce and self.state.status not in ("completed", "failed", "stopped"):
            await self._emit(SessionStopped(reason=stop.reason, detail=stop.detail))
        await self.pool.shutdown(f"session {self.state.status}")
        self._finished.set()
        for fut in self._stop_waiters:
            if not fut.done():
                fut.set_result(None)
