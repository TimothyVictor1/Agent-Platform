"""AgentRuntime: the step loop of one agent running one task at a time (SPEC §10).

The runtime is coded only against interfaces, so the scheduler (T11), runtime
wiring (T13), coordination (T14) and human-in-the-loop (T18) plug in without
changing this module:

* ``ModelClient``, ``SandboxHandle``, ``ArtifactStore`` (frames), ``ToolRegistry``;
* ``EventSink`` (``EventRecorder`` satisfies it) and ``EphemeralPublisher``
  (``EventBus`` satisfies it);
* the Protocols defined here, each with an in-process default: ``ControlState``
  (``LocalControlState``), ``DigestProvider`` (``NoDigest``), ``ApprovalGate``
  (none: ``request_approval`` returns an error), ``BudgetMonitor`` (``LocalBudget``),
  ``RosterProvider`` (just this agent), plus a ``renew_lease`` callback.

Event ownership (see docs/agent-loop.md): the loop emits only ``agent.*`` events
(``agent.step``, ``agent.status_changed``, ``agent.stuck_detected``,
``agent.error``) and bus-only heartbeats/thumbnails. It returns a ``TaskOutcome``;
the caller emits ``task.started``, ``task.result_submitted`` and ``task.failed`` and
decides about retries. ``agent.stopped`` (agent teardown) is also the caller's.

Concurrency and failure rules:

* A stop (or a lost lease) interrupts the model call and any wait immediately, so
  the loop exits within one step.
* Pause/takeover take effect at the next checkpoint. The control state is checked
  again right before any tool runs: an action computed while the user took over is
  discarded, never executed on the user's screen.
* Tool failures never crash the loop (the registry turns them into ``ERROR: ...``).
  Model failures and transient sandbox failures emit ``agent.error`` and are retried
  with a back-off; after ``max_consecutive_*`` failures the task fails. An expired
  sandbox fails the task with ``sandbox_lost`` at once.
* Event store failures propagate: the caller treats them like a crash (the task
  lease then expires and the scheduler requeues the task).
"""

from __future__ import annotations

import asyncio
import base64
import io
import time
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any, Literal, Protocol, TypeVar, cast, runtime_checkable

import structlog
from PIL import Image
from pydantic import JsonValue

from agentplatform.artifacts.base import ArtifactStore, frame_key
from agentplatform.config import RoleConfig
from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Approval, Frame
from agentplatform.contracts.common import (
    Actor,
    AgentStatus,
    CoordinateMode,
    RiskLevel,
    new_id,
    utcnow,
)
from agentplatform.contracts.events import (
    ActionRecord,
    AgentError,
    AgentStatusChanged,
    AgentStep,
    AgentStuckDetected,
    EphemeralMessage,
    Event,
    EventDraft,
    EventPayload,
    Heartbeat,
    Thumbnail,
)
from agentplatform.contracts.model_io import (
    ImagePart,
    Message,
    ModelRequest,
    ModelUsage,
    ToolCall,
    ToolSpec,
)
from agentplatform.contracts.session import Budgets, Session, Usage
from agentplatform.contracts.task import Task, TaskResult
from agentplatform.errors import LeaseLostError, SandboxError, SandboxExpiredError
from agentplatform.models.base import ModelClient
from agentplatform.runtime.agent.actions import (
    FallbackParser,
    Observation,
    normalize_tool_calls,
    prepare_observation,
)
from agentplatform.runtime.agent.context import (
    RESUME_NOTE,
    ContextBuilder,
    ContextSettings,
    RosterEntry,
    StepRecord,
    Summarizer,
    UpstreamInputs,
    describe_call,
    step_line,
)
from agentplatform.runtime.agent.stuck import (
    StepSignals,
    StuckConfig,
    StuckDetector,
    StuckSignal,
    phash_distance,
)
from agentplatform.runtime.agent.tools.control import register_control_tools
from agentplatform.runtime.agent.tools.desktop import register_desktop_tools
from agentplatform.runtime.agent.tools.registry import (
    MAX_RESULT_CHARS,
    TaskGaveUp,
    TaskSubmitted,
    ToolRegistry,
    ToolResult,
)
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.sandboxes.base import SandboxHandle

__all__ = [
    "NO_TOOL_CORRECTION",
    "PROGRESS_TOOLS",
    "AgentRuntime",
    "AgentRuntimeConfig",
    "AgentState",
    "ApprovalGate",
    "BudgetMonitor",
    "ControlMode",
    "ControlState",
    "Digest",
    "DigestProvider",
    "EphemeralPublisher",
    "EventSink",
    "FailureReason",
    "LeaseRenewer",
    "LocalBudget",
    "LocalControlState",
    "LoopHooks",
    "NoDigest",
    "RosterProvider",
    "TaskOutcome",
    "ToolContext",
    "build_agent_registry",
]

log = structlog.get_logger(__name__)

T = TypeVar("T")

