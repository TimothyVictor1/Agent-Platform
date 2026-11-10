"""Pure scheduler helpers: ready set, critical path, assignment policy, upstream
inputs, retry policy, stall detection, session budget and the event fold."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import Any

from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.common import Actor, utcnow
from agentplatform.contracts.events import (
    EventDraft,
    TaskAssigned,
    TaskFailed,
    TaskReady,
    TaskStarted,
    build_event,
)
from agentplatform.contracts.session import Budgets, Usage
from agentplatform.contracts.task import Task, TaskResult
from agentplatform.runtime.scheduler import (
    SchedulerState,
    SessionBudget,
    descendant_counts,
    pick_assignments,
    ready_keys,
    resolve_upstream,
    retry_decision,
    stalled,
)
from agentplatform.testing import make_session, make_task, make_tasks


def dag(edges: Mapping[str, Sequence[str]], **status: Any) -> dict[str, Task]:
    return {
        d.key: make_task(d, status=status.get(d.key, "pending"), task_id=f"id-{d.key}")
        for d in make_tasks(dict(edges))
    }


SPEC = {"T1": [], "T2": [], "T3": ["T1", "T2"], "T4": ["T3"]}


def test_ready_keys_follow_dependencies() -> None:
    assert ready_keys(dag(SPEC)) == ["T1", "T2"]
    assert ready_keys(dag(SPEC, T1="done")) == ["T2"]
    assert ready_keys(dag(SPEC, T1="done", T2="done")) == ["T3"]
    # running/ready tasks are not "pending", and a missing dependency never becomes ready
    assert ready_keys(dag(SPEC, T1="running", T2="ready")) == []
    assert ready_keys(dag({"T1": ["T9"]})) == []


def test_descendant_counts_ignore_finished_tasks() -> None:
    assert descendant_counts(dag(SPEC)) == {"T1": 2, "T2": 2, "T3": 1, "T4": 0}
    assert descendant_counts(dag(SPEC, T4="done"))["T1"] == 1


def test_pick_prefers_affinity_then_critical_path_then_plan_order() -> None:
    tasks = dag({"T1": [], "T2": [], "T3": ["T1"], "T4": [], "T5": ["T4"], "T6": ["T5"]})
    ready = [tasks["T3"], tasks["T4"]]
    desc = descendant_counts(tasks)
    # affinity: Cyra did T1, so she gets T3 even though Ada comes first
    pairs = pick_assignments(ready, ["Ada", "Cyra"], done_by={"T1": "Cyra"}, descendants=desc)
    assert [(t.key, a) for t, a in pairs] == [("T3", "Cyra"), ("T4", "Ada")]
    # no affinity: the critical-path task (T4 unblocks two) goes first, to the first agent
    pairs = pick_assignments(ready, ["Ada", "Cyra"], done_by={}, descendants=desc)
    assert [(t.key, a) for t, a in pairs] == [("T4", "Ada"), ("T3", "Cyra")]
    # one agent: affinity still beats the critical path
    pairs = pick_assignments(ready, ["Cyra"], done_by={"T1": "Cyra"}, descendants=desc)
    assert [(t.key, a) for t, a in pairs] == [("T3", "Cyra")]
    # equal tasks: plan order
    two = dag({"T1": [], "T2": []})
    pairs = pick_assignments(list(two.values()), ["Ada"], done_by={}, descendants={})
    assert [t.key for t, _ in pairs] == ["T1"]


def test_pick_avoids_agents_that_timed_out_and_honours_rework_preference() -> None:
    tasks = dag({"T1": []})
    ready = [tasks["T1"]]
    pairs = pick_assignments(
        ready, ["Ada", "Brook"], done_by={}, descendants={}, avoid={"T1": ["Ada"]}
    )
    assert pairs[0][1] == "Brook"
    # an avoided agent is still used when it is the only one
    pairs = pick_assignments(ready, ["Ada"], done_by={}, descendants={}, avoid={"T1": ["Ada"]})
    assert pairs[0][1] == "Ada"
    pairs = pick_assignments(
        ready, ["Ada", "Brook"], done_by={}, descendants={}, preferred={"T1": "Brook"}
    )
    assert pairs[0][1] == "Brook"


def test_more_agents_than_tasks_assigns_each_task_once() -> None:
    tasks = dag({"T1": []})
    pairs = pick_assignments([tasks["T1"]], ["Ada", "Brook", "Cyra"], done_by={}, descendants={})
    assert len(pairs) == 1


def test_resolve_upstream_values_and_artifacts() -> None:
    tasks = dag({"T1": [], "T2": ["T1"]}, T1="done")
    tasks["T1"] = tasks["T1"].model_copy(
        update={
            "result": TaskResult(
                summary="ok", outputs={"result": [1, 2]}, artifact_ids=["a1", "missing"]
            )
        }
    )
    art = Artifact(
        id="a1",
        session_id="s-test",
        name="table.csv",
        mime="text/csv",
        size_bytes=3,
        sha256="0" * 64,
        produced_by=Actor.agent("x", "Ada"),
        storage_key="sha256/00/" + "0" * 64,
        version=1,
        created_at=utcnow(),
    )
    up = resolve_upstream(tasks["T2"], tasks, {"a1": art})
    assert [(v.from_task, v.output_key, v.value) for v in up.values] == [("T1", "result", [1, 2])]
    assert [a.id for a in up.artifacts] == ["a1"]
    # nothing to resolve before the upstream task has a result
    assert resolve_upstream(tasks["T2"], dag({"T1": [], "T2": ["T1"]}), {}).values == []


def test_retry_decision() -> None:
    t = make_task(make_tasks({"T1": []})[0])
    assert retry_decision(t, 1, "fail_task: gave up") is True
    assert retry_decision(t, 2, "fail_task: gave up") is False  # max_attempts = 2
    assert retry_decision(t, 1, "budget: max_steps_per_task (150) reached") is False
    reworked = t.model_copy(update={"rework_count": 1, "attempt": 2})
    assert retry_decision(reworked, 2, "model_error") is True  # the reworked run is free


def test_stalled() -> None:
    assert not stalled(dag(SPEC))
    assert not stalled(dag(SPEC, T1="running"))
    assert stalled(dag(SPEC, T1="failed", T2="done"))  # T3/T4 blocked by T1
    assert not stalled(dag(SPEC, T1="failed", T2="running"))
    assert not stalled(dag({"T1": []}, T1="done"))  # finished, not stalled


def test_session_budget_metrics_and_exceeded() -> None:
    now = utcnow()
    clock = [now]
    budget = SessionBudget(
        Budgets(max_total_steps=10, max_tokens=1000, max_cost_usd=1.0, max_wall_minutes=5),
        started_at=now,
        now=lambda: clock[0],
    )
    assert budget.exceeded() is None
    budget.record(Usage(steps=8, input_tokens=100, output_tokens=50, cost_usd=0.5))
    metrics = {m: (used, limit) for m, used, limit in budget.metrics()}
    assert metrics["total_steps"] == (8.0, 10.0)
    assert metrics["tokens"] == (150.0, 1000.0)
    assert metrics["cost_usd"] == (0.5, 1.0)
    clock[0] = now + timedelta(minutes=6)
    assert budget.exceeded() == "wall_minutes budget (5) reached"


def _status(state: SchedulerState) -> str:
    return state.tasks["T1"].status


def _ev(seq: int, payload: Any) -> Any:
    return build_event(EventDraft(payload=payload, actor=Actor.system()), session_id="s", seq=seq)


def test_fold_ignores_stale_attempt_events() -> None:
    state = SchedulerState.for_session(make_session(session_id="s"))
    task = make_task(make_tasks({"T1": []})[0], session_id="s", task_id="t1")
    state.tasks["T1"] = task
    state.key_by_id["t1"] = "T1"
    later = utcnow() + timedelta(seconds=10)
    state.apply(_ev(1, TaskAssigned(task_id="t1", task_key="T1", agent_id="a", attempt=1,
                                    ack_deadline=later, lease_expires_at=later)))  # fmt: skip
    state.apply(_ev(2, TaskReady(task_id="t1", task_key="T1")))  # ack timeout
    assert state.avoid == {"T1": {"a"}}
    state.apply(_ev(3, TaskAssigned(task_id="t1", task_key="T1", agent_id="b", attempt=1,
                                    ack_deadline=later, lease_expires_at=later)))  # fmt: skip
    # the late agent's start and failure are ignored: b owns the task now
    state.apply(_ev(4, TaskStarted(task_id="t1", task_key="T1", agent_id="a", attempt=1)))
    state.apply(_ev(5, TaskFailed(task_id="t1", task_key="T1", reason="x", attempt=1,
                                  will_retry=True, agent_id="a")))  # fmt: skip
    assert _status(state) == "assigned"
    assert state.assignments["T1"].agent_id == "b"
    state.apply(_ev(6, TaskStarted(task_id="t1", task_key="T1", agent_id="b", attempt=1)))
    assert _status(state) == "running"
    assert state.tasks["T1"].attempt == 1
    assert state.last_seq == 6
