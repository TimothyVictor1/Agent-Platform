"""Executes API commands for one session (T13; SPEC §16, ``contracts/commands.py``).

``CommandRouter.handle`` covers every command type:

* ``plan_session``: ``session.planning_started``, then planner ``propose`` →
  ``session.plan_proposed`` or ``session.planning_failed``;
* ``refine_plan``: planner ``refine`` → ``plan_proposed`` (with diff) or
  ``planning_failed``;
* ``update_plan``: re-validated → ``session.plan_updated`` (``source="user"``);
* ``approve_plan``: → ``session.approved`` (the scheduler creates the tasks);
* ``followup``: planner ``extend`` → ``plan_proposed``, plus ``session.approved``
  when ``auto_approve_followups`` is set;
* ``pause`` / ``resume``: ``session.paused`` / ``session.resumed`` and the session
  hold on every agent (``AgentControls``);
* ``steer``, ``takeover``, ``release``, ``decide_approval``, ``stop``:
  ``HitlController.handle`` (stop → ``Scheduler.stop``);
* ``post_board``: ``BoardService.post`` as the user.

Every event a command causes carries ``causation_id = command.id`` (the
``command_id`` the API returned), including ``plan_proposed`` and
``planning_failed`` (SPEC §7.1).

Commands are delivered at least once. ``handle`` returns normally once the command
is done or rejected (rejections are logged: the API already answered 202); it
raises only for infrastructure failures, so the caller does not ack and the command
is redelivered. Before running, a command is skipped when the ``CommandLog`` knows
it or when the log already has an event caused by it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import structlog

from agentplatform.contracts.commands import (
    ApprovePlanCommand,
    Command,
    DecideApprovalCommand,
    FollowupCommand,
    PauseSessionCommand,
    PlanSessionCommand,
    PostBoardCommand,
    RefinePlanCommand,
    ReleaseAgentCommand,
    ResumeSessionCommand,
    SteerCommand,
    StopSessionCommand,
    TakeoverAgentCommand,
    UpdatePlanCommand,
)
from agentplatform.contracts.common import Actor, SessionStatus, TaskStatus
from agentplatform.contracts.events import (
    Event,
    EventDraft,
    EventPayload,
    SessionApproved,
    SessionCompleted,
    SessionCreated,
    SessionFailed,
    SessionPaused,
    SessionPlanningFailed,
    SessionPlanningStarted,
    SessionPlanProposed,
    SessionPlanUpdated,
    SessionResumed,
    SessionStopped,
    TaskAccepted,
    TaskCancelled,
    TaskCreated,
    TaskFailed,
    TaskUpdated,
)
from agentplatform.contracts.plan import PlanProposal, validate_plan
from agentplatform.db.event_store import EventStore
from agentplatform.errors import AppError, ConflictError, ModelError, PlannerError
from agentplatform.runtime.container import SessionRuntime
from agentplatform.runtime.readmodels import PLANNED_EVENT_TYPES

__all__ = ["CommandRouter", "PlanLog", "read_plan_log"]

log = structlog.get_logger(__name__)

EDITABLE: frozenset[SessionStatus] = frozenset({"planning", "awaiting_approval"})
FOLLOWUP_OK: frozenset[SessionStatus] = frozenset({"running", "paused", "completed"})
HITL_COMMANDS = (
    SteerCommand,
    TakeoverAgentCommand,
    ReleaseAgentCommand,
    DecideApprovalCommand,
    StopSessionCommand,
)


@dataclass(slots=True)
class PlanLog:
    """What the commands need from the session's log."""

    status: SessionStatus = "planning"
    latest_version: int = 0
    latest: PlanProposal | None = None
    approved_version: int = 0
    started: bool = False
    ended: bool = False
    planned: bool = False
    causations: set[str] = field(default_factory=set)
    task_status: dict[str, TaskStatus] = field(default_factory=dict)

    def apply(self, event: Event) -> None:
        if event.causation_id:
            self.causations.add(event.causation_id)
        if event.type in PLANNED_EVENT_TYPES:
            self.planned = True
        p = event.payload
        if isinstance(p, SessionCreated):
            self.status = p.session.status
            self.started = p.session.started_at is not None
            self.ended = p.session.ended_at is not None
        elif isinstance(p, SessionPlanProposed | SessionPlanUpdated):
            if p.plan_version >= self.latest_version:
                self.latest_version, self.latest = p.plan_version, p.proposal
            user_plan = isinstance(p, SessionPlanProposed) or p.source == "user"
            if user_plan and self.status not in ("failed", "stopped"):
                self.status = "awaiting_approval"
        elif isinstance(p, SessionApproved):
            self.approved_version = max(self.approved_version, p.plan_version)
            self.started, self.ended = True, False
            self.status = "running"
        elif isinstance(p, SessionPaused | SessionResumed):
            if self.status != "awaiting_approval":
                self.status = "paused" if isinstance(p, SessionPaused) else "running"
        elif isinstance(p, SessionCompleted):
            self.status = "completed"
            self.ended = True
        elif isinstance(p, SessionFailed):
            self.status = "failed"
            self.ended = True
        elif isinstance(p, SessionStopped):
            self.status = "stopped"
            self.ended = True
        elif isinstance(p, TaskCreated | TaskUpdated):
            self.task_status[p.task.key] = p.task.status
        elif isinstance(p, TaskAccepted):
            self.task_status[p.task_key] = "done"
        elif isinstance(p, TaskCancelled):
            self.task_status[p.task_key] = "cancelled"
        elif isinstance(p, TaskFailed) and not p.will_retry:
            self.task_status[p.task_key] = "failed"


