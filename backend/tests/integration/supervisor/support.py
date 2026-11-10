"""Test support for the supervisor: an in-memory ``BoardService``, a scripted
supervisor model, a "light" world (event log + board, no scheduler) and a world with
the real scheduler, pool and agent runtimes (fakes only: no network, no Docker)."""

from __future__ import annotations

import asyncio
import itertools
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from agentplatform.contracts.agent import Agent, agent_identity
from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import (
    Actor,
    BoardEntryKind,
    BoardEntryStatus,
    HealthStatus,
    new_id,
    utcnow,
)
from agentplatform.contracts.events import (
    AgentCreated,
    BoardEntryPosted,
    BoardEntryStatusChanged,
    Event,
    EventDraft,
    EventPayload,
    SessionApproved,
    SessionCreated,
    SessionPlanProposed,
    TaskAssigned,
    TaskCreated,
    TaskResultSubmitted,
    TaskStarted,
)
from agentplatform.contracts.model_io import ModelRequest, ModelResponse
from agentplatform.contracts.plan import PlanProposal
from agentplatform.contracts.session import Session
from agentplatform.contracts.task import Task, TaskDraft, TaskResult
from agentplatform.errors import BoardRuleError, NotFoundError
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.loop import AgentRuntime, AgentRuntimeConfig, build_agent_registry
from agentplatform.runtime.coordination.digest import DigestBuilder
from agentplatform.runtime.coordination.visibility import visible_to
from agentplatform.runtime.pool import AgentPool, PoolConfig, RuntimeDeps
from agentplatform.runtime.scheduler import Scheduler, SchedulerConfig, SessionBudget
from agentplatform.runtime.supervisor import (
    PoolAgentControl,
    Supervisor,
    SupervisorConfig,
    supervised_scheduler_config,
)
from agentplatform.sandboxes.base import SandboxHandle
from agentplatform.testing import (
    FakeSandboxProvider,
    InMemoryArtifactStore,
    InMemoryBus,
    InMemoryEventStore,
    InMemoryLeaseStore,
    make_session,
    make_tasks,
    multi_tool_response,
    text_response,
    tool_response,
)

FAST = SupervisorConfig(
    debounce_s=0.05, tick_s=0.02, model_backoff_s=(), requeue_after_s=0.1, question_timeout_s=90
)
FAST_SCHEDULER = SchedulerConfig(tick_s=0.02, ack_timeout_s=2.0, lease_ttl_s=30.0, stop_grace_s=2.0)
FAST_POOL = PoolConfig(
    fence_grace_s=0.2, recovery_backoff_s=0.01, stop_grace_s=2.0, dequeue_timeout_s=0.05
)

Call = tuple[str, dict[str, Any]]
Policy = Callable[[str], list[Call]]


async def until(check: Callable[[], Awaitable[bool]], limit_s: float = 10.0) -> None:
    async with asyncio.timeout(limit_s):
        for _ in itertools.count():  # polling fakes: nothing to wait on
            if await check():
                return
            await asyncio.sleep(0.01)


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


# -- board --------------------------------------------------------------------------------


