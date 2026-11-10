"""Human in the loop (SPEC §10.7, §15.2; T18): approvals, steering, takeover and the
kill switch.

``HitlApprovalGate`` implements ``runtime.agent.loop.ApprovalGate``:

1. ``request`` stores a pending ``Approval`` (``expires_at`` = now + 15 min) and emits
   ``approval.requested`` with the agent's current frame, in one transaction
   (``read_model_update``), then waits for the decision or the expiry and returns
   the final approval. The ``request_approval`` tool turns it into the tool result
   and the next step's note ("Approved: ...", "Denied: ... Do this instead: ...",
   "... expired; continue without doing it.").
2. ``decide`` (the user's ``command.decide_approval``) and ``expire`` move a pending
   approval to its final status and emit ``approval.decided``. Decisions are
   serialized in-process, and the store re-checks ``pending`` inside the event's
   transaction (row lock in Postgres), so two racing decisions never both win:
   the loser gets ``ConflictError`` and no event is written.
3. A cancelled wait (the user stopped the agent) expires the approval in the
   background, so no stale card stays open. After a runtime restart nobody waits
   for older approvals any more: ``expire_pending(session_id)`` closes them.
4. ``flag`` lets the supervisor ask the user to approve an action an agent is about
   to take. The agent gets a direct note to wait; the decision (or the expiry)
   reaches it as a direct note from the user. The agent is not paused.

``HitlController`` executes the user's commands for one session:

* steering (``command.steer``): a board note from the user to one agent or to all,
  shown first in the digest (``post_user_steering``), plus ``user.steer``; when
  addressed to all, also ``supervisor.triggered(user_message)``;
* takeover/release: the agent's ``ControlState`` goes to ``takeover`` (the loop
  stops stepping, status ``paused_by_user``) and back to ``run`` (the loop adds the
  "re-observe" note), with ``user.takeover_started``/``user.takeover_ended``;
* kill switch (``command.stop``): every bound control goes to ``stop`` at once (the
  loops interrupt model calls and waits), pending approvals expire, then
  ``Scheduler.stop("user")`` cancels the tasks, emits ``session.stopped`` and kills
  the sandboxes (the pool cancels runs that ignore the stop after 4 s), and the
  replay index is flushed. The target is 5 s.

The runtime wiring (T13) binds each agent's control (``RuntimeDeps.control``) with
``bind_control`` inside the ``RuntimeFactory`` and routes commands to ``handle``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Collection, Coroutine
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, Protocol

import structlog

from agentplatform.contracts.artifact import Approval
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.commands import (
    Command,
    DecideApprovalCommand,
    ReleaseAgentCommand,
    SteerCommand,
    StopSessionCommand,
    TakeoverAgentCommand,
)
from agentplatform.contracts.common import Actor, ApprovalStatus, RiskLevel, new_id, utcnow
from agentplatform.contracts.events import (
    ApprovalDecided,
    ApprovalRequested,
    Event,
    EventDraft,
    EventPayload,
    SupervisorTriggered,
    UserSteer,
    UserTakeoverEnded,
    UserTakeoverStarted,
)
from agentplatform.contracts.session import Session
from agentplatform.db.event_store import ReadModelUpdate
from agentplatform.db.repositories import ApprovalRepository
from agentplatform.db.session import SessionMaker, as_session, transaction
from agentplatform.errors import (
    ConflictError,
    InvalidRequestError,
    NotFoundError,
    NotSupportedError,
)
from agentplatform.runtime.agent.loop import ControlMode, ControlState
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.runtime.coordination.posting import message_agent, post_user_steering

__all__ = [
    "APPROVAL_TTL_S",
    "STOP_DEADLINE_S",
    "ApprovalEventSink",
    "ApprovalStore",
    "HitlApprovalGate",
    "HitlController",
    "InMemoryApprovalStore",
    "PostgresApprovalStore",
    "ReplayFlusher",
    "SessionStopper",
    "SettableControl",
    "StopReport",
    "decision_text",
]

log = structlog.get_logger(__name__)

APPROVAL_TTL_S = 15 * 60.0
STOP_DEADLINE_S = 5.0
_DETAIL_CHARS = 300


# -- interfaces ---------------------------------------------------------------------------


class ApprovalEventSink(Protocol):
    """``EventRecorder``: appends an event (with an atomic read-model update) and
    publishes it."""

    async def record(
        self,
        session_id: str,
        draft: EventDraft,
        read_model_update: ReadModelUpdate | None = None,
    ) -> Event: ...


class ApprovalStore(Protocol):
    """Approval rows (SPEC §17 ``approvals``). Writes happen inside the event's
    transaction through the returned ``ReadModelUpdate``s."""

    def insert_update(self, approval: Approval) -> ReadModelUpdate:
        """Insert a new pending approval (``ConflictError`` if the id exists)."""
        ...

    def decide_update(self, decided: Approval) -> ReadModelUpdate:
        """Move the approval to ``decided.status``; ``ConflictError`` if it is no
        longer pending (then the event is not written)."""
        ...

    async def get(self, approval_id: str) -> Approval:
        """``NotFoundError`` if missing."""
        ...

    async def pending(self, session_id: str) -> list[Approval]: ...


class SettableControl(ControlState, Protocol):
    """A ``ControlState`` the user can change (``LocalControlState``)."""

    def set(self, mode: ControlMode) -> None: ...


class SessionStopper(Protocol):
    """``Scheduler.stop``: stop runs, cancel tasks, ``session.stopped``, kill sandboxes."""

    async def stop(
        self, reason: Literal["user", "budget", "idle", "error"] = "user", detail: str | None = None
    ) -> None: ...


class ReplayFlusher(Protocol):
    """Persists whatever the replay needs that is not yet durable (frame index)."""

    async def flush(self, session_id: str) -> None: ...


# -- stores -------------------------------------------------------------------------------


class InMemoryApprovalStore:
    """``ApprovalStore`` for tests and single-process runs without Postgres."""

    def __init__(self) -> None:
        self.approvals: dict[str, Approval] = {}

    def insert_update(self, approval: Approval) -> ReadModelUpdate:
        async def apply(event: Event, tx: object) -> None:
            if approval.id in self.approvals:
                raise ConflictError(f"approval {approval.id} already exists")
            self.approvals[approval.id] = approval

        return apply

    def decide_update(self, decided: Approval) -> ReadModelUpdate:
        async def apply(event: Event, tx: object) -> None:
            current = self.approvals.get(decided.id)
            if current is None:
                raise NotFoundError(f"approval {decided.id} does not exist")
            if current.status != "pending":
                raise ConflictError(f"approval {decided.id} is already {current.status}")
            self.approvals[decided.id] = decided

        return apply

    async def get(self, approval_id: str) -> Approval:
        try:
            return self.approvals[approval_id]
        except KeyError:
            raise NotFoundError(f"approval {approval_id} does not exist") from None

    async def pending(self, session_id: str) -> list[Approval]:
        return sorted(
            (
                a
                for a in self.approvals.values()
                if a.session_id == session_id and a.status == "pending"
            ),
            key=lambda a: (a.created_at, a.id),
        )


class PostgresApprovalStore:
    """``ApprovalStore`` over ``ApprovalRepository`` (needs ``PostgresEventStore``)."""

    def __init__(self, sessionmaker: SessionMaker) -> None:
        self._sm = sessionmaker

    def insert_update(self, approval: Approval) -> ReadModelUpdate:
        async def apply(event: Event, tx: object) -> None:
            await ApprovalRepository(as_session(tx)).insert(approval)

        return apply

    def decide_update(self, decided: Approval) -> ReadModelUpdate:
        assert decided.decided_at is not None

        async def apply(event: Event, tx: object) -> None:
            assert decided.decided_at is not None
            await ApprovalRepository(as_session(tx)).decide(
                decided.id,
                decided.status,
                decided_by=decided.decided_by,
                decided_at=decided.decided_at,
                note=decided.note,
            )

        return apply

    async def get(self, approval_id: str) -> Approval:
        async with transaction(self._sm) as tx:
            return await ApprovalRepository(tx).get(approval_id)

    async def pending(self, session_id: str) -> list[Approval]:
        async with transaction(self._sm) as tx:
            return await ApprovalRepository(tx).list(session_id, statuses=["pending"])


# -- approval gate ------------------------------------------------------------------------


def decision_text(approval: Approval) -> str:
    """What the agent is told about a final approval (SPEC §15.2)."""
    summary = approval.action_summary
    note = (approval.note or "").strip()
    if approval.status == "approved":
        return f"Approved: {summary}" + (f". Note: {note}" if note else "")
    if approval.status == "denied":
        return f"Denied: {summary}. Do this instead: {note or 'choose another way'}"
    return f"Approval for {summary!r} expired; continue without doing it."


@dataclass(slots=True)
class _Flag:
    agent_id: str
    timer: asyncio.Task[None] | None = None


class HitlApprovalGate:
    """``ApprovalGate`` (see the module docstring). One per runtime process; it may
    serve several sessions. ``agent_name`` maps agent ids to display names for the
    event actor."""

    def __init__(
        self,
        *,
        events: ApprovalEventSink,
        store: ApprovalStore,
        board: BoardService | None = None,
        ttl_s: float = APPROVAL_TTL_S,
        agent_name: Callable[[str], str] | None = None,
        now: Callable[[], datetime] = utcnow,
    ) -> None:
        self._events = events
        self._store = store
        self._board = board
        self._ttl_s = ttl_s
        self._agent_name = agent_name or (lambda agent_id: agent_id)
        self._now = now
        self._lock = asyncio.Lock()
        self._waiters: dict[str, asyncio.Future[Approval]] = {}
        self._flags: dict[str, _Flag] = {}
        self._background: set[asyncio.Task[Any]] = set()

    @property
    def ttl_s(self) -> float:
        return self._ttl_s

    # -- ApprovalGate -----------------------------------------------------------------

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
        approval = self._new(session_id, agent_id, task_key, action_summary, risk, frame_id)
        fut: asyncio.Future[Approval] = asyncio.get_running_loop().create_future()
        self._waiters[approval.id] = fut
        try:
            await self._record_requested(
                approval, Actor.agent(agent_id, self._agent_name(agent_id))
            )
            try:
                async with asyncio.timeout(self._ttl_s):
                    return await asyncio.shield(fut)
            except TimeoutError:
                return await self.expire(approval.id, note="no decision in time")
        except asyncio.CancelledError:
            if not fut.done():
                self._spawn(self._expire_quietly(approval.id, "the agent stopped waiting"))
            raise
        finally:
            self._waiters.pop(approval.id, None)

    # -- decisions --------------------------------------------------------------------

    async def decide(
        self,
        approval_id: str,
        decision: Literal["approve", "deny"],
        *,
        by: Actor,
        note: str | None = None,
        session_id: str | None = None,
    ) -> Approval:
        """Apply the user's decision. ``NotFoundError`` for unknown approvals (or of
        another session), ``ConflictError`` if already decided or expired."""
        current = await self._store.get(approval_id)
        if session_id is not None and current.session_id != session_id:
            raise NotFoundError(f"approval {approval_id} does not exist")
        if (
            current.status == "pending"
            and current.expires_at is not None
            and current.expires_at <= self._now()
        ):
            await self.expire(approval_id, note="no decision in time")
            raise ConflictError(f"approval {approval_id} is already expired")
        status: ApprovalStatus = "approved" if decision == "approve" else "denied"
        return await self._finish(approval_id, status, by, note)

    async def expire(self, approval_id: str, note: str | None = None) -> Approval:
        """Expire a pending approval; returns the final approval (whoever won)."""
        try:
            return await self._finish(approval_id, "expired", Actor.system(), note)
        except ConflictError:
            return await self._store.get(approval_id)

    async def expire_pending(
        self, session_id: str, note: str = "session stopped"
    ) -> list[Approval]:
        """Expire every pending approval of a session (stop, runtime restart)."""
        return [await self.expire(a.id, note=note) for a in await self._store.pending(session_id)]

    # -- supervisor ---------------------------------------------------------------------

    async def flag(
        self,
        *,
        session_id: str,
        agent_id: str,
        action_summary: str,
        risk: RiskLevel,
        task_key: str | None = None,
        frame_id: str | None = None,
    ) -> Approval:
        """The supervisor flags an action of ``agent_id`` for the user's approval.
        Returns the pending approval; the decision is delivered as a note."""
        board = self._board
        if board is None:
            raise NotSupportedError("flagging an action needs the board")
        approval = self._new(session_id, agent_id, task_key, action_summary, risk, frame_id)
        await self._record_requested(approval, Actor.supervisor())
        flag = _Flag(agent_id)
        self._flags[approval.id] = flag
        flag.timer = self._spawn(self._expire_later(approval.id))
        try:
            await message_agent(
                board,
                session_id,
                agent_id,
                f"Wait for the user's approval before you do this: {action_summary}. "
                "Do not do it until you are told it was approved.",
            )
        except Exception as exc:
            log.warning(
                "approval_flag_note_failed",
                session_id=session_id,
                agent_id=agent_id,
                error=type(exc).__name__,
            )
        return approval

    async def aclose(self) -> None:
        """Cancel background work (flag timers, cancelled-wait expiries)."""
        tasks = list(self._background)
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # -- internals ----------------------------------------------------------------------

    def _new(
        self,
        session_id: str,
        agent_id: str,
        task_key: str | None,
        action_summary: str,
        risk: RiskLevel,
        frame_id: str | None,
    ) -> Approval:
        now = self._now()
        return Approval(
            id=new_id(),
            session_id=session_id,
            agent_id=agent_id,
            task_key=task_key,
            action_summary=action_summary,
            risk=risk,
            frame_id=frame_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl_s),
        )

    async def _record_requested(self, approval: Approval, actor: Actor) -> None:
        await self._events.record(
            approval.session_id,
            EventDraft(
                payload=ApprovalRequested(approval=approval),
                actor=actor,
                correlation_id=approval.id,
            ),
            read_model_update=self._store.insert_update(approval),
        )
        log.info(
            "approval_requested",
            session_id=approval.session_id,
            agent_id=approval.agent_id,
            task_key=approval.task_key,
            approval_id=approval.id,
            risk=approval.risk,
        )

    async def _finish(
        self, approval_id: str, status: ApprovalStatus, by: Actor, note: str | None
    ) -> Approval:
        async with self._lock:
            current = await self._store.get(approval_id)
            if current.status != "pending":
                raise ConflictError(f"approval {approval_id} is already {current.status}")
            decided = Approval.model_validate(
                {
                    **current.model_dump(),
                    "status": status,
                    "decided_by": by,
                    "decided_at": self._now(),
                    "note": note,
                }
            )
            await self._events.record(
                decided.session_id,
                EventDraft(
                    payload=ApprovalDecided(approval=decided),
                    actor=by,
                    correlation_id=decided.id,
                ),
                read_model_update=self._store.decide_update(decided),
            )
        log.info(
            "approval_decided",
            session_id=decided.session_id,
            agent_id=decided.agent_id,
            approval_id=decided.id,
            status=status,
        )
        fut = self._waiters.get(approval_id)
        if fut is not None and not fut.done():
            fut.set_result(decided)
        await self._deliver_flag(decided)
        return decided

    async def _deliver_flag(self, decided: Approval) -> None:
        flag = self._flags.pop(decided.id, None)
        if flag is None:
            return
        if flag.timer is not None and flag.timer is not asyncio.current_task():
            flag.timer.cancel()
        if self._board is None:
            return
        by = decided.decided_by
        author = by if by is not None and by.type == "user" else Actor.supervisor()
        try:
            await message_agent(
                self._board,
                decided.session_id,
                flag.agent_id,
                decision_text(decided),
                author=author,
            )
        except Exception as exc:
            log.warning(
                "approval_decision_note_failed",
                session_id=decided.session_id,
                agent_id=flag.agent_id,
                error=type(exc).__name__,
            )

    async def _expire_later(self, approval_id: str) -> None:
        await asyncio.sleep(self._ttl_s)
        await self._expire_quietly(approval_id, "no decision in time")

    async def _expire_quietly(self, approval_id: str, note: str) -> None:
        try:
            await self.expire(approval_id, note=note)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("approval_expiry_failed", approval_id=approval_id, error=type(exc).__name__)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        return task


# -- controller ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class StopReport:
    elapsed_s: float
    within_deadline: bool
    approvals_expired: int


class HitlController:
    """Carries out the user's steering, takeover, approval and stop commands for one
    session (see the module docstring)."""

    def __init__(
        self,
        *,
        session: Session,
        events: ApprovalEventSink,
        gate: HitlApprovalGate,
        scheduler: SessionStopper,
        board: BoardService | None = None,
        replay: ReplayFlusher | None = None,
        agent_ids: Callable[[], Collection[str]] | None = None,
        stop_deadline_s: float = STOP_DEADLINE_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self._events = events
        self._gate = gate
        self._scheduler = scheduler
        self._board = board
        self._replay = replay
        self._agent_ids = agent_ids
        self._deadline_s = stop_deadline_s
        self._monotonic = monotonic
        self._controls: dict[str, SettableControl] = {}
        self._lock = asyncio.Lock()
        self._stop: asyncio.Task[StopReport] | None = None
        self._log = log.bind(session_id=session.id)

    @property
    def session_id(self) -> str:
        return self.session.id

    # -- wiring -----------------------------------------------------------------------

    def bind_control(self, agent_id: str, control: SettableControl) -> None:
        """Register an agent's control state (call from the ``RuntimeFactory`` with
        ``RuntimeDeps.control``). A stopped session stops new controls at once."""
        self._controls[agent_id] = control
        if self._stop is not None:
            control.set("stop")

    def known_agents(self) -> Collection[str]:
        return self._agent_ids() if self._agent_ids is not None else self._controls.keys()

    # -- commands ---------------------------------------------------------------------

    async def handle(self, command: Command) -> bool:
        """Execute a T18 command. ``False`` for commands this controller does not own."""
        if command.session_id != self.session_id:
            raise InvalidRequestError("command for another session")
        by = command.issued_by
        if isinstance(command, SteerCommand):
            await self.steer(command.agent_id, command.text, by=by)
        elif isinstance(command, TakeoverAgentCommand):
            await self.takeover(command.agent_id, by=by)
        elif isinstance(command, ReleaseAgentCommand):
            await self.release(command.agent_id, by=by)
        elif isinstance(command, DecideApprovalCommand):
            await self._gate.decide(
                command.approval_id,
                command.decision,
                by=by,
                note=command.note,
                session_id=self.session_id,
            )
        elif isinstance(command, StopSessionCommand):
            await self.stop(by=by)
        else:
            return False
        return True

    async def steer(self, agent_id: str | None, text: str, *, by: Actor) -> BoardEntry:
        """A user steering note to ``agent_id`` (or all agents and the supervisor)."""
        if self.session.mode == "parallel_isolated":
            raise NotSupportedError(
                "steering is not available in parallel_isolated mode (agents get no digest)"
            )
        if self._board is None:
            raise NotSupportedError("steering needs the board")
        async with self._lock:
            self._ensure_running()
            if agent_id is not None and agent_id not in self.known_agents():
                raise NotFoundError(f"agent {agent_id} does not exist")
            user = by if by.type == "user" else Actor.user()
            entry = await post_user_steering(
                self._board,
                self.session_id,
                text,
                to=None if agent_id is None else [agent_id],
                user_id=user.id,
                user_name=user.name,
            )
            await self._emit(UserSteer(agent_id=agent_id, text=text, entry_id=entry.id), by)
            if agent_id is None:
                await self._emit(
                    SupervisorTriggered(
                        reason="user_message",
                        detail=" ".join(text.split())[:_DETAIL_CHARS],
                        entry_ids=[entry.id],
                    ),
                    Actor.system(),
                )
        self._log.info("user_steer", agent_id=agent_id, entry_id=entry.id)
        return entry

    async def takeover(self, agent_id: str, *, by: Actor) -> None:
        """Pause the agent's loop for the user (``paused_by_user`` at its checkpoint)."""
        async with self._lock:
            self._ensure_running()
            control = self._control(agent_id)
            if control.mode == "takeover":
                raise ConflictError(f"agent {agent_id} is already taken over")
            if control.mode == "stop":
                raise ConflictError(f"agent {agent_id} is stopped")
            control.set("takeover")  # first: no further action reaches the screen
            await self._emit(UserTakeoverStarted(agent_id=agent_id), by)
        self._log.info("user_takeover_started", agent_id=agent_id)

    async def release(self, agent_id: str, *, by: Actor) -> None:
        """Give the desktop back; the next step carries the re-observe note."""
        async with self._lock:
            self._ensure_running()
            control = self._control(agent_id)
            if control.mode != "takeover":
                raise ConflictError(f"agent {agent_id} is not taken over")
            control.set("run")
            await self._emit(UserTakeoverEnded(agent_id=agent_id), by)
        self._log.info("user_takeover_ended", agent_id=agent_id)

    async def stop(self, *, by: Actor | None = None) -> StopReport:
        """Kill switch. Idempotent: a second call waits for the first."""
        if self._stop is None:
            self._stop = asyncio.create_task(self._do_stop(by), name=f"kill-{self.session_id}")
        return await asyncio.shield(self._stop)

    # -- internals ----------------------------------------------------------------------

    async def _do_stop(self, by: Actor | None) -> StopReport:
        started = self._monotonic()
        for control in list(self._controls.values()):
            control.set("stop")  # loops interrupt model calls and waits right away
        self._log.info("kill_switch", by=None if by is None else by.type)
        expired = await self._gate.expire_pending(self.session_id, note="session stopped")
        await self._scheduler.stop("user", "stopped by the user")
        if self._replay is not None:
            try:
                await self._replay.flush(self.session_id)
            except Exception as exc:
                self._log.warning("replay_flush_failed", error=type(exc).__name__)
        elapsed = self._monotonic() - started
        within = elapsed <= self._deadline_s
        if not within:
            self._log.warning("kill_switch_slow", elapsed_s=round(elapsed, 2))
        return StopReport(elapsed_s=elapsed, within_deadline=within, approvals_expired=len(expired))

    def _ensure_running(self) -> None:
        if self._stop is not None:
            raise ConflictError("the session is stopping")

    def _control(self, agent_id: str) -> SettableControl:
        control = self._controls.get(agent_id)
        if control is None:
            raise NotFoundError(f"agent {agent_id} does not exist or has no desktop yet")
        return control

    async def _emit(self, payload: EventPayload, actor: Actor) -> None:
        await self._events.record(self.session_id, EventDraft(payload=payload, actor=actor))
