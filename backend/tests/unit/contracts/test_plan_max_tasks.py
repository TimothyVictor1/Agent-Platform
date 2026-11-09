"""validate_plan(max_tasks=...) and the planner error codes in PlanErrorCode (SPEC §11)."""

from __future__ import annotations

from typing import get_args

import pytest

from agentplatform.contracts.plan import MAX_TASKS, PlanError, PlanErrorCode, validate_plan
from agentplatform.runtime.planner import PlannerErrorCode
from agentplatform.testing import make_tasks

NEW_CODES = (
    "invalid_json",
    "invalid_plan",
    "invalid_task",
    "key_collision",
    "depends_on_unusable_task",
    "no_new_tasks",
)


def chain(n: int) -> list[str]:
    return [f"T{i}" for i in range(1, n + 1)]


def test_default_limit_is_still_twelve() -> None:
    assert MAX_TASKS == 12
    assert validate_plan(make_tasks(dict.fromkeys(chain(12), []))) == []
    errors = validate_plan(make_tasks(dict.fromkeys(chain(13), [])))
    assert [e.code for e in errors] == ["too_many_tasks"]
    assert "maximum is 12" in errors[0].message


def test_extend_limit_allows_twelve_new_tasks() -> None:
    existing = 10
    tasks = make_tasks(dict.fromkeys(chain(existing + MAX_TASKS), []))
    assert validate_plan(tasks, max_tasks=existing + MAX_TASKS) == []
    errors = validate_plan([*tasks, *make_tasks({"T99": []})], max_tasks=existing + MAX_TASKS)
    assert [e.code for e in errors] == ["too_many_tasks"]
    assert f"maximum is {existing + MAX_TASKS}" in errors[0].message


def test_lower_limit_is_respected() -> None:
    assert [e.code for e in validate_plan(make_tasks(dict.fromkeys(chain(3), [])), max_tasks=2)] == [
        "too_many_tasks"
    ]


@pytest.mark.parametrize("bad", [0, -1])
def test_limit_must_be_positive(bad: int) -> None:
    with pytest.raises(ValueError, match="max_tasks"):
        validate_plan(make_tasks({"T1": []}), max_tasks=bad)


def test_empty_plan_is_reported_before_the_limit() -> None:
    assert [e.code for e in validate_plan([], max_tasks=24)] == ["empty_plan"]


@pytest.mark.parametrize("code", NEW_CODES)
def test_planner_codes_are_plan_error_codes(code: str) -> None:
    assert code in get_args(PlanErrorCode)
    error = PlanError.model_validate({"code": code, "message": "x"})
    assert PlanError.model_validate_json(error.model_dump_json()) == error


def test_planner_error_codes_are_a_subset() -> None:
    assert set(get_args(PlannerErrorCode)) <= set(get_args(PlanErrorCode))