class MemoryBoard:
    """``BoardService`` over the event log, with the rules the supervisor relies on
    (decision authors, answer resolves an open question, open status kinds)."""

    def __init__(self, recorder: EventRecorder, agents: Callable[[], Iterable[Agent]]) -> None:
        self._recorder = recorder
        self._agents = agents
        self.entries: dict[str, BoardEntry] = {}

    async def post(
        self,
        session_id: str,
        draft: BoardEntryDraft,
        *,
        author: Actor,
        step: int | None = None,
    ) -> BoardEntry:
        if draft.kind == "decision" and author.type not in ("supervisor", "user"):
            raise BoardRuleError("only the supervisor or the user may post a decision")
        question: BoardEntry | None = None
        if draft.kind == "answer":
            if len(draft.refs) != 1:
                raise BoardRuleError("an answer must reference exactly one question")
            question = self.entries.get(draft.refs[0])
            if question is None or question.kind != "question" or question.status != "open":
                raise BoardRuleError("the question is not open")
        status: BoardEntryStatus | None = (
            "open" if draft.kind in ("question", "claim", "blocker") else None
        )
        holder: list[BoardEntry] = []

        def factory(seq: int) -> EventDraft:
            entry = BoardEntry(
                **draft.model_dump(),
                id=new_id(),
                session_id=session_id,
                seq=seq,
                author=author,
                status=status,
                created_at=utcnow(),
            )
            holder.append(entry)
            return EventDraft(payload=BoardEntryPosted(entry=entry), actor=author)

        await self._recorder.record(session_id, factory)
        entry = holder[-1]
        self.entries[entry.id] = entry
        if question is not None:
            await self.set_status(question.id, "resolved", actor=author)
        return entry

    async def set_status(
        self,
        entry_id: str,
        status: BoardEntryStatus,
        *,
        actor: Actor,
        reason: str | None = None,
    ) -> BoardEntry:
        entry = await self.get(entry_id)
        updated = entry.model_copy(update={"status": status})
        self.entries[entry_id] = updated
        await self._recorder.record(
            entry.session_id,
            EventDraft(
                payload=BoardEntryStatusChanged(
                    entry_id=entry_id, status=status, previous=entry.status, reason=reason
                ),
                actor=actor,
            ),
        )
        return updated

    async def get(self, entry_id: str) -> BoardEntry:
        if entry_id not in self.entries:
            raise NotFoundError(f"entry {entry_id} not found")
        return self.entries[entry_id]

    async def search(
        self,
        session_id: str,
        *,
        query: str | None = None,
        kinds: list[BoardEntryKind] | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
        viewer_agent_id: str | None = None,
    ) -> list[BoardEntry]:
        out = [
            e
            for e in sorted(self.entries.values(), key=lambda e: -e.seq)
            if (kinds is None or e.kind in kinds)
            and (tags is None or set(tags) & set(e.tags))
            and (query is None or query.lower() in f"{e.title} {e.body}".lower())
            and (viewer_agent_id is None or visible_to(e, viewer_agent_id))
        ]
        return out[:limit]

    async def get_by_seq(self, session_id: str, seq: int) -> BoardEntry:
        for e in self.entries.values():
            if e.seq == seq:
                return e
        raise NotFoundError(f"no entry #{seq}")

    async def find_agent(self, session_id: str, name: str) -> Agent | None:
        return next((a for a in self._agents() if a.name.lower() == name.lower()), None)

    async def entries_after(
        self,
        session_id: str,
        after_seq: int,
        *,
        viewer_agent_id: str | None = None,
        limit: int = 200,
    ) -> list[BoardEntry]:
        out = [
            e
            for e in sorted(self.entries.values(), key=lambda e: e.seq)
            if e.seq > after_seq and (viewer_agent_id is None or visible_to(e, viewer_agent_id))
        ]
        return out[:limit]

    async def wait_for_answer(self, question_id: str, timeout_s: float) -> BoardEntry | None:
        try:
            async with asyncio.timeout(timeout_s):
                while True:
                    for e in self.entries.values():
                        if e.kind == "answer" and question_id in e.refs:
                            return e
                    await asyncio.sleep(0.01)
        except TimeoutError:
            return None


# -- supervisor model ---------------------------------------------------------------------


TRIGGER_RE = re.compile(r"^- \[(\w+)\] (.*)$", re.MULTILINE)


def triggers_in(run_text: str) -> list[tuple[str, str]]:
    """``(reason, detail)`` of each trigger line in a run message."""
    return TRIGGER_RE.findall(run_text)


def submitted_tasks(run_text: str) -> list[str]:
    return [
        m.group(1)
        for reason, detail in triggers_in(run_text)
        if reason == "result_submitted" and (m := re.search(r"\b(T\d+)\b", detail))
    ]


class Brain:
    """Supervisor ``ModelClient``: ``policy(run message)`` decides the tool calls of a
    run's first turn; later turns (after tool results) end the run."""

    def __init__(self, policy: Policy | None = None, *, fail_first: int = 0) -> None:
        self.policy: Policy = policy or (lambda text: [("noop", {"reason": "nothing to do"})])
        self.requests: list[ModelRequest] = []
        self.runs: list[str] = []
        self._fail = fail_first

    @property
    def name(self) -> str:
        return "brain"

    async def complete(self, req: ModelRequest) -> ModelResponse:
        self.requests.append(req)
        if self._fail > 0:
            self._fail -= 1
            raise RuntimeError("model down")
        if req.messages[-1].role == "tool":
            return text_response("done")
        text = req.messages[1].text_content()
        self.runs.append(text)
        calls = self.policy(text)
        if not calls:
            return text_response("nothing to do")
        if len(calls) == 1:
            return tool_response(calls[0][0], **calls[0][1])
        return multi_tool_response(*calls)

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=0)

    async def aclose(self) -> None:
        return None


