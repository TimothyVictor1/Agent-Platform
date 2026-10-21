"""validate_plan and topological_order (SPEC §6.2, §11.3)."""

from __future__ import annotations

import pytest

from agentplatform.contracts.plan import MAX_TASKS, PlanError, topological_order, validate_plan
from agentplatform.contracts.task import InputRef, OutputSpec, TaskDraft
from agentplatform.testing import make_plan, make_task_draft, make_tasks


def codes(errors: list[PlanError]) -> list[str]:
    return [e.code for e in errors]


def test_valid_spec_example_plan() -> None:
    assert validate_plan(make_plan().tasks) == []


def test_empty_plan() -> None:
    assert codes(validate_plan([])) == ["empty_plan"]


def test_too_many_tasks() -> None:
    tasks = make_tasks({f"T{i}": [] for i in range(1, MAX_TASKS + 2)})
    assert "too_many_tasks" in codes(validate_plan(tasks))
    assert validate_plan(tasks[:MAX_TASKS]) == []


def test_duplicate_keys() -> None:
    errors = validate_plan([make_task_draft("T1"), make_task_draft("T1")])
    assert codes(errors) == ["duplicate_key"]
    assert errors[0].task_key == "T1"


def test_unknown_and_self_dependency() -> None:
    errors = validate_plan(
        [make_task_draft("T1", depends_on=["T9"]), make_task_draft("T2", depends_on=["T2"])]
    )
    assert sorted(codes(errors)) == ["self_dependency", "unknown_dependency"]
    by_code = {e.code: e for e in errors}
    assert by_code["unknown_dependency"].task_key == "T1"
    assert "T9" in by_code["unknown_dependency"].message
    assert by_code["self_dependency"].task_key == "T2"


def test_cycle_reports_path() -> None:
    tasks = make_tasks({"T1": ["T3"], "T2": ["T1"], "T3": ["T2"], "T4": []})
    errors = [e for e in validate_plan(tasks) if e.code == "cycle"]
    assert len(errors) == 1
    cycle = errors[0].cycle
    assert cycle is not None
    assert cycle[0] == cycle[-1]
    assert set(cycle) == {"T1", "T2", "T3"}
    assert len(cycle) == 4
    assert "->" in errors[0].message


def test_two_separate_cycles_reported_once_each() -> None:
    tasks = make_tasks({"A": ["B"], "B": ["A"], "C": ["D"], "D": ["C"], "E": ["A"]})
    cycles = [e.cycle for e in validate_plan(tasks) if e.code == "cycle"]
    assert len(cycles) == 2
    assert {frozenset(c or []) for c in cycles} == {frozenset({"A", "B"}), frozenset({"C", "D"})}


def test_criteria_rules() -> None:
    errors = validate_plan(
        [
            make_task_draft("T1", criteria=[]),
            make_task_draft("T2", criteria=[f"c{i}" for i in range(7)]),
            make_task_draft("T3", criteria=["ok", "   "]),
        ]
    )
    assert {(e.code, e.task_key) for e in errors} == {
        ("empty_criteria", "T1"),
        ("too_many_criteria", "T2"),
        ("blank_criterion", "T3"),
    }


def test_output_rules() -> None:
    bad_schema = OutputSpec(key="rows", kind="json", json_schema={"type": "nonsense"})
    on_text = OutputSpec(key="note", kind="text", json_schema={"type": "string"})
    dup = [OutputSpec(key="x", kind="text"), OutputSpec(key="x", kind="text")]
    errors = validate_plan(
        [
            make_task_draft("T1", outputs=[bad_schema]),
            make_task_draft("T2", outputs=[on_text]),
            make_task_draft("T3", outputs=dup),
        ]
    )
    assert {(e.code, e.task_key) for e in errors} == {
        ("invalid_json_schema", "T1"),
        ("json_schema_on_non_json", "T2"),
        ("duplicate_output_key", "T3"),
    }


def test_valid_json_schema_output() -> None:
    schema = {"type": "array", "items": {"type": "object", "required": ["app"]}}
    task = make_task_draft("T1", outputs=[OutputSpec(key="rows", kind="json", json_schema=schema)])
    assert validate_plan([task]) == []


def test_input_rules() -> None:
    t1 = make_task_draft("T1", outputs=["rows"])
    t2 = make_task_draft("T2", depends_on=["T1"], inputs=[("T1", "missing")])
    t3 = make_task_draft("T3", inputs=[("T1", "rows")])  # not in depends_on
    t4 = make_task_draft("T4", inputs=[("T7", "rows")])
    errors = validate_plan([t1, t2, t3, t4])
    assert {(e.code, e.task_key) for e in errors} == {
        ("unknown_input_output", "T2"),
        ("input_not_dependency", "T3"),
        ("unknown_input_task", "T4"),
    }


def test_errors_are_serializable_for_problem_json() -> None:
    errors = validate_plan(make_tasks({"T1": ["T2"], "T2": ["T1"]}))
    dumped = [e.model_dump(mode="json", exclude_none=True) for e in errors]
    assert dumped[0]["code"] == "cycle"
    assert dumped[0]["cycle"][0] == dumped[0]["cycle"][-1]


def test_taskdraft_field_limits() -> None:
    with pytest.raises(ValueError, match="80"):
        TaskDraft(key="T1", title="x" * 81, goal="g")
    with pytest.raises(ValueError, match="pattern"):
        TaskDraft(key="bad key", title="t", goal="g")
    with pytest.raises(ValueError, match="pattern"):
        InputRef(from_task="T1", output_key="9bad")


def test_topological_order_is_stable() -> None:
    tasks = make_tasks({"T4": ["T3"], "T3": ["T1", "T2"], "T1": [], "T2": []})
    assert topological_order(tasks) == ["T1", "T2", "T3", "T4"]
    assert topological_order(make_tasks({"B": [], "A": []})) == ["B", "A"]


def test_topological_order_rejects_cycle() -> None:
    with pytest.raises(ValueError, match="cycle"):
        topological_order(make_tasks({"T1": ["T2"], "T2": ["T1"]}))