ControlMode = Literal["run", "pause", "takeover", "stop"]
FailureReason = Literal[
    "fail_task", "budget", "model_error", "sandbox_lost", "sandbox_error", "lease_lost"
]
LeaseRenewer = Callable[[], Awaitable[bool]]
"""Renews the task lease; returns False (or raises ``LeaseLostError``) if it was lost.
Any other exception is not proof that the lease is still held: see ``_heartbeat``."""

NO_TOOL_CORRECTION = (
    "You did not call a tool. Respond only with tool calls: up to 2 coordination calls, "
    "then exactly one desktop action (or a control tool such as submit_result) as the "
    "last call. Do not answer in plain text."
)
PROGRESS_TOOLS = frozenset({"post", "answer", "message", "release_claim", "publish_artifact"})
"""Coordination tools whose success counts as progress for the stuck detector."""
ARG_STRING_LIMIT = 200


# -- interfaces ---------------------------------------------------------------------------


@runtime_checkable
class ControlState(Protocol):
    """Per-agent control state (SPEC §10.7). T13 backs it with a Redis key plus an
    in-process event; ``LocalControlState`` is the in-process implementation."""

    @property
    def mode(self) -> ControlMode:
        """Current mode: ``run``, ``pause``, ``takeover`` or ``stop``."""
        ...

    async def wait_for_change(self, current: ControlMode) -> ControlMode:
        """Return as soon as the mode differs from ``current`` (immediately if it
        already does). Must be cancellation-safe."""
        ...


class LocalControlState:
    """In-process ``ControlState``. ``set`` wakes every waiter."""

    def __init__(self, mode: ControlMode = "run") -> None:
        self._mode: ControlMode = mode
        self._changed = asyncio.Event()

    @property
    def mode(self) -> ControlMode:
        return self._mode

    def set(self, mode: ControlMode) -> None:
        if mode == self._mode:
            return
        self._mode = mode
        event, self._changed = self._changed, asyncio.Event()
        event.set()

    async def wait_for_change(self, current: ControlMode) -> ControlMode:
        while self._mode == current:
            await self._changed.wait()
        return self._mode


@dataclass(frozen=True, slots=True)
class Digest:
    """Inbox digest for one step. ``cursor`` = max board seq considered."""

    text: str = ""
    cursor: int = 0


@runtime_checkable
class DigestProvider(Protocol):
    """Builds the inbox digest (SPEC §13.2). Implemented by T14."""

    async def build(self, agent: Agent, task: Task, *, since: int, budget_tokens: int) -> Digest:
        """Digest of board entries with ``seq > since`` visible to ``agent``, at most
        ``budget_tokens`` (approx.), highest priority first. Return
        ``Digest("", since)`` when there is nothing new."""
        ...


class NoDigest:
    """Default ``DigestProvider``: always empty (also used in ``parallel_isolated``)."""

    async def build(self, agent: Agent, task: Task, *, since: int, budget_tokens: int) -> Digest:
        return Digest("", since)


@runtime_checkable
class ApprovalGate(Protocol):
    """Approval workflow (SPEC §15.2). Implemented by T18."""

    async def request(
        self,
        *,
        session_id: str,
        agent_id: str,
        task_key: str | None,
        action_summary: str,
        risk: RiskLevel,
        frame_id: str | None,
    ) -> Approval:
        """Create the approval (emitting ``approval.requested``), wait until it is
        decided or expires (15 minutes) and return it with its final status
        (``approved``/``denied``/``expired``). Must be cancellation-safe: a cancelled
        wait leaves the approval pending or expires it."""
        ...


@runtime_checkable
class BudgetMonitor(Protocol):
    """Session-wide budget accounting (SPEC §12.8). T11 provides a shared one."""

    def record(self, usage: Usage) -> None:
        """Add the usage of one step (``usage.steps == 1``)."""
        ...

    def exceeded(self) -> str | None:
        """A short description of the exceeded budget, or ``None``."""
        ...


