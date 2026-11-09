"""Strict, declarative task definitions; never import code named by YAML."""

from pathlib import Path
from typing import Literal

import yaml
from agentplatform.contracts.common import Contract
from agentplatform.contracts.session import Budgets
from pydantic import Field, JsonValue, model_validator


class Check(Contract):
    kind: Literal[
        "artifact",
        "spreadsheet",
        "json",
        "text",
        "injection",
        "approval",
        "handoff",
        "decision",
        "chart",
        "action",
    ]
    artifact: str | None = None
    expected: dict[str, JsonValue] = Field(default_factory=dict)
    columns: list[str] = Field(default_factory=list)
    rows: int | None = Field(default=None, ge=0)
    contains: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_target(self) -> "Check":
        if self.kind in {"artifact", "spreadsheet", "json", "text", "chart"} and (
            not self.artifact or Path(self.artifact).name != self.artifact
        ):
            raise ValueError("artifact must be a basename")
        return self


class EvalTask(Contract):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    prompt: str = Field(min_length=1)
    agent_count: int = Field(ge=1, le=6)
    budgets: Budgets
    checks: list[Check] = Field(min_length=1)


def load_tasks(directory: Path) -> list[EvalTask]:
    tasks = [
        EvalTask.model_validate(yaml.safe_load(path.read_text()))
        for path in sorted(directory.glob("*.yaml"))
    ]
    if not tasks or len({task.key for task in tasks}) != len(tasks):
        raise ValueError("task directory must contain tasks with unique keys")
    return tasks
