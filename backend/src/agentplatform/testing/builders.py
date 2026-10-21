"""Builders for contract objects in tests. Every argument has a sensible default."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agentplatform.contracts.agent import Agent, agent_identity
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.common import (
    Actor,
    BoardEntryKind,
    BoardEntryStatus,
    SessionMode,
    TaskStatus,
    new_id,
    utcnow,
)
from agentplatform.contracts.plan import PlanProposal
from agentplatform.contracts.session import Budgets, Session
from agentplatform.contracts.task import InputRef, OutputSpec, Task, TaskDraft

__all__ = [
    "make_agent",
    "make_board_entry",
    "make_plan",
    "make_session",
    "make_task",
    "make_task_draft",
    "make_tasks",
]


def make_session(
    *,
    session_id: str | None = None,
    prompt: str = "Compare note apps and build a spreadsheet",
    agent_count: int = 3,
    mode: SessionMode = "coordinated",
    status: Any = "planning",
    budgets: Budgets | None = None,
    plan_version: int = 0,
    model_profile: str = "test",
) -> Session:
    return Session(
        id=session_id or new_id(),
        user_id="local",
        title=prompt[:60],
        prompt=prompt,
        status=status,
        agent_count=agent_count,
        model_profile=model_profile,
        sandbox_provider="docker_local",
        mode=mode,
        plan_version=plan_version,
        budgets=budgets or Budgets(),
        created_at=utcnow(),
    )


def make_task_draft(
    key: str = "T1",
    *,
    depends_on: Sequence[str] = (),
    inputs: Sequence[tuple[str, str]] = (),
    outputs: Sequence[OutputSpec | str] = ("result",),
    criteria: Sequence[str] = ("The result is visible on screen",),
    tags: Sequence[str] = (),
    title: str | None = None,
    goal: str | None = None,
) -> TaskDraft:
    """A task draft. ``outputs`` items that are strings become text outputs;
    ``inputs`` are ``(from_task, output_key)`` pairs."""
    specs = [
        o if isinstance(o, OutputSpec) else OutputSpec(key=o, kind="text", description=o)
        for o in outputs
    ]
    return TaskDraft(
        key=key,
        title=title or f"Task {key}",
        goal=goal or f"Do the work of task {key}.",
        acceptance_criteria=list(criteria),
        depends_on=list(depends_on),
        inputs=[InputRef(from_task=t, output_key=k) for t, k in inputs],
        outputs=specs,
        tags=list(tags),
        suggested_tools=["browser"],
    )


def make_tasks(edges: dict[str, Sequence[str]]) -> list[TaskDraft]:
    """Drafts from a dependency map, e.g. ``{"T1": [], "T2": [], "T3": ["T1", "T2"]}``.
    Each task consumes ``result`` from each dependency."""
    return [
        make_task_draft(key, depends_on=deps, inputs=[(d, "result") for d in deps])
        for key, deps in edges.items()
    ]


def make_plan(edges: dict[str, Sequence[str]] | None = None) -> PlanProposal:
    """A valid proposal; default is the 4-task DAG from SPEC §4.1."""
    edges = edges or {"T1": [], "T2": [], "T3": ["T1", "T2"], "T4": ["T3"]}
    return PlanProposal(tasks=make_tasks(edges), rationale="test plan")


def make_task(
    draft: TaskDraft | None = None,
    *,
    session_id: str = "s-test",
    plan_version: int = 1,
    status: TaskStatus = "pending",
    task_id: str | None = None,
    **draft_overrides: Any,
) -> Task:
    base = draft or make_task_draft(**draft_overrides)
    return Task(
        **base.model_dump(),
        id=task_id or new_id(),
        session_id=session_id,
        plan_version=plan_version,
        status=status,
    )


def make_agent(index: int = 0, *, session_id: str = "s-test", agent_id: str | None = None) -> Agent:
    name, color = agent_identity(index)
    return Agent(id=agent_id or new_id(), session_id=session_id, name=name, color=color)


def make_board_entry(
    kind: BoardEntryKind = "fact",
    *,
    seq: int = 1,
    session_id: str = "s-test",
    author: Actor | None = None,
    title: str = "Something verified",
    body: str = "",
    tags: Sequence[str] = (),
    to: list[str] | None = None,
    refs: Sequence[str] = (),
    task_key: str | None = None,
    status: BoardEntryStatus | None = None,
) -> BoardEntry:
    if status is None and kind in ("question", "claim", "blocker"):
        status = "open"
    return BoardEntry(
        id=new_id(),
        session_id=session_id,
        seq=seq,
        author=author or Actor.agent("a-1", "Ada"),
        kind=kind,
        title=title,
        body=body,
        tags=list(tags),
        to=to,
        refs=list(refs),
        task_key=task_key,
        status=status,
        created_at=utcnow(),
    )
