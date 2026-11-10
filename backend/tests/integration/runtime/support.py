"""Fakes for the runtime wiring tests: a "world" of shared infrastructure (event log,
bus, leases, session directory, board, control store, sandboxes) that several
``Runtime`` instances can use one after the other, like processes sharing Postgres
and Redis. No network, no Docker, no real model."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from agentplatform.artifacts.service import ArtifactService
from agentplatform.config import PoliciesConfig, RoleConfig, RoleName
from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.commands import COMMAND_ADAPTER, Command
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
    AgentStatusChanged,
    BoardEntryPosted,
    BoardEntryStatusChanged,
    Event,
    EventDraft,
    EventPayload,
    SessionCreated,
)
from agentplatform.contracts.model_io import ModelRequest, ModelResponse, ModelUsage
from agentplatform.contracts.plan import PlanProposal
from agentplatform.contracts.session import Budgets, Session
from agentplatform.contracts.task import TaskDraft
from agentplatform.errors import BoardRuleError, NotFoundError
from agentplatform.eventing import EventRecorder
from agentplatform.models.base import ModelClient
from agentplatform.runtime.container import Container, InMemoryCommandLog, RuntimeKnobs
from agentplatform.runtime.controls import InMemoryControlStore
from agentplatform.runtime.coordination.visibility import visible_to
from agentplatform.runtime.hitl import HitlApprovalGate, InMemoryApprovalStore
from agentplatform.runtime.main import Runtime
from agentplatform.runtime.policies import Policy
from agentplatform.runtime.pool import PoolConfig
from agentplatform.runtime.readmodels import InMemorySessionDirectory, ProjectingEventStore
from agentplatform.runtime.scheduler import SchedulerConfig
from agentplatform.runtime.supervisor import SupervisorConfig
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

_AGENT_RE = re.compile(r"\bYou are ([A-Z][A-Za-z]+)")
_TASK_RE = re.compile(r"## Your task: Task (\S+)")
_STEP_RE = re.compile(r"Step (\d+) of max")
_TRIGGER_RE = re.compile(r"^- \[(\w+)\] (.*)$", re.MULTILINE)


async def until(check: Callable[[], Awaitable[bool] | bool], limit_s: float = 10.0) -> None:
    async with asyncio.timeout(limit_s):
        while True:
            result = check()
            if result if isinstance(result, bool) else await result:
                return
            await asyncio.sleep(0.01)


def payload[P: EventPayload](event: Event, cls: type[P]) -> P:
    """``event.payload`` narrowed to ``cls``."""
    assert isinstance(event.payload, cls), event.type
    return event.payload


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


# -- models -------------------------------------------------------------------------------


@dataclass
class Behavior:
    steps: int = 1
    """Shell steps before ``submit_result``."""
    latency_s: float = 0.0
    fail: bool = False


class AgentModel:
    """Agent ``ModelClient``: per task key, ``steps`` shell steps, then submit."""

    def __init__(self) -> None:
        self.behaviors: dict[str, Behavior] = {}
        self.requests: list[tuple[str, str, int]] = []
        """(agent name, task key, step) per request."""

    @property
    def name(self) -> str:
        return "agent-model"

    def runs_of(self, key: str) -> int:
        return sum(1 for _, k, step in self.requests if k == key and step == 1)

    async def complete(self, req: ModelRequest) -> ModelResponse:
        system = req.messages[0].text_content()
        agent_m, task_m = _AGENT_RE.search(system), _TASK_RE.search(system)
        assert agent_m is not None
        assert task_m is not None
        text = [m for m in req.messages if m.role == "user"][-1].text_content()
        step_m = _STEP_RE.search(text)
        step = int(step_m.group(1)) if step_m else 1
        key = task_m.group(1)
        self.requests.append((agent_m.group(1), key, step))
        b = self.behaviors.get(key, Behavior())
        if b.latency_s:
            await asyncio.sleep(b.latency_s)
        if b.fail:
            return tool_response("fail_task", reason=f"{key} cannot be done", what_was_tried="x")
        if step < b.steps:
            return tool_response("run_command", cmd=f"echo {key}-{step}")
        return tool_response(
            "submit_result", summary=f"{key} done", outputs={"result": f"{key} by {agent_m[1]}"}
        )

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=0)

    async def aclose(self) -> None:
        return None


class PlannerModel:
    """Planner ``ModelClient``: replies with ``plans`` in order (the last one repeats);
    ``fail`` makes every reply invalid JSON (planner error after the repair call)."""

    def __init__(self, *plans: list[TaskDraft]) -> None:
        self.plans = list(plans) or [make_tasks({"T1": []})]
        self.fail = False
        self.calls = 0

    @property
    def name(self) -> str:
        return "planner-model"

    async def complete(self, req: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.fail:
            return text_response("I cannot plan this.")
        tasks = self.plans[min(self.calls - 1, len(self.plans) - 1)]
        proposal = PlanProposal(tasks=tasks, rationale="test")
        return text_response(json.dumps(proposal.model_dump(mode="json")))

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=0)

    async def aclose(self) -> None:
        return None


class SupervisorModel:
    """Supervisor ``ModelClient``: accepts every submitted result (one criterion per
    task, as ``make_tasks`` builds them); each reply costs ``cost_usd``."""

    def __init__(self, cost_usd: float = 0.0) -> None:
        self.cost_usd = cost_usd
        self.runs = 0

    @property
    def name(self) -> str:
        return "supervisor-model"

    async def complete(self, req: ModelRequest) -> ModelResponse:
        usage = ModelUsage(input_tokens=50, output_tokens=10, cost_usd=self.cost_usd)
        if req.messages[-1].role == "tool":
            return text_response("done").model_copy(update={"usage": usage})
        self.runs += 1
        text = req.messages[1].text_content()
        keys = [
            m.group(1)
            for reason, detail in _TRIGGER_RE.findall(text)
            if reason == "result_submitted" and (m := re.search(r"\b(T\d+)\b", detail))
        ]
        calls: list[tuple[str, dict[str, Any]]] = [
            (
                "accept_result",
                {
                    "task_key": key,
                    "verdicts": [{"criterion": 1, "passed": True, "reason": "checked"}],
                    "why": f"{key} meets its criterion.",
                },
            )
            for key in keys
        ] or [("noop", {"reason": "nothing to do"})]
        if len(calls) == 1:
            resp = tool_response(calls[0][0], **calls[0][1])
        else:
            resp = multi_tool_response(*calls)
        return resp.model_copy(update={"usage": usage})

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=0)

    async def aclose(self) -> None:
        return None


class Models:
    """``ModelSource`` with one fake client per role."""

    def __init__(self) -> None:
        self.agent = AgentModel()
        self.planner = PlannerModel()
        self.supervisor = SupervisorModel()
        self.summarizer = SupervisorModel()

    def client(self, profile: str, role: RoleName) -> ModelClient:
        client: ModelClient = getattr(self, role)
        return client

    def role(self, profile: str, role: RoleName) -> RoleConfig | None:
        return None


# -- board and artifact metadata ----------------------------------------------------------


class MemoryBoard:
    """``BoardService`` over the event log (the rules the runtime relies on)."""

    def __init__(self, recorder: EventRecorder, agents: Callable[[], Iterable[Agent]]) -> None:
        self.recorder = recorder
        self._agents = agents
        self.entries: dict[str, BoardEntry] = {}

    async def post(
        self, session_id: str, draft: BoardEntryDraft, *, author: Actor, step: int | None = None
    ) -> BoardEntry:
        if draft.kind == "decision" and author.type not in ("supervisor", "user"):
            raise BoardRuleError("only the supervisor or the user may post a decision")
        if draft.kind == "artifact" and draft.artifact_id is None:
            raise BoardRuleError("artifact entries need artifact_id")
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

        await self.recorder.record(session_id, factory)
        self.entries[holder[-1].id] = holder[-1]
        return holder[-1]

    async def set_status(
        self, entry_id: str, status: BoardEntryStatus, *, actor: Actor, reason: str | None = None
    ) -> BoardEntry:
        entry = await self.get(entry_id)
        updated = entry.model_copy(update={"status": status})
        self.entries[entry_id] = updated
        await self.recorder.record(
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
            if e.session_id == session_id
            and (kinds is None or e.kind in kinds)
            and (tags is None or set(tags) & set(e.tags))
            and (query is None or query.lower() in f"{e.title} {e.body}".lower())
            and (viewer_agent_id is None or visible_to(e, viewer_agent_id))
        ]
        return out[:limit]

    async def get_by_seq(self, session_id: str, seq: int) -> BoardEntry:
        for e in self.entries.values():
            if e.session_id == session_id and e.seq == seq:
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
            if e.session_id == session_id
            and e.seq > after_seq
            and (viewer_agent_id is None or visible_to(e, viewer_agent_id))
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


class MemoryArtifactMetadata:
    def __init__(self) -> None:
        self.artifacts: dict[str, Artifact] = {}

    async def get(self, artifact_id: str) -> Artifact:
        try:
            return self.artifacts[artifact_id]
        except KeyError:
            raise NotFoundError(f"artifact {artifact_id} does not exist") from None

    async def latest(self, session_id: str, name: str) -> Artifact | None:
        found = [
            a for a in self.artifacts.values() if a.session_id == session_id and a.name == name
        ]
        return max(found, key=lambda a: a.version) if found else None

    async def insert(self, artifact: Artifact, tx: object) -> None:
        self.artifacts[artifact.id] = artifact


# -- the world ----------------------------------------------------------------------------

FAST_KNOBS = dict(
    session_lease_ttl_s=1.0,
    session_lease_renew_s=0.1,
    command_dequeue_timeout_s=0.05,
    planning_sweep_s=0.3,
    sweep_interval_s=0.05,
    orphan_sweep_s=3600.0,
    scheduler=SchedulerConfig(tick_s=0.02, ack_timeout_s=2.0, lease_ttl_s=1.0, stop_grace_s=2.0),
    pool=PoolConfig(
        fence_grace_s=0.2, recovery_backoff_s=0.01, stop_grace_s=2.0, dequeue_timeout_s=0.05
    ),
    supervisor=SupervisorConfig(
        debounce_s=0.05, tick_s=0.02, model_backoff_s=(), requeue_after_s=0.1
    ),
    agent_config={"error_backoff_s": 0.0, "heartbeat_interval_s": 0.05},
    agent_sleep=fast_sleep,
)


@dataclass
class World:
    """Shared infrastructure; ``runtime()`` starts a new runtime "process" on it."""

    directory: InMemorySessionDirectory = field(default_factory=InMemorySessionDirectory)
    bus: InMemoryBus = field(default_factory=InMemoryBus)
    leases: InMemoryLeaseStore = field(default_factory=InMemoryLeaseStore)
    control_store: InMemoryControlStore = field(default_factory=InMemoryControlStore)
    command_log: InMemoryCommandLog = field(default_factory=InMemoryCommandLog)
    provider: FakeSandboxProvider = field(default_factory=FakeSandboxProvider)
    approvals: InMemoryApprovalStore = field(default_factory=InMemoryApprovalStore)
    frames: InMemoryArtifactStore = field(default_factory=InMemoryArtifactStore)
    metadata: MemoryArtifactMetadata = field(default_factory=MemoryArtifactMetadata)
    models: Models = field(default_factory=Models)
    policies: PoliciesConfig = field(default_factory=PoliciesConfig)
    raw: InMemoryEventStore = field(default_factory=lambda: InMemoryEventStore(strict=True))
    runtimes: list[Runtime] = field(default_factory=list)
    knobs: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.store = ProjectingEventStore(self.raw, self.directory.project)
        self.recorder = EventRecorder(self.store, self.bus)
        self.board = MemoryBoard(self.recorder, self._agents)

    def _agents(self) -> list[Agent]:
        out: list[Agent] = []
        for sid in list(self.directory.known):
            out.extend(
                payload(e, AgentCreated).agent for e in self.raw.of_type(sid, "agent.created")
            )
        return out

    async def list_tasks(self, session_id: str) -> list[Any]:
        return []

    def container(self, runtime_id: str, **knobs: Any) -> Container:
        gate = HitlApprovalGate(events=self.recorder, store=self.approvals, board=self.board)
        return Container(
            store=self.store,
            bus=self.bus,
            leases=self.leases,
            recorder=self.recorder,
            directory=self.directory,
            frames=self.frames,
            board=self.board,
            task_lister=self.list_tasks,
            approvals=gate,
            control_store=self.control_store,
            command_log=self.command_log,
            models=self.models,
            providers={"docker_local": self.provider},
            policy=Policy.from_config(self.policies),
            policies=self.policies,
            knobs=RuntimeKnobs(runtime_id=runtime_id, **{**FAST_KNOBS, **self.knobs, **knobs}),
            artifacts=ArtifactService(self.metadata, self.recorder),
            closers=[gate.aclose],
        )

    async def runtime(self, runtime_id: str = "rt-a", **knobs: Any) -> Runtime:
        rt = Runtime(self.container(runtime_id, **knobs))
        await rt.start()
        self.runtimes.append(rt)
        return rt

    async def close(self) -> None:
        for rt in self.runtimes:
            await rt.shutdown()
            await rt.container.aclose()

    # -- sessions and commands --------------------------------------------------------

    async def create_session(
        self, *, agent_count: int = 1, budgets: Budgets | None = None, **kw: Any
    ) -> Session:
        session = make_session(agent_count=agent_count, budgets=budgets, **kw)
        await self.recorder.record(
            session.id, EventDraft(payload=SessionCreated(session=session), actor=Actor.user())
        )
        return session

    def command(self, type_: str, session_id: str, **fields: Any) -> Command:
        return COMMAND_ADAPTER.validate_python(
            {
                "type": type_,
                "id": new_id(),
                "session_id": session_id,
                "issued_at": utcnow(),
                "issued_by": Actor.user().model_dump(),
                **fields,
            }
        )

    async def send(self, type_: str, session_id: str, **fields: Any) -> Command:
        from agentplatform.api.commands import command_queue

        cmd = self.command(type_, session_id, **fields)
        await self.bus.enqueue(command_queue(cmd), cmd)
        return cmd

    # -- queries ----------------------------------------------------------------------

    def events(self, session_id: str, type_: str | None = None) -> list[Event]:
        return [e for e in self.raw.all(session_id) if type_ is None or e.type == type_]

    async def wait_event(
        self,
        session_id: str,
        type_: str,
        pred: Callable[[Event], bool] = lambda e: True,
        limit_s: float = 10.0,
    ) -> Event:
        found: list[Event] = []

        def check() -> bool:
            found[:] = [e for e in self.events(session_id, type_) if pred(e)]
            return bool(found)

        await until(check, limit_s)
        return found[0]

    def agent_ids(self, session_id: str) -> list[str]:
        return [payload(e, AgentCreated).agent.id for e in self.events(session_id, "agent.created")]

    def agent_status(self, session_id: str, agent_id: str) -> str | None:
        status: str | None = None
        for e in self.events(session_id):
            p = e.payload
            if isinstance(p, AgentStatusChanged) and p.agent_id == agent_id:
                status = p.status
            elif e.type == "agent.stopped" and getattr(p, "agent_id", None) == agent_id:
                status = "stopped"
        return status