def all_pass(n: int) -> list[dict[str, Any]]:
    return [{"criterion": i, "passed": True, "reason": "checked"} for i in range(1, n + 1)]


# -- fakes for ports ----------------------------------------------------------------------


@dataclass
class FakeControl:
    calls: list[tuple[str, str, str]] = field(default_factory=list)

    async def pause(self, agent_id: str, reason: str) -> None:
        self.calls.append(("pause", agent_id, reason))

    async def resume(self, agent_id: str, note: str) -> None:
        self.calls.append(("resume", agent_id, note))


@dataclass
class FakeReassigner:
    calls: list[tuple[str, str | None]] = field(default_factory=list)

    async def reassign(self, task_key: str, agent_id: str | None = None) -> str:
        self.calls.append((task_key, agent_id))
        return "requeued"


class Clock:
    """A wall clock tests can move forward."""

    def __init__(self) -> None:
        self.offset = timedelta()

    def now(self) -> datetime:
        return utcnow() + self.offset


# -- light world --------------------------------------------------------------------------


@dataclass
class Light:
    """Event log, bus, board and a supervisor; no scheduler (tests emit task events)."""

    session: Session
    store: InMemoryEventStore
    bus: InMemoryBus
    recorder: EventRecorder
    board: MemoryBoard
    brain: Brain
    control: FakeControl
    reassigner: FakeReassigner
    frames: InMemoryArtifactStore
    clock: Clock
    config: SupervisorConfig
    agents: list[Agent] = field(default_factory=list)
    supervisor: Supervisor | None = None
    task: asyncio.Task[None] | None = None

    @property
    def sup(self) -> Supervisor:
        assert self.supervisor is not None
        return self.supervisor

    def agent(self, name: str) -> Agent:
        return next(a for a in self.agents if a.name == name)

    async def record(
        self, payload: EventPayload, actor: Actor | None = None, causation_id: str | None = None
    ) -> Event:
        return await self.recorder.record(
            self.session.id,
            EventDraft(payload=payload, actor=actor or Actor.system(), causation_id=causation_id),
        )

    def events(self, type_: str | None = None) -> list[Event]:
        return [e for e in self.store.all(self.session.id) if type_ is None or e.type == type_]

    def payloads(self, type_: str) -> list[Any]:
        return [e.payload for e in self.events(type_)]

    async def wait_for(
        self, type_: str, pred: Callable[[Any], bool] = lambda p: True, limit_s: float = 10.0
    ) -> Any:
        found: list[Any] = []

        async def check() -> bool:
            found[:] = [p for p in self.payloads(type_) if pred(p)]
            return bool(found)

        await until(check, limit_s)
        return found[0]

    def new_supervisor(self) -> Supervisor:
        return Supervisor(
            session=self.session,
            model=self.brain,
            board=self.board,
            events=self.recorder,
            store=self.store,
            bus=self.bus,
            scheduler=self.reassigner,
            control=self.control,
            frames=self.frames,
            config=self.config,
            now=self.clock.now,
            sleep=fast_sleep,
        )

    def start(self) -> None:
        self.supervisor = self.new_supervisor()
        self.task = asyncio.create_task(self.supervisor.run())

    async def stop(self) -> None:
        if self.supervisor is not None and self.task is not None:
            await self.supervisor.stop()
            async with asyncio.timeout(5):
                await self.task
            self.task = None

    async def post(self, author: str, kind: BoardEntryKind, title: str, **kw: Any) -> BoardEntry:
        actor = (
            Actor.user("local") if author == "user" else Actor.agent(self.agent(author).id, author)
        )
        return await self.board.post(
            self.session.id, BoardEntryDraft(kind=kind, title=title, **kw), author=actor
        )

    async def in_review(
        self, key: str, agent: str, *, outputs: dict[str, Any] | None = None
    ) -> Task:
        """Assign, start and submit ``key`` (the events the pool would emit)."""
        task: Task = next(
            e.payload.task
            for e in self.events("task.created")
            if isinstance(e.payload, TaskCreated) and e.payload.task.key == key
        )
        attempt = 1 + sum(1 for p in self.payloads("task.assigned") if p.task_key == key)
        a = self.agent(agent)
        now = utcnow()
        await self.record(
            TaskAssigned(
                task_id=task.id,
                task_key=key,
                agent_id=a.id,
                attempt=attempt,
                ack_deadline=now + timedelta(seconds=10),
                lease_expires_at=now + timedelta(seconds=45),
            )
        )
        await self.record(
            TaskStarted(task_id=task.id, task_key=key, agent_id=a.id, attempt=attempt),
            Actor.agent(a.id, agent),
        )
        await self.record(
            TaskResultSubmitted(
                task_id=task.id,
                task_key=key,
                agent_id=a.id,
                result=TaskResult(summary=f"{key} done", outputs=outputs or {"result": "42"}),
            ),
            Actor.agent(a.id, agent),
        )
        return task


