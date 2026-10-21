"""Plans: proposals, versions, diffs and DAG validation (SPEC §6.2, §11)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from pydantic import Field, JsonValue

from agentplatform.contracts.common import Contract, UtcDatetime
from agentplatform.contracts.task import TaskDraft

__all__ = [
    "MAX_CRITERIA",
    "MAX_TASKS",
    "FieldChange",
    "Plan",
    "PlanDiff",
    "PlanError",
    "PlanErrorCode",
    "PlanProposal",
    "TaskChange",
    "topological_order",
    "validate_plan",
]

MAX_TASKS = 12
MAX_CRITERIA = 6

PlanErrorCode = Literal[
    "empty_plan",
    "too_many_tasks",
    "duplicate_key",
    "unknown_dependency",
    "self_dependency",
    "cycle",
    "empty_criteria",
    "too_many_criteria",
    "blank_criterion",
    "duplicate_output_key",
    "invalid_json_schema",
    "json_schema_on_non_json",
    "unknown_input_task",
    "input_not_dependency",
    "unknown_input_output",
]


class PlanError(Contract):
    """One problem found by ``validate_plan``.

    ``task_key`` is set when the problem belongs to one task (UI shows it on that
    node). ``cycle`` holds the cycle path for ``code="cycle"``, first key repeated
    at the end, e.g. ``["T1", "T2", "T1"]``.
    """

    code: PlanErrorCode
    message: str
    task_key: str | None = None
    field: str | None = None
    cycle: list[str] | None = None


class PlanProposal(Contract):
    """Planner output (SPEC §11.1), also the stored body of every plan version."""

    tasks: list[TaskDraft]
    rationale: str = Field(default="", max_length=4000)
    assumptions: list[str] = Field(default_factory=list)
    questions_for_user: list[str] = Field(default_factory=list, max_length=3)


class Plan(Contract):
    """A stored plan version of a session."""

    session_id: str
    version: int = Field(ge=1)
    proposal: PlanProposal
    created_at: UtcDatetime
    approved_at: UtcDatetime | None = None


class FieldChange(Contract):
    field: str
    before: JsonValue
    after: JsonValue


class TaskChange(Contract):
    key: str
    fields: list[FieldChange]


class PlanDiff(Contract):
    """Difference between two proposals, matched by task key (computed by the planner)."""

    added: list[TaskDraft] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[TaskChange] = Field(default_factory=list)


def validate_plan(tasks: Sequence[TaskDraft]) -> list[PlanError]:
    """Check the cross-task rules of a plan. Returns an empty list when valid.

    Rules: at least one and at most ``MAX_TASKS`` tasks; unique keys; every
    ``depends_on`` key exists and is not the task itself; no cycles (each cycle is
    reported once with its path); 1..``MAX_CRITERIA`` non-blank acceptance criteria;
    unique output keys per task; ``json_schema`` only on json outputs and must be a
    valid JSON Schema; every input references a task in ``depends_on`` and an output
    key that task declares.
    """
    errors: list[PlanError] = []
    if not tasks:
        return [PlanError(code="empty_plan", message="The plan has no tasks.")]
    if len(tasks) > MAX_TASKS:
        errors.append(
            PlanError(
                code="too_many_tasks",
                message=f"The plan has {len(tasks)} tasks; the maximum is {MAX_TASKS}.",
            )
        )

    by_key: dict[str, TaskDraft] = {}
    for task in tasks:
        if task.key in by_key:
            errors.append(
                PlanError(
                    code="duplicate_key",
                    task_key=task.key,
                    field="key",
                    message=f"Task key {task.key!r} is used more than once.",
                )
            )
        else:
            by_key[task.key] = task

    for task in tasks:
        errors.extend(_task_local_errors(task))
        for dep in task.depends_on:
            if dep == task.key:
                errors.append(
                    PlanError(
                        code="self_dependency",
                        task_key=task.key,
                        field="depends_on",
                        message=f"Task {task.key} depends on itself.",
                    )
                )
            elif dep not in by_key:
                errors.append(
                    PlanError(
                        code="unknown_dependency",
                        task_key=task.key,
                        field="depends_on",
                        message=f"Task {task.key} depends on unknown task {dep!r}.",
                    )
                )
        errors.extend(_input_errors(task, by_key))

    errors.extend(_cycle_errors(by_key))
    return errors


def _task_local_errors(task: TaskDraft) -> list[PlanError]:
    errors: list[PlanError] = []
    criteria = task.acceptance_criteria
    if not criteria:
        errors.append(
            PlanError(
                code="empty_criteria",
                task_key=task.key,
                field="acceptance_criteria",
                message=f"Task {task.key} needs at least one acceptance criterion.",
            )
        )
    elif len(criteria) > MAX_CRITERIA:
        errors.append(
            PlanError(
                code="too_many_criteria",
                task_key=task.key,
                field="acceptance_criteria",
                message=f"Task {task.key} has {len(criteria)} acceptance criteria; "
                f"the maximum is {MAX_CRITERIA}.",
            )
        )
    if any(not c.strip() for c in criteria):
        errors.append(
            PlanError(
                code="blank_criterion",
                task_key=task.key,
                field="acceptance_criteria",
                message=f"Task {task.key} has a blank acceptance criterion.",
            )
        )

    seen: set[str] = set()
    for output in task.outputs:
        if output.key in seen:
            errors.append(
                PlanError(
                    code="duplicate_output_key",
                    task_key=task.key,
                    field="outputs",
                    message=f"Task {task.key} declares output {output.key!r} twice.",
                )
            )
        seen.add(output.key)
        if output.json_schema is not None:
            if output.kind != "json":
                errors.append(
                    PlanError(
                        code="json_schema_on_non_json",
                        task_key=task.key,
                        field="outputs",
                        message=f"Output {output.key!r} of task {task.key} has a JSON schema "
                        f"but kind {output.kind!r}; only json outputs take a schema.",
                    )
                )
            else:
                try:
                    Draft202012Validator.check_schema(output.json_schema)
                except SchemaError as exc:
                    errors.append(
                        PlanError(
                            code="invalid_json_schema",
                            task_key=task.key,
                            field="outputs",
                            message=f"Output {output.key!r} of task {task.key} has an invalid "
                            f"JSON schema: {exc.message}",
                        )
                    )
    return errors


def _input_errors(task: TaskDraft, by_key: dict[str, TaskDraft]) -> list[PlanError]:
    errors: list[PlanError] = []
    for ref in task.inputs:
        upstream = by_key.get(ref.from_task)
        if upstream is None:
            errors.append(
                PlanError(
                    code="unknown_input_task",
                    task_key=task.key,
                    field="inputs",
                    message=f"Task {task.key} takes input from unknown task {ref.from_task!r}.",
                )
            )
            continue
        if ref.from_task not in task.depends_on:
            errors.append(
                PlanError(
                    code="input_not_dependency",
                    task_key=task.key,
                    field="inputs",
                    message=f"Task {task.key} takes input from {ref.from_task} but does not "
                    f"list it in depends_on.",
                )
            )
        if all(o.key != ref.output_key for o in upstream.outputs):
            errors.append(
                PlanError(
                    code="unknown_input_output",
                    task_key=task.key,
                    field="inputs",
                    message=f"Task {task.key} takes output {ref.output_key!r} from "
                    f"{ref.from_task}, which does not declare it.",
                )
            )
    return errors


def _cycle_errors(by_key: dict[str, TaskDraft]) -> list[PlanError]:
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(by_key, white)
    stack: list[str] = []
    seen_cycles: set[frozenset[str]] = set()
    errors: list[PlanError] = []

    def visit(key: str) -> None:
        color[key] = gray
        stack.append(key)
        for dep in by_key[key].depends_on:
            if dep not in by_key or dep == key:
                continue  # reported elsewhere
            if color[dep] == gray:
                path = [*stack[stack.index(dep) :], dep]
                ident = frozenset(path)
                if ident not in seen_cycles:
                    seen_cycles.add(ident)
                    errors.append(
                        PlanError(
                            code="cycle",
                            task_key=dep,
                            field="depends_on",
                            cycle=path,
                            message="Dependency cycle: " + " -> ".join(path) + ".",
                        )
                    )
            elif color[dep] == white:
                visit(dep)
        stack.pop()
        color[key] = black

    for key in by_key:
        if color[key] == white:
            visit(key)
    return errors


def topological_order(tasks: Sequence[TaskDraft]) -> list[str]:
    """Return task keys so every task comes after its dependencies.

    Ties keep the input order (stable Kahn's algorithm). Unknown dependency keys are
    ignored. Raises ``ValueError`` if the plan has a cycle.
    """
    keys = [t.key for t in tasks]
    known = set(keys)
    deps = {t.key: {d for d in t.depends_on if d in known and d != t.key} for t in tasks}
    order: list[str] = []
    done: set[str] = set()
    while len(order) < len(keys):
        progressed = False
        for key in keys:
            if key not in done and deps[key] <= done:
                order.append(key)
                done.add(key)
                progressed = True
                break
        if not progressed:
            raise ValueError("plan has a dependency cycle")
    return order
