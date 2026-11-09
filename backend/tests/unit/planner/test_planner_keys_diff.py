"""normalize_keys, next_key_number and diff_plans."""

from __future__ import annotations

from agentplatform.contracts.plan import validate_plan
from agentplatform.contracts.task import InputRef, OutputSpec
from agentplatform.runtime.planner import diff_plans, next_key_number, normalize_keys
from agentplatform.testing.builders import make_plan, make_task_draft, make_tasks


def test_normalize_renames_in_topological_order_and_rewrites_references() -> None:
    tasks = [
        make_task_draft("sheet", depends_on=["b", "a", "a"], inputs=[("a", "result")]),
        make_task_draft("a"),
        make_task_draft("b", depends_on=["a"], inputs=[("a", "result")]),
    ]
    out = normalize_keys(tasks)
    assert [t.key for t in out] == ["T1", "T2", "T3"]
    assert [t.title for t in out] == ["Task a", "Task b", "Task sheet"]
    assert out[1].depends_on == ["T1"]
    assert out[2].depends_on == ["T2", "T1"]  # de-duplicated, order kept
    assert out[2].inputs == [InputRef(from_task="T1", output_key="result")]
    assert validate_plan(out) == []


def test_normalize_handles_swapped_t_keys() -> None:
    tasks = make_tasks({"T2": [], "T1": ["T2"]})
    out = normalize_keys(tasks)
    assert [(t.key, t.title, t.depends_on) for t in out] == [
        ("T1", "Task T2", []),
        ("T2", "Task T1", ["T1"]),
    ]


def test_normalize_keep_preserves_and_reserves_names() -> None:
    tasks = [
        make_task_draft("T1"),
        make_task_draft("new", depends_on=["T1"]),
        make_task_draft("T3", depends_on=["new"]),
    ]
    out = normalize_keys(tasks, keep=["T1", "T2", "T3"], start=4)
    assert [t.key for t in out] == ["T1", "T4", "T3"]
    assert out[2].depends_on == ["T4"]


def test_normalize_leaves_external_references() -> None:
    tasks = [make_task_draft("x", depends_on=["T7"], inputs=[("T7", "result")])]
    out = normalize_keys(tasks, keep=["T7"], start=8)
    assert out[0].key == "T8"
    assert out[0].depends_on == ["T7"]
    assert out[0].inputs[0].from_task == "T7"


def test_next_key_number() -> None:
    assert next_key_number([]) == 1
    assert next_key_number(make_tasks({"T1": [], "T9": [], "scrape": []})) == 10
    assert next_key_number(make_tasks({"a": []})) == 1


def test_diff_identical_is_empty() -> None:
    plan = make_plan()
    diff = diff_plans(plan.tasks, list(plan.tasks))
    assert diff.added == []
    assert diff.removed == []
    assert diff.changed == []


def test_diff_added_removed_changed_with_field_values() -> None:
    before = make_plan().tasks  # T1, T2, T3(T1,T2), T4(T3)
    t3 = before[2]
    after = [
        before[0],
        before[1].model_copy(update={"title": "Renamed", "acceptance_criteria": ["One", "Two"]}),
        t3.model_copy(
            update={
                "outputs": [OutputSpec(key="result", kind="json", description="rows")],
                "tags": ["pricing"],
            }
        ),
        make_task_draft("T5", depends_on=["T3"], inputs=[("T3", "result")]),
    ]
    diff = diff_plans(before, after)
    assert [t.key for t in diff.added] == ["T5"]
    assert diff.removed == ["T4"]
    assert [c.key for c in diff.changed] == ["T2", "T3"]

    t2_fields = {f.field: (f.before, f.after) for f in diff.changed[0].fields}
    assert t2_fields == {
        "title": ("Task T2", "Renamed"),
        "acceptance_criteria": (["The result is visible on screen"], ["One", "Two"]),
    }
    t3_fields = {f.field: (f.before, f.after) for f in diff.changed[1].fields}
    assert set(t3_fields) == {"outputs", "tags"}
    assert t3_fields["tags"] == ([], ["pricing"])
    assert t3_fields["outputs"][1] == [
        {"key": "result", "kind": "json", "description": "rows", "json_schema": None}
    ]


def test_diff_dependency_change_is_reported() -> None:
    before = make_tasks({"T1": [], "T2": []})
    after = [before[0], make_task_draft("T2", depends_on=["T1"])]
    diff = diff_plans(before, after)
    assert [(c.key, [f.field for f in c.fields]) for c in diff.changed] == [("T2", ["depends_on"])]


def test_normalize_skips_reserved_names() -> None:
    out = normalize_keys(make_tasks({"a": [], "b": []}), keep=["T1", "T3"])
    assert [t.key for t in out] == ["T2", "T4"]