async def light(
    *,
    drafts: Sequence[TaskDraft] | None = None,
    agent_count: int = 3,
    policy: Policy | None = None,
    brain: Brain | None = None,
    config: SupervisorConfig = FAST,
    task_overrides: dict[str, dict[str, Any]] | None = None,
    start: bool = True,
    status: str = "running",
) -> Light:
    """A running session (plan v1 approved, agents and tasks created) and a supervisor."""
    session = make_session(agent_count=agent_count, status="awaiting_approval")
    store = InMemoryEventStore(strict=True)
    bus = InMemoryBus()
    recorder = EventRecorder(store, bus)
    agents: list[Agent] = []
    w = Light(
        session=session,
        store=store,
        bus=bus,
        recorder=recorder,
        board=MemoryBoard(recorder, lambda: agents),
        brain=brain or Brain(policy),
        control=FakeControl(),
        reassigner=FakeReassigner(),
        frames=InMemoryArtifactStore(),
        clock=Clock(),
        config=config,
        agents=agents,
    )
    plan = list(drafts if drafts is not None else make_tasks({"T1": []}))
    await w.record(SessionCreated(session=session))
    await w.record(SessionPlanProposed(plan_version=1, proposal=PlanProposal(tasks=plan)))
    if status == "running":
        await w.record(SessionApproved(plan_version=1))
    for i in range(agent_count):
        name, color = agent_identity(i)
        agent = Agent(id=new_id(), session_id=session.id, name=name, color=color, status="idle")
        agents.append(agent)
        await w.record(AgentCreated(agent=agent))
    for d in plan:
        overrides = (task_overrides or {}).get(d.key, {})
        task = Task(
            **d.model_dump(),
            id=new_id(),
            session_id=session.id,
            plan_version=1,
            **overrides,
        )
        await w.record(TaskCreated(task=task))
    if start:
        w.start()
    return w


# -- world with the real scheduler --------------------------------------------------------


@dataclass
class Behavior:
    steps: int = 3
    fail: bool = False
    latency_s: float = 0.0


_AGENT_RE = re.compile(r"\bYou are ([A-Z][A-Za-z]+)")
_TASK_RE = re.compile(r"## Your task: Task (\S+)")
_STEP_RE = re.compile(r"Step (\d+) of max")


class AgentModel:
    """Agent ``ModelClient``: per task key, ``steps`` shell steps then submit/fail."""

    def __init__(self, behaviors: dict[str, Behavior] | None = None) -> None:
        self.behaviors = dict(behaviors or {})
        self.user_texts: list[tuple[str, str, str]] = []
        """(agent, task key, last user message text) per request."""

    @property
    def name(self) -> str:
        return "agent-model"

    def runs_of(self, key: str) -> int:
        return sum(
            1
            for agent, k, text in self.user_texts
            if k == key and re.search(r"Step 1 of max", text)
        )

    async def complete(self, req: ModelRequest) -> ModelResponse:
        system = req.messages[0].text_content()
        agent_m, task_m = _AGENT_RE.search(system), _TASK_RE.search(system)
        assert agent_m is not None
        assert task_m is not None
        agent, key = agent_m.group(1), task_m.group(1)
        text = [m for m in req.messages if m.role == "user"][-1].text_content()
        self.user_texts.append((agent, key, text))
        step_m = _STEP_RE.search(text)
        step = int(step_m.group(1)) if step_m else 1
        b = self.behaviors.get(key, Behavior())
        if b.latency_s:
            await asyncio.sleep(b.latency_s)
        if b.fail:
            return tool_response("fail_task", reason=f"{key} cannot be done", what_was_tried="x")
        if step < b.steps:
            return tool_response("run_command", cmd=f"echo {key}-{step}")
        return tool_response(
            "submit_result", summary=f"{key} done", outputs={"result": f"{key} by {agent}"}
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=0)

    async def aclose(self) -> None:
        return None


