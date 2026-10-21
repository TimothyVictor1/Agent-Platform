"""Commands from the API to the Runtime (SPEC §5.1 decision 2, §16).

The API never runs agents. For every user action that the Runtime must carry out it
validates the request against current state, then enqueues one ``Command`` on the
bus and answers ``202 {command_id}``. The Runtime that owns the session consumes it.

Queues (see ``bus.base``): ``command.plan_session`` for a brand-new session goes to
``NEW_SESSIONS_QUEUE`` (any runtime may claim the session); every other command goes
to ``session_command_queue(session_id)``.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.common import Actor, Contract, UtcDatetime
from agentplatform.contracts.task import TaskDraft

__all__ = [
    "COMMAND_ADAPTER",
    "ApprovePlanCommand",
    "Command",
    "CommandBase",
    "DecideApprovalCommand",
    "FollowupCommand",
    "PauseSessionCommand",
    "PlanSessionCommand",
    "PostBoardCommand",
    "RefinePlanCommand",
    "ReleaseAgentCommand",
    "ResumeSessionCommand",
    "SteerCommand",
    "StopSessionCommand",
    "TakeoverAgentCommand",
    "UpdatePlanCommand",
]


class CommandBase(Contract):
    id: str
    session_id: str
    issued_at: UtcDatetime
    issued_by: Actor


class PlanSessionCommand(CommandBase):
    """Run the planner for a newly created session."""

    type: Literal["command.plan_session"] = "command.plan_session"


class RefinePlanCommand(CommandBase):
    type: Literal["command.refine_plan"] = "command.refine_plan"
    instruction: str = Field(min_length=1, max_length=4000)


class UpdatePlanCommand(CommandBase):
    """Store a user-edited plan (already validated by the API) as a new plan version."""

    type: Literal["command.update_plan"] = "command.update_plan"
    tasks: list[TaskDraft]
    base_version: int = Field(ge=1)


class ApprovePlanCommand(CommandBase):
    type: Literal["command.approve_plan"] = "command.approve_plan"
    plan_version: int = Field(ge=1)


class FollowupCommand(CommandBase):
    type: Literal["command.followup"] = "command.followup"
    prompt: str = Field(min_length=1, max_length=8000)


class SteerCommand(CommandBase):
    type: Literal["command.steer"] = "command.steer"
    agent_id: str | None = None
    text: str = Field(min_length=1, max_length=1500)


class StopSessionCommand(CommandBase):
    type: Literal["command.stop"] = "command.stop"


class PauseSessionCommand(CommandBase):
    type: Literal["command.pause"] = "command.pause"


class ResumeSessionCommand(CommandBase):
    type: Literal["command.resume"] = "command.resume"


class TakeoverAgentCommand(CommandBase):
    type: Literal["command.takeover"] = "command.takeover"
    agent_id: str


class ReleaseAgentCommand(CommandBase):
    type: Literal["command.release"] = "command.release"
    agent_id: str


class DecideApprovalCommand(CommandBase):
    type: Literal["command.decide_approval"] = "command.decide_approval"
    approval_id: str
    decision: Literal["approve", "deny"]
    note: str | None = Field(default=None, max_length=1000)


class PostBoardCommand(CommandBase):
    """A user board post (kinds note/decision/answer, checked by the API)."""

    type: Literal["command.post_board"] = "command.post_board"
    entry: BoardEntryDraft


Command = Annotated[
    PlanSessionCommand
    | RefinePlanCommand
    | UpdatePlanCommand
    | ApprovePlanCommand
    | FollowupCommand
    | SteerCommand
    | StopSessionCommand
    | PauseSessionCommand
    | ResumeSessionCommand
    | TakeoverAgentCommand
    | ReleaseAgentCommand
    | DecideApprovalCommand
    | PostBoardCommand,
    Field(discriminator="type"),
]

COMMAND_ADAPTER: TypeAdapter[Command] = TypeAdapter(Command)
