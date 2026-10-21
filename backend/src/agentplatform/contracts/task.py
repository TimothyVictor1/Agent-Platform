"""Tasks (DAG nodes), their inputs/outputs and results (SPEC §6.2)."""

from __future__ import annotations

from typing import Any

from pydantic import Field, JsonValue

from agentplatform.contracts.common import (
    Confidence,
    Contract,
    OutputKind,
    SuggestedTool,
    TaskCreator,
    TaskStatus,
)

__all__ = [
    "TASK_KEY_PATTERN",
    "InputRef",
    "OutputSpec",
    "Task",
    "TaskDraft",
    "TaskResult",
]

TASK_KEY_PATTERN = r"^[A-Za-z0-9_-]{1,16}$"
"""Task keys are short, stable and unique per session, e.g. ``T3``."""

OUTPUT_KEY_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,63}$"


class InputRef(Contract):
    """Wires an upstream task's output into this task's context."""

    from_task: str = Field(pattern=TASK_KEY_PATTERN)
    output_key: str = Field(pattern=OUTPUT_KEY_PATTERN)


class OutputSpec(Contract):
    """One output a task must produce in ``submit_result``.

    ``kind="artifact"`` outputs are satisfied by an artifact id (string) in
    ``TaskResult.outputs[key]`` that is also listed in ``TaskResult.artifact_ids``.
    ``json_schema`` is only meaningful for ``kind="json"``.
    """

    key: str = Field(pattern=OUTPUT_KEY_PATTERN)
    kind: OutputKind
    description: str = Field(default="", max_length=500)
    json_schema: dict[str, Any] | None = None


class TaskResult(Contract):
    summary: str = Field(max_length=4000)
    outputs: dict[str, JsonValue] = Field(default_factory=dict)
    artifact_ids: list[str] = Field(default_factory=list)
    confidence: Confidence = "medium"
    open_issues: list[str] = Field(default_factory=list)
    final_frame_id: str | None = None


class TaskDraft(Contract):
    """A task without runtime fields, as produced by the planner or edited by the user.

    Structural rules that span tasks (unique keys, known dependencies, no cycles,
    1..6 criteria, inputs wired to existing outputs) are checked by
    ``contracts.plan.validate_plan`` so they can be reported per task.
    """

    key: str = Field(pattern=TASK_KEY_PATTERN)
    title: str = Field(min_length=1, max_length=80)
    goal: str = Field(min_length=1, max_length=1200)
    acceptance_criteria: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    inputs: list[InputRef] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    suggested_tools: list[SuggestedTool] = Field(default_factory=list)
    max_attempts: int = Field(default=2, ge=1, le=5)


class Task(TaskDraft):
    """A task in a session's plan, with runtime state."""

    id: str
    session_id: str
    plan_version: int = Field(ge=1)
    status: TaskStatus = "pending"
    assigned_agent_id: str | None = None
    attempt: int = Field(default=0, ge=0)
    """Number of times this task has been started (0 before the first start)."""
    rework_count: int = Field(default=0, ge=0)
    result: TaskResult | None = None
    created_by: TaskCreator = "planner"

    def to_draft(self) -> TaskDraft:
        """Drop runtime fields."""
        return TaskDraft.model_validate(self.model_dump(include=set(TaskDraft.model_fields)))