@dataclass
class Full:
    session: Session
    store: InMemoryEventStore
    bus: InMemoryBus
    recorder: EventRecorder
    board: MemoryBoard
    brain: Brain
    agent_model: AgentModel
    scheduler: Scheduler
    supervisor: Supervisor
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def events(self, type_: str | None = None) -> list[Event]:
        return [e for e in self.store.all(self.session.id) if type_ is None or e.type == type_]

    def payloads(self, type_: str) -> list[Any]:
        return [e.payload for e in self.events(type_)]

    async def wait_for(
        self, type_: str, pred: Callable[[Any], bool] = lambda p: True, limit_s: float = 15.0
    ) -> Any:
        found: list[Any] = []

        async def check() -> bool:
            found[:] = [p for p in self.payloads(type_) if pred(p)]
            return bool(found)

        await until(check, limit_s)
        return found[0]

    def agent_id(self, name: str) -> str:
        return next(a.id for a in self.scheduler.state.agents.values() if a.name == name)

    async def stop(self) -> None:
        await self.supervisor.stop()
        if not self.scheduler.finished:
            async with asyncio.timeout(15):
                await self.scheduler.stop("user")
        async with asyncio.timeout(15):
            await asyncio.gather(*self.tasks)


async def full(
    *,
    drafts: Sequence[TaskDraft],
    policy: Policy,
    behaviors: dict[str, Behavior] | None = None,
    agent_count: int = 2,
    config: SupervisorConfig = FAST,
) -> Full:
    """Scheduler (supervisor review mode) + pool + agent runtimes + supervisor, with the
    real ``DigestBuilder`` over the in-memory board."""
    session = make_session(agent_count=agent_count, status="awaiting_approval")
    store = InMemoryEventStore(strict=True)
    bus = InMemoryBus()
    leases = InMemoryLeaseStore()
    recorder = EventRecorder(store, bus)
    holder: list[Scheduler] = []

    def agents() -> Iterable[Agent]:
        return holder[0].state.agents.values() if holder else []

    board = MemoryBoard(recorder, agents)

    async def list_tasks(session_id: str) -> Sequence[Task]:
        return list(holder[0].state.tasks.values())

    digest = DigestBuilder(board, list_tasks)
    agent_model = AgentModel(behaviors)
    sched_config = supervised_scheduler_config(FAST_SCHEDULER)
    budget = SessionBudget(session.budgets)
    frames = InMemoryArtifactStore()

    def factory(
        session: Session, agent: Agent, sandbox: SandboxHandle, deps: RuntimeDeps
    ) -> AgentRuntime:
        return AgentRuntime(
            session=session,
            agent=agent,
            sandbox=sandbox,
            model=agent_model,
            events=recorder,
            bus=bus,
            frames=frames,
            registry=build_agent_registry(),
            config=AgentRuntimeConfig(
                error_backoff_s=0.0, heartbeat_interval_s=0.05, lease_ttl_s=deps.lease_ttl_s
            ),
            state=deps.state,
            control=deps.control,
            budget=deps.budget,
            renew_lease=deps.renew_lease,
            digest=digest,
            board=board,
            sleep=fast_sleep,
        )

    pool = AgentPool(
        session=session,
        provider=FakeSandboxProvider(),
        factory=factory,
        events=recorder,
        bus=bus,
        leases=leases,
        budget=budget,
        lease_ttl_s=sched_config.lease_ttl_s,
        config=FAST_POOL,
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
        config=sched_config,
    )
    holder.append(scheduler)
    brain = Brain(policy)
    supervisor = Supervisor(
        session=session,
        model=brain,
        board=board,
        events=recorder,
        store=store,
        bus=bus,
        scheduler=scheduler,
        control=PoolAgentControl(pool),
        frames=frames,
        config=config,
        sleep=fast_sleep,
    )
    w = Full(session, store, bus, recorder, board, brain, agent_model, scheduler, supervisor)
    actor = Actor.user("local")
    await recorder.record(session.id, EventDraft(SessionCreated(session=session), actor))
    await recorder.record(
        session.id,
        EventDraft(
            SessionPlanProposed(plan_version=1, proposal=PlanProposal(tasks=list(drafts))), actor
        ),
    )
    await recorder.record(session.id, EventDraft(SessionApproved(plan_version=1), actor))
    w.tasks = [asyncio.create_task(scheduler.run()), asyncio.create_task(supervisor.run())]
    return w
