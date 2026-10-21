"""Contracts: Pydantic models shared by the API, the runtime and (via generated
TypeScript) the frontend. This package is the source of truth (SPEC §5.1 decision 6).

Only the T00 task and "T00.x contracts patch" tasks may edit this package.
"""

from __future__ import annotations

from pydantic import BaseModel

from agentplatform.contracts import api, commands, events
from agentplatform.contracts.agent import (
    AGENT_COLORS,
    AGENT_NAMES,
    Agent,
    SandboxRef,
    agent_identity,
)
from agentplatform.contracts.artifact import Approval, Artifact, Frame
from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.commands import COMMAND_ADAPTER, Command
from agentplatform.contracts.common import Actor, Contract, new_id, utcnow
from agentplatform.contracts.events import (
    EPHEMERAL_ADAPTER,
    EVENT_ADAPTER,
    EphemeralMessage,
    Event,
    EventDraft,
    build_event,
)
from agentplatform.contracts.plan import (
    Plan,
    PlanDiff,
    PlanError,
    PlanProposal,
    topological_order,
    validate_plan,
)
from agentplatform.contracts.session import Budgets, Session, Usage
from agentplatform.contracts.task import InputRef, OutputSpec, Task, TaskDraft, TaskResult

__all__ = [
    "AGENT_COLORS",
    "AGENT_NAMES",
    "COMMAND_ADAPTER",
    "EPHEMERAL_ADAPTER",
    "EVENT_ADAPTER",
    "PUBLIC_MODELS",
    "Actor",
    "Agent",
    "Approval",
    "Artifact",
    "BoardEntry",
    "BoardEntryDraft",
    "Budgets",
    "Command",
    "Contract",
    "EphemeralMessage",
    "Event",
    "EventDraft",
    "Frame",
    "InputRef",
    "OutputSpec",
    "Plan",
    "PlanDiff",
    "PlanError",
    "PlanProposal",
    "SandboxRef",
    "Session",
    "Task",
    "TaskDraft",
    "TaskResult",
    "Usage",
    "agent_identity",
    "build_event",
    "new_id",
    "topological_order",
    "utcnow",
    "validate_plan",
]


def _public_models() -> list[type[BaseModel]]:
    models: list[type[BaseModel]] = [
        Session,
        Budgets,
        Usage,
        Task,
        TaskDraft,
        InputRef,
        OutputSpec,
        TaskResult,
        Plan,
        PlanProposal,
        PlanError,
        PlanDiff,
        Agent,
        SandboxRef,
        BoardEntry,
        BoardEntryDraft,
        Actor,
        Artifact,
        Approval,
        Frame,
    ]
    models += list(events.EVENT_TYPES.values())
    models += list(events.PAYLOAD_TYPES.values())
    models += [events.ActionRecord, events.Heartbeat, events.Thumbnail]
    models += [getattr(api, name) for name in api.__all__ if isinstance(getattr(api, name), type)]
    models += [
        getattr(commands, name)
        for name in commands.__all__
        if isinstance(getattr(commands, name), type)
        and issubclass(getattr(commands, name), BaseModel)
    ]
    return models


PUBLIC_MODELS: list[type[BaseModel]] = _public_models()
"""Models exported to JSON Schema / TypeScript (``scripts/gen_contracts.py``).

The unions ``Event``, ``Command``, ``WsServerMessage`` and ``EphemeralMessage`` are
exported in addition to these by the generator."""