async def read_plan_log(store: EventStore, session_id: str) -> PlanLog:
    plan_log = PlanLog()
    after = 0
    while True:
        batch = await store.read(session_id, after, 5000)
        if not batch:
            return plan_log
        for event in batch:
            plan_log.apply(event)
        after = batch[-1].seq


class CommandRouter:
    """Runs the commands of one session (see the module docstring). Commands of a
    session are handled one at a time by its host, in queue order."""

    def __init__(self, rt: SessionRuntime) -> None:
        self.rt = rt
        self._c = rt.container
        self._session = rt.session
        self._log = log.bind(session_id=rt.session.id)

    # -- entry points -----------------------------------------------------------------

    async def handle(self, command: Command) -> None:
        if command.session_id != self._session.id:
            self._log.warning("command_for_other_session", command_id=command.id)
            return
        clog = self._c.command_log
        if await clog.seen(command.id):
            self._log.info("command_duplicate_skipped", command_id=command.id)
            return
        log_ = self._log.bind(command_id=command.id, command_type=command.type)
        try:
            await self._dispatch(command)
        except AppError as exc:
            # rejected: the session moved on since the API checked (or a bad id)
            log_.warning("command_rejected", error=type(exc).__name__, detail=exc.detail[:300])
        await clog.mark(command.id)
        log_.info("command_handled")

    async def plan_initial(self, causation_id: str | None) -> None:
        """Plan a new session unless it already went through the planner. Used for
        ``plan_session`` and by the stuck-planning sweep (``causation_id=None``)."""
        plan_log = await read_plan_log(self._c.store, self._session.id)
        if plan_log.planned or plan_log.status != "planning":
            self._log.info("planning_skipped", reason="already planned")
            return
        await self._emit(SessionPlanningStarted(reason="initial"), causation_id)
        session = self._session
        try:
            proposal = await self._c.planner(session).propose(
                session.prompt, agent_count_hint=session.agent_count, session_id=session.id
            )
        except (PlannerError, ModelError) as exc:
            await self._planning_failed(exc, causation_id)
            return
        await self._emit(
            SessionPlanProposed(plan_version=plan_log.latest_version + 1, proposal=proposal),
            causation_id,
        )

    # -- dispatch ---------------------------------------------------------------------

    async def _dispatch(self, command: Command) -> None:
        if isinstance(command, HITL_COMMANDS):
            await self.rt.hitl.handle(command)
            return
        if isinstance(command, PostBoardCommand):
            await self._c.board.post(self._session.id, command.entry, author=command.issued_by)
            return
        plan_log = await read_plan_log(self._c.store, self._session.id)
        if command.id in plan_log.causations:
            self._log.info("command_already_applied", command_id=command.id)
            return
        if isinstance(command, PlanSessionCommand):
            await self.plan_initial(command.id)
        elif isinstance(command, RefinePlanCommand):
            await self._refine(command, plan_log)
        elif isinstance(command, UpdatePlanCommand):
            await self._update(command, plan_log)
        elif isinstance(command, ApprovePlanCommand):
            await self._approve(command, plan_log)
        elif isinstance(command, FollowupCommand):
            await self._followup(command, plan_log)
        elif isinstance(command, PauseSessionCommand):
            if not plan_log.started or plan_log.ended:
                raise ConflictError(f"cannot pause a {plan_log.status} session")
            await self._emit(SessionPaused(reason="paused by the user"), command.id, command)
            self.rt.controls.pause_session()
        elif isinstance(command, ResumeSessionCommand):
            if not plan_log.started or plan_log.ended:
                raise ConflictError(f"cannot resume a {plan_log.status} session")
            self.rt.controls.resume_session()
            await self._emit(SessionResumed(), command.id, command)

    async def _refine(self, cmd: RefinePlanCommand, plan_log: PlanLog) -> None:
        if plan_log.status not in EDITABLE or plan_log.latest is None:
            raise ConflictError(f"cannot refine the plan of a {plan_log.status} session")
        await self._emit(
            SessionPlanningStarted(reason="refine", instruction=cmd.instruction[:4000]), cmd.id
        )
        try:
            revision = await self._c.planner(self._session).refine(
                plan_log.latest,
                cmd.instruction,
                agent_count_hint=self._session.agent_count,
                session_id=self._session.id,
            )
        except (PlannerError, ModelError) as exc:
            await self._planning_failed(exc, cmd.id)
            return
        await self._emit(
            SessionPlanProposed(
                plan_version=plan_log.latest_version + 1,
                proposal=revision.proposal,
                diff=revision.diff,
            ),
            cmd.id,
        )

    async def _update(self, cmd: UpdatePlanCommand, plan_log: PlanLog) -> None:
        if plan_log.status not in EDITABLE or plan_log.latest is None:
            raise ConflictError(f"cannot edit the plan of a {plan_log.status} session")
        if cmd.base_version != plan_log.latest_version:
            raise ConflictError(
                f"the plan changed meanwhile (current version {plan_log.latest_version}, "
                f"edit based on {cmd.base_version})"
            )
        errors = validate_plan(cmd.tasks)
        if errors:
            raise ConflictError(f"invalid plan: {errors[0].message}")
        proposal = plan_log.latest.model_copy(update={"tasks": list(cmd.tasks)})
        await self._emit(
            SessionPlanUpdated(
                plan_version=plan_log.latest_version + 1,
                proposal=proposal,
                source="user",
                reason="edited by the user",
            ),
            cmd.id,
            cmd,
        )

    async def _approve(self, cmd: ApprovePlanCommand, plan_log: PlanLog) -> None:
        if plan_log.status != "awaiting_approval":
            raise ConflictError(f"cannot approve while the session is {plan_log.status}")
        if cmd.plan_version != plan_log.latest_version:
            raise ConflictError(
                f"plan version {cmd.plan_version} is not the current version "
                f"{plan_log.latest_version}"
            )
        await self._emit(SessionApproved(plan_version=cmd.plan_version), cmd.id, cmd)

    async def _followup(self, cmd: FollowupCommand, plan_log: PlanLog) -> None:
        if plan_log.status not in FOLLOWUP_OK or plan_log.latest is None:
            raise ConflictError(f"cannot add a follow-up to a {plan_log.status} session")
        await self._emit(
            SessionPlanningStarted(reason="followup", instruction=cmd.prompt[:4000]), cmd.id
        )
        try:
            revision = await self._c.planner(self._session).extend(
                plan_log.latest,
                cmd.prompt,
                statuses=plan_log.task_status,
                agent_count_hint=self._session.agent_count,
                session_id=self._session.id,
            )
        except (PlannerError, ModelError) as exc:
            await self._planning_failed(exc, cmd.id)
            return
        version = plan_log.latest_version + 1
        await self._emit(
            SessionPlanProposed(
                plan_version=version, proposal=revision.proposal, diff=revision.diff
            ),
            cmd.id,
        )
        if self._c.policies.auto_approve_followups:
            await self._emit(SessionApproved(plan_version=version), cmd.id, cmd)

    # -- helpers ----------------------------------------------------------------------

    async def _planning_failed(self, exc: AppError, causation_id: str | None) -> None:
        if isinstance(exc, PlannerError):
            reason, errors = exc.detail[:1000], list(exc.errors or [])
        else:
            reason, errors = f"the planner model failed ({type(exc).__name__})", []
        self._log.warning("planning_failed", error=type(exc).__name__)
        await self._emit(SessionPlanningFailed(reason=reason, errors=errors), causation_id)

    async def _emit(
        self, payload: EventPayload, causation_id: str | None, cmd: Command | None = None
    ) -> Event:
        actor = cmd.issued_by if cmd is not None else Actor.system()
        return await self._c.recorder.record(
            self._session.id, EventDraft(payload=payload, actor=actor, causation_id=causation_id)
        )