class LocalBudget:
    """In-process ``BudgetMonitor`` over a session's ``Budgets`` (total steps,
    tokens, cost, wall time). ``max_steps_per_task`` is enforced by the loop."""

    def __init__(
        self,
        budgets: Budgets,
        *,
        usage: Usage | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budgets = budgets
        self.usage = usage or Usage()
        self._clock = clock
        self._started = clock()

    def record(self, usage: Usage) -> None:
        self.usage = self.usage.plus(usage)

    def exceeded(self) -> str | None:
        b, u = self.budgets, self.usage
        if u.steps >= b.max_total_steps:
            return f"max_total_steps ({b.max_total_steps}) reached"
        if b.max_tokens is not None and u.input_tokens + u.output_tokens >= b.max_tokens:
            return f"max_tokens ({b.max_tokens}) reached"
        if b.max_cost_usd is not None and u.cost_usd >= b.max_cost_usd:
            return f"max_cost_usd ({b.max_cost_usd}) reached"
        if (self._clock() - self._started) / 60 >= b.max_wall_minutes:
            return f"max_wall_minutes ({b.max_wall_minutes}) reached"
        return None


@runtime_checkable
class RosterProvider(Protocol):
    """Current team roster (names and task titles) for the system prompt."""

    async def roster(self) -> Sequence[RosterEntry]: ...


class EventSink(Protocol):
    """Appends and publishes a persisted event (``EventRecorder``)."""

    async def record(self, session_id: str, draft: EventDraft) -> Event: ...


class EphemeralPublisher(Protocol):
    """Bus-only messages (``EventBus.publish_ephemeral``)."""

    async def publish_ephemeral(self, message: EphemeralMessage) -> None: ...


class LoopHooks(Protocol):
    """What tool handlers may ask of the running loop."""

    async def set_status(self, status: AgentStatus, reason: str | None = None) -> None:
        """Change the agent status (``working`` maps back to ``stuck`` while a stuck
        episode is open)."""
        ...

    async def wait_or_stop(self, aw: Awaitable[T]) -> T | None:
        """Await ``aw`` unless a stop (or lease loss) comes first; then cancel it
        and return ``None``."""
        ...

    async def sleep(self, seconds: float) -> None:
        """Sleep (interrupted by a stop)."""
        ...

    def add_note(self, text: str) -> None:
        """Add a system note to the next step's user message."""
        ...


@dataclass(slots=True)
class AgentState:
    """Per-agent state that survives across tasks."""

    digest_cursor: int = 0
    notebook: str = ""
    """Scratchpad written by ``notebook_write`` (T14), shown every step (1500 chars)."""
    total_steps: int = 0
    status: AgentStatus = "idle"


@dataclass(slots=True)
class ToolContext:
    """Handed to every tool handler for one step. Stable API for T14/T15 tools."""

    session_id: str
    agent: Agent
    task: Task
    step: int
    state: AgentState
    sandbox: SandboxHandle
    observation: Observation
    screenshot_png: bytes
    coordinate_mode: CoordinateMode
    hooks: LoopHooks
    frame_id: str | None = None
    board: BoardService | None = None
    artifacts: ArtifactStore | None = None
    approvals: ApprovalGate | None = None

    @property
    def actor(self) -> Actor:
        return Actor.agent(self.agent.id, self.agent.name)


@dataclass(frozen=True, slots=True)
class TaskOutcome:
    """How ``run_task`` ended. ``status``: ``submitted`` (``result`` set),
    ``failed`` (``reason`` set) or ``stopped`` (user stop)."""

    status: Literal["submitted", "failed", "stopped"]
    result: TaskResult | None = None
    reason: FailureReason | None = None
    detail: str = ""
    steps: int = 0
    usage: Usage = field(default_factory=Usage)


@dataclass(frozen=True, slots=True)
class AgentRuntimeConfig:
    coordinate_mode: CoordinateMode = "absolute"
    screenshot_target_width: int = 1280
    jpeg_quality: int = 70
    frame_width: int = 480
    frame_quality: int = 50
    keep_full_frames: bool = False
    max_output_tokens: int = 1024
    temperature: float = 0.0
    model_timeout_s: float = 120.0
    context: ContextSettings = field(default_factory=ContextSettings)
    stuck: StuckConfig = field(default_factory=StuckConfig)
    digest_budget_tokens: int = 700
    max_coordination_calls: int = 2
    submit_hidden_steps: int = 3
    max_no_tool_retries: int = 2
    max_consecutive_model_errors: int = 3
    max_consecutive_sandbox_errors: int = 3
    error_backoff_s: float = 2.0
    heartbeat_interval_s: float = 10.0
    heartbeat_publish_timeout_s: float = 2.0
    """Bound on every bus-only publish (heartbeats and thumbnails)."""
    lease_renew_timeout_s: float = 5.0
    lease_ttl_s: float | None = None
    """TTL the caller renews the task lease with. ``None`` (unknown): the first
    failed renewal (exception or timeout) ends the task with ``lease_lost``."""
    publish_thumbnails: bool = True

    @classmethod
    def from_role(
        cls, role: RoleConfig, *, keep_full_frames: bool = False, **overrides: Any
    ) -> AgentRuntimeConfig:
        """Config from the profile's ``agent`` role (SPEC §8.2)."""
        base = cls(
            coordinate_mode=role.coordinate_mode,
            screenshot_target_width=role.screenshot_target_width,
            keep_full_frames=keep_full_frames,
            max_output_tokens=role.max_output_tokens,
            temperature=role.temperature,
            model_timeout_s=role.timeout_s,
            context=ContextSettings(
                max_images_in_context=role.max_images_in_context,
                prompt_budget_tokens=role.effective_prompt_budget,
                image_tokens=role.image_tokens,
            ),
        )
        return replace(base, **overrides)


def build_agent_registry() -> ToolRegistry:
    """A registry with the desktop and control tools. T13 adds coordination and
    artifact tools (T14/T15) before handing it to ``AgentRuntime``."""
    registry = ToolRegistry()
    register_desktop_tools(registry)
    register_control_tools(registry)
    return registry


# -- internals ----------------------------------------------------------------------------


@dataclass(slots=True)
class _TaskRun:
    task: Task
    builder: ContextBuilder
    detector: StuckDetector
    started: float
    step: int = 0
    usage: Usage = field(default_factory=Usage)
    notes: list[str] = field(default_factory=list)
    pending_image: bytes | None = None
    model_errors: int = 0
    sandbox_errors: int = 0
    last_phash: str | None = None


@dataclass(slots=True)
class _ModelTurn:
    calls: list[ToolCall]
    text: str | None
    usage: ModelUsage
    latency_ms: int


@dataclass(slots=True)
class _SavedFrame:
    id: str
    small_key: str
    full_key: str | None
    small_jpeg: bytes


class _Stopped:
    pass


_STOPPED = _Stopped()


def _encode_jpeg(png: bytes, width: int | None, quality: int) -> bytes:
    with Image.open(io.BytesIO(png)) as img:
        rgb = img.convert("RGB")
    if width is not None and width < rgb.width:
        rgb = rgb.resize(
            (width, max(1, round(rgb.height * width / rgb.width))), Image.Resampling.LANCZOS
        )
    out = io.BytesIO()
    rgb.save(out, format="JPEG", quality=quality)
    return out.getvalue()


def _jpeg_size(data: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as img:
        return img.size


def _record_value(value: Any) -> JsonValue:
    if isinstance(value, str):
        return value if len(value) <= ARG_STRING_LIMIT else value[: ARG_STRING_LIMIT - 1] + "…"
    if isinstance(value, dict):
        return {str(k): _record_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_record_value(v) for v in value]
    return cast(JsonValue, value)


def _action_record(call: ToolCall, result: ToolResult, latency_ms: int | None) -> ActionRecord:
    args = {k: _record_value(v) for k, v in call.arguments.items()}
    return ActionRecord(
        tool=call.name,
        args=args,
        result=result.text[:MAX_RESULT_CHARS],
        is_error=result.is_error,
        latency_ms=latency_ms,
    )


def _not_executed(reason: str) -> ToolResult:
    return ToolResult(text=f"ERROR: not executed: {reason}"[:MAX_RESULT_CHARS], is_error=True)


# -- the runtime --------------------------------------------------------------------------


class AgentRuntime:
    """Runs tasks for one agent, one at a time. See the module docstring."""

    def __init__(
        self,
        *,
        session: Session,
        agent: Agent,
        sandbox: SandboxHandle,
        model: ModelClient,
        events: EventSink,
        bus: EphemeralPublisher,
        frames: ArtifactStore,
        registry: ToolRegistry | None = None,
        config: AgentRuntimeConfig | None = None,
        state: AgentState | None = None,
        control: ControlState | None = None,
        digest: DigestProvider | None = None,
        approvals: ApprovalGate | None = None,
        board: BoardService | None = None,
        budget: BudgetMonitor | None = None,
        roster: RosterProvider | None = None,
        summarizer: Summarizer | None = None,
        fallback_parser: FallbackParser | None = None,
        renew_lease: LeaseRenewer | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        isolated = session.mode == "parallel_isolated"
        self.session = session
        self.agent = agent
        self.config = config or AgentRuntimeConfig()
        self.state = state or AgentState()
        self.control: ControlState = control or LocalControlState()
        self.registry = (registry or build_agent_registry()).for_mode(session.mode)
        self._sandbox = sandbox
        self._model = model
        self._events = events
        self._bus = bus
        self._frames = frames
        self._digest: DigestProvider = NoDigest() if isolated or digest is None else digest
        self._approvals = approvals
        self._board = None if isolated else board
        self._budget: BudgetMonitor = budget or LocalBudget(session.budgets, clock=clock)
        self._roster = roster
        self._summarizer = summarizer
        self._fallback = fallback_parser
        self._renew_lease = renew_lease
        self._clock = clock
        self._sleep_fn = sleep
        self._actor = Actor.agent(agent.id, agent.name)
        self._run: _TaskRun | None = None
        self._lease_lost = asyncio.Event()
        self._log = log.bind(session_id=session.id, agent_id=agent.id)

    # -- public ---------------------------------------------------------------------

    async def run_task(self, task: Task, upstream: UpstreamInputs | None = None) -> TaskOutcome:
        """Run ``task`` until it is submitted, fails or is stopped. Never raises for
        model, tool or sandbox problems (see the module docstring)."""
        if self._run is not None:
            raise RuntimeError(f"agent {self.agent.name} is already running a task")
        cfg = self.config
        builder = ContextBuilder(
            agent_name=self.agent.name,
            team_size=self.session.agent_count,
            task=task,
            upstream=upstream or UpstreamInputs(),
            coordinate_mode=cfg.coordinate_mode,
            screen_size=self._sandbox.screen_size,
            screenshot_target_width=cfg.screenshot_target_width,
            max_steps=self.session.budgets.max_steps_per_task,
            settings=cfg.context,
            summarizer=self._summarizer,
        )
        run = _TaskRun(
            task=task, builder=builder, detector=StuckDetector(cfg.stuck), started=self._clock()
        )
        self._run = run
        self._lease_lost = asyncio.Event()
        self._log = log.bind(session_id=self.session.id, agent_id=self.agent.id, task_key=task.key)
        heartbeat = asyncio.create_task(self._heartbeat(task), name=f"heartbeat-{self.agent.id}")
        try:
            await self.set_status("working")
            outcome = await self._loop(run)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat
            self._run = None
        if outcome.status == "stopped":
            await self.set_status("stopped", reason="stopped by user")
        elif outcome.reason == "lease_lost":
            await self.set_status("error", reason="task lease lost")
        else:
            await self.set_status("idle")
        self._log.info(
            "task_finished", status=outcome.status, reason=outcome.reason, steps=outcome.steps
        )
        return outcome

    # -- LoopHooks ------------------------------------------------------------------

    async def set_status(self, status: AgentStatus, reason: str | None = None) -> None:
        run = self._run
        if status == "working" and run is not None and run.detector.episode is not None:
            status = "stuck"
        previous = self.state.status
        if status == previous:
            return
        self.state.status = status
        await self._emit(
            AgentStatusChanged(
                agent_id=self.agent.id, status=status, previous=previous, reason=reason
            )
        )

    async def wait_or_stop(self, aw: Awaitable[T]) -> T | None:
        work: asyncio.Future[T] = asyncio.ensure_future(aw)
        if self._should_abort():
            work.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await work
            return None
        stop = asyncio.create_task(self._abort_signal())
        racers: set[asyncio.Future[Any]] = {work, stop}
        try:
            done, _ = await asyncio.wait(racers, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (work, stop):
                if not t.done():
                    t.cancel()
            await asyncio.gather(work, stop, return_exceptions=True)
        if work in done:
            return work.result()
        return None

    async def sleep(self, seconds: float) -> None:
        await self.wait_or_stop(self._sleep_fn(seconds))

    def add_note(self, text: str) -> None:
        if self._run is not None:
            self._run.notes.append(text)

    # -- loop -----------------------------------------------------------------------

    def _should_abort(self) -> bool:
        return self.control.mode == "stop" or self._lease_lost.is_set()

    async def _abort_signal(self) -> None:
        async def until_stop() -> None:
            mode = self.control.mode
            while mode != "stop":
                mode = await self.control.wait_for_change(mode)

        waiters = [
            asyncio.create_task(until_stop()),
            asyncio.create_task(self._lease_lost.wait()),
        ]
        try:
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for w in waiters:
                w.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)

    def _outcome(
        self,
        run: _TaskRun,
        status: Literal["submitted", "failed", "stopped"],
        *,
        result: TaskResult | None = None,
        reason: FailureReason | None = None,
        detail: str = "",
    ) -> TaskOutcome:
        return TaskOutcome(
            status=status,
            result=result,
            reason=reason,
            detail=detail,
            steps=run.step,
            usage=run.usage,
        )

    def _aborted(self, run: _TaskRun) -> TaskOutcome:
        """Outcome after ``wait_or_stop`` was interrupted: lease loss wins over stop,
        because the scheduler must requeue the task."""
        if self._lease_lost.is_set():
            return self._outcome(run, "failed", reason="lease_lost", detail="task lease lost")
        return self._outcome(run, "stopped", detail="stopped by user")

    async def _loop(self, run: _TaskRun) -> TaskOutcome:
        max_steps = self.session.budgets.max_steps_per_task
        while True:
            if await self._checkpoint(run):
                return self._outcome(run, "stopped", detail="stopped by user")
            if self._lease_lost.is_set():
                return self._outcome(run, "failed", reason="lease_lost", detail="task lease lost")
            if run.step >= max_steps:
                detail = f"max_steps_per_task ({max_steps}) reached"
                return self._outcome(run, "failed", reason="budget", detail=detail)
            exceeded = self._budget.exceeded()
            if exceeded:
                return self._outcome(run, "failed", reason="budget", detail=exceeded)
            outcome = await self._step(run)
            if outcome is not None:
                return outcome

    async def _checkpoint(self, run: _TaskRun) -> bool:
        """Handle pause/takeover/stop between steps. True means stop."""
        mode = self.control.mode
        if mode == "stop":
            return True
        if mode not in ("pause", "takeover"):
            return False
        await self.set_status("paused_by_user", reason=mode)
        while mode in ("pause", "takeover"):
            # also wakes on lease loss, so a paused agent never hangs on a dead task
            new = await self.wait_or_stop(self.control.wait_for_change(mode))
            if new is None:
                return self.control.mode == "stop"  # lease lost -> False -> lease_lost
            mode = new
        if mode == "stop":
            return True
        run.notes.append(RESUME_NOTE)
        await self.set_status("working")
        return False

    async def _step(self, run: _TaskRun) -> TaskOutcome | None:
        cfg = self.config
        task = run.task
        step = run.step + 1
        global_step = self.state.total_steps + 1

        # 1. observe
        try:
            png = await self._sandbox.screenshot()
            obs = await asyncio.to_thread(
                prepare_observation, png, cfg.screenshot_target_width, cfg.jpeg_quality
            )
        except SandboxExpiredError as exc:
            await self._emit(
                AgentError(agent_id=self.agent.id, error=exc.detail[:500], recoverable=False)
            )
            return self._outcome(run, "failed", reason="sandbox_lost", detail=exc.detail[:500])
        except SandboxError as exc:
            run.sandbox_errors += 1
            give_up = run.sandbox_errors >= cfg.max_consecutive_sandbox_errors
            await self._emit(
                AgentError(agent_id=self.agent.id, error=exc.detail[:500], recoverable=not give_up)
            )
            if give_up:
                return self._outcome(run, "failed", reason="sandbox_error", detail=exc.detail[:500])
            await self.sleep(cfg.error_backoff_s)
            return None
        run.sandbox_errors = 0
        if run.last_phash is not None and run.builder.history:
            changed = phash_distance(run.last_phash, obs.phash) > cfg.stuck.unchanged_max_distance
            run.builder.mark_screen_changed(changed)
        frame = await self._save_frame(png, global_step)

        # 2. context
        digest = await self._build_digest(task)
        await self._refresh_roster(run)
        minutes = (self._clock() - run.started) / 60
        header = run.builder.header(step, minutes, run.notes)
        image = ImagePart(jpeg_bytes=obs.jpeg, width=obs.width, height=obs.height)
        extra: ImagePart | None = None
        if run.pending_image is not None:
            w, h = _jpeg_size(run.pending_image)
            extra = ImagePart(jpeg_bytes=run.pending_image, width=w, height=h)
        hidden = frozenset({"submit_result"}) if step <= cfg.submit_hidden_steps else frozenset()
        specs = self.registry.specs(hidden=hidden)
        messages = run.builder.build(
            header=header,
            image=image,
            digest=digest.text,
            notebook=self.state.notebook,
            extra_image=extra,
        )

        # 3. model
        try:
            turn = await self._ask_model(messages, specs)
        except Exception as exc:
            run.model_errors += 1
            give_up = run.model_errors >= cfg.max_consecutive_model_errors
            detail = getattr(exc, "detail", None) or type(exc).__name__
            self._log.warning("model_call_failed", error=detail, attempt=run.model_errors)
            await self._emit(
                AgentError(
                    agent_id=self.agent.id, error=f"model: {detail}"[:500], recoverable=not give_up
                )
            )
            if give_up:
                return self._outcome(run, "failed", reason="model_error", detail=str(detail)[:500])
            await self.sleep(cfg.error_backoff_s)
            return None
        if isinstance(turn, _Stopped):
            return self._aborted(run)
        run.model_errors = 0

        # the step happened: commit counters
        run.step = step
        self.state.total_steps = global_step
        run.notes.clear()
        run.pending_image = None
        step_usage = Usage(
            steps=1,
            input_tokens=turn.usage.input_tokens,
            output_tokens=turn.usage.output_tokens,
            images=turn.usage.images,
            cost_usd=turn.usage.cost_usd,
        )
        run.usage = run.usage.plus(step_usage)
        self._budget.record(step_usage)

        # 4. execute
        ctx = ToolContext(
            session_id=self.session.id,
            agent=self.agent,
            task=task,
            step=step,
            state=self.state,
            sandbox=self._sandbox,
            observation=obs,
            screenshot_png=png,
            coordinate_mode=cfg.coordinate_mode,
            hooks=self,
            frame_id=frame.id if frame else None,
            board=self._board,
            artifacts=self._frames,
            approvals=self._approvals,
        )
        calls = turn.calls
        results: list[ToolResult] = []
        latencies: list[int | None] = []
        executed: list[bool] = []
        stuck_signal: StuckSignal | None = None
        if not calls:
            stuck_signal = run.detector.report_no_tool(
                f"no tool call after {cfg.max_no_tool_retries} corrective retries"
            )
        else:
            problem = self._order_problem(calls)
            for call in calls:
                if problem is None and self.control.mode != "run":
                    problem = "the user paused or stopped you before this action ran"
                if problem is not None:
                    results.append(_not_executed(problem))
                    latencies.append(None)
                    executed.append(False)
                    continue
                t0 = time.perf_counter()
                res = await self.registry.execute(call, ctx, hidden=hidden)
                results.append(res)
                latencies.append(int((time.perf_counter() - t0) * 1000))
                executed.append(True)

        # 5. outcome of terminal call
        # The step's action is its (first) desktop/control call. It only ran if the
        # composition was valid, in which case it is also the last call.
        terminal_idx = next(
            (
                i
                for i, c in enumerate(calls)
                if self.registry.kind_of(c.name) in ("desktop", "control")
            ),
            None,
        )
        if terminal_idx is None and calls and self.registry.kind_of(calls[-1].name) is None:
            terminal_idx = len(calls) - 1  # unknown tool in the action slot (ERROR result)
        task_outcome: TaskOutcome | None = None
        if terminal_idx is not None and executed[terminal_idx]:
            res = results[terminal_idx]
            run.pending_image = res.image
            if isinstance(res.outcome, TaskSubmitted):
                task_outcome = self._outcome(run, "submitted", result=res.outcome.result)
            elif isinstance(res.outcome, TaskGaveUp):
                detail = res.outcome.reason
                if res.outcome.what_was_tried:
                    detail += f" (tried: {res.outcome.what_was_tried})"
                task_outcome = self._outcome(
                    run, "failed", reason="fail_task", detail=detail[:1000]
                )

        # 6. agent.step
        records = [
            _action_record(c, r, lat) for c, r, lat in zip(calls, results, latencies, strict=True)
        ]
        action = records[terminal_idx] if terminal_idx is not None else None
        coordination = [r for i, r in enumerate(records) if i != terminal_idx]
        label = describe_call(calls[terminal_idx])[:200] if terminal_idx is not None else ""
        await self._emit(
            AgentStep(
                agent_id=self.agent.id,
                task_id=task.id,
                task_key=task.key,
                step=step,
                coordination_calls=coordination,
                action=action,
                reasoning_excerpt=(turn.text or "")[:400],
                frame=self._frame_model(frame, global_step, task, obs, label),
                usage=step_usage,
                model_latency_ms=turn.latency_ms,
            )
        )

        # 7. stuck detection
        progress = any(
            ok and c.name in PROGRESS_TOOLS and not r.is_error
            for c, r, ok in zip(calls, results, executed, strict=True)
        ) or any(c.name == "submit_result" and ok for c, ok in zip(calls, executed, strict=True))
        if calls:
            done_terminal = (
                calls[terminal_idx] if terminal_idx is not None and executed[terminal_idx] else None
            )
            stuck_signal = run.detector.observe(
                StepSignals(
                    step=step,
                    phash=obs.phash,
                    action=done_terminal.name if done_terminal else None,
                    action_args=done_terminal.arguments if done_terminal else {},
                    call_errors=tuple(r.is_error for r in results),
                    progress=progress,
                    frame_id=frame.id if frame else None,
                )
            )
        if stuck_signal is not None and task_outcome is None:
            await self._report_stuck(task, stuck_signal)
        elif run.detector.episode is None and self.state.status == "stuck":
            await self.set_status("working", reason="progress")

        # 8. history
        texts = [r.text for r in results]
        run.builder.add_step(
            StepRecord(
                step=step,
                header=header,
                image=image,
                assistant_text=turn.text,
                calls=list(calls),
                results=texts,
                line=step_line(step, calls, texts, note="no tool call"),
            )
        )
        run.last_phash = obs.phash
        self.state.digest_cursor = max(self.state.digest_cursor, digest.cursor)
        if task_outcome is None:
            await run.builder.maybe_summarize(step)
        return task_outcome

    def _order_problem(self, calls: Sequence[ToolCall]) -> str | None:
        """Enforce: up to N coordination calls, then exactly one desktop/control call
        as the last call. Unknown tools are left to the registry (ERROR result)."""
        kinds = [self.registry.kind_of(c.name) for c in calls]
        terminals = [i for i, k in enumerate(kinds) if k in ("desktop", "control")]
        n_coord = sum(1 for k in kinds if k == "coordination")
        rule = (
            f"send at most {self.config.max_coordination_calls} coordination calls followed by "
            "exactly one desktop or control call as the last call"
        )
        if len(terminals) > 1:
            return f"{len(terminals)} desktop/control calls in one step; {rule}"
        if terminals and terminals[0] != len(calls) - 1:
            return f"{calls[terminals[0]].name} must be the last tool call; {rule}"
        if n_coord > self.config.max_coordination_calls:
            return f"{n_coord} coordination calls in one step; {rule}"
        return None

    async def _ask_model(
        self, messages: list[Message], specs: list[ToolSpec]
    ) -> _ModelTurn | _Stopped:
        cfg = self.config
        names = [s.name for s in specs]
        usage = ModelUsage()
        latency = 0
        text: str | None = None
        convo = list(messages)
        for attempt in range(cfg.max_no_tool_retries + 1):
            req = ModelRequest(
                messages=convo,
                tools=specs,
                tool_choice="required",
                max_output_tokens=cfg.max_output_tokens,
                temperature=cfg.temperature,
                timeout_s=cfg.model_timeout_s,
            )
            t0 = time.perf_counter()
            resp = await self.wait_or_stop(self._model.complete(req))
            if resp is None:
                return _STOPPED
            latency += resp.latency_ms or int((time.perf_counter() - t0) * 1000)
            usage = ModelUsage(
                input_tokens=usage.input_tokens + resp.usage.input_tokens,
                output_tokens=usage.output_tokens + resp.usage.output_tokens,
                images=usage.images + resp.usage.images,
                cost_usd=usage.cost_usd + resp.usage.cost_usd,
            )
            text = resp.text
            calls, used_fallback = normalize_tool_calls(resp, names, self._fallback)
            if used_fallback:
                self._log.info("tool_calls_from_text", count=len(calls))
            if calls:
                return _ModelTurn(calls=calls, text=text, usage=usage, latency_ms=latency)
            self._log.info("no_tool_call", attempt=attempt + 1)
            convo = [
                *convo,
                Message.text("assistant", text or "(no tool call)"),
                Message.text("user", NO_TOOL_CORRECTION),
            ]
        return _ModelTurn(calls=[], text=text, usage=usage, latency_ms=latency)

    # -- helpers --------------------------------------------------------------------

    async def _emit(self, payload: EventPayload) -> Event:
        task_id = self._run.task.id if self._run is not None else None
        draft = EventDraft(payload=payload, actor=self._actor, correlation_id=task_id)
        return await self._events.record(self.session.id, draft)

    async def _report_stuck(self, task: Task, signal: StuckSignal) -> None:
        self._log.warning("agent_stuck", reason=signal.reason, detail=signal.detail)
        await self._emit(
            AgentStuckDetected(
                agent_id=self.agent.id,
                task_key=task.key,
                reason=signal.reason,
                detail=signal.detail,
                frame_ids=signal.frame_ids[-3:],
            )
        )
        await self.set_status("stuck", reason=signal.reason)

    async def _save_frame(self, png: bytes, global_step: int) -> _SavedFrame | None:
        cfg = self.config
        try:
            small = await asyncio.to_thread(_encode_jpeg, png, cfg.frame_width, cfg.frame_quality)
            small_key = frame_key(self.session.id, self.agent.id, global_step)
            await self._frames.put(small, content_type="image/jpeg", key=small_key)
            full_key = None
            if cfg.keep_full_frames:
                full = await asyncio.to_thread(_encode_jpeg, png, None, 85)
                full_key = frame_key(self.session.id, self.agent.id, global_step, full=True)
                await self._frames.put(full, content_type="image/jpeg", key=full_key)
        except Exception:
            self._log.warning("frame_save_failed", step=global_step, exc_info=True)
            return None
        saved = _SavedFrame(id=new_id(), small_key=small_key, full_key=full_key, small_jpeg=small)
        if cfg.publish_thumbnails:
            try:
                await asyncio.wait_for(
                    self._bus.publish_ephemeral(
                        Thumbnail(
                            session_id=self.session.id,
                            agent_id=self.agent.id,
                            step=global_step,
                            jpeg_b64=base64.b64encode(small).decode("ascii"),
                            ts=utcnow(),
                        )
                    ),
                    timeout=cfg.heartbeat_publish_timeout_s,
                )
            except Exception:
                self._log.warning("thumbnail_publish_failed", step=global_step)
        return saved

    def _frame_model(
        self, frame: _SavedFrame | None, global_step: int, task: Task, obs: Observation, label: str
    ) -> Frame | None:
        if frame is None:
            return None
        return Frame(
            id=frame.id,
            session_id=self.session.id,
            agent_id=self.agent.id,
            step=global_step,
            task_key=task.key,
            ts=utcnow(),
            storage_key_small=frame.small_key,
            storage_key_full=frame.full_key,
            action_label=label,
            phash=obs.phash,
        )

    async def _build_digest(self, task: Task) -> Digest:
        since = self.state.digest_cursor
        try:
            return await self._digest.build(
                self.agent, task, since=since, budget_tokens=self.config.digest_budget_tokens
            )
        except Exception:
            self._log.warning("digest_failed", exc_info=True)
            return Digest("", since)

    async def _refresh_roster(self, run: _TaskRun) -> None:
        if self._roster is None:
            return
        try:
            run.builder.set_roster(await self._roster.roster())
        except Exception:
            self._log.warning("roster_failed", exc_info=True)

    async def _heartbeat(self, task: Task) -> None:
        """Bus-only heartbeat + lease renewal every ``heartbeat_interval_s``.

        Both waits are bounded (``heartbeat_publish_timeout_s``,
        ``lease_renew_timeout_s``) so a hung bus cannot delay renewal. Renewal fails
        closed: ``False``, ``LeaseLostError``, a timeout or any other exception
        counts as lease loss, unless ``lease_ttl_s`` is set and the lease confirmed
        by the last successful renewal will still be valid at the next attempt.
        """
        cfg = self.config
        confirmed_until: float | None = None
        while True:
            run = self._run
            try:
                await asyncio.wait_for(
                    self._bus.publish_ephemeral(
                        Heartbeat(
                            session_id=self.session.id,
                            agent_id=self.agent.id,
                            task_id=task.id,
                            step=run.step if run else 0,
                            ts=utcnow(),
                        )
                    ),
                    timeout=cfg.heartbeat_publish_timeout_s,
                )
            except Exception:
                self._log.warning("heartbeat_publish_failed")
            if self._renew_lease is not None:
                attempt_started = self._clock()
                try:
                    ok = await asyncio.wait_for(
                        self._renew_lease(), timeout=cfg.lease_renew_timeout_s
                    )
                except LeaseLostError:
                    ok = False
                except Exception as exc:
                    # Fail closed: keep going only while the last confirmed renewal
                    # provably covers the next attempt as well.
                    next_attempt_done = (
                        self._clock() + cfg.heartbeat_interval_s + cfg.lease_renew_timeout_s
                    )
                    ok = confirmed_until is not None and next_attempt_done < confirmed_until
                    self._log.warning(
                        "lease_renew_failed", error=type(exc).__name__, still_valid=ok
                    )
                else:
                    if ok and cfg.lease_ttl_s is not None:
                        confirmed_until = attempt_started + cfg.lease_ttl_s
                if not ok:
                    self._log.error("lease_lost")
                    self._lease_lost.set()
                    return
            await asyncio.sleep(cfg.heartbeat_interval_s)
