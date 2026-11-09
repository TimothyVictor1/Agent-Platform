from pathlib import Path

import pytest
from agentplatform.contracts.artifact import Approval
from agentplatform.contracts.events import (
    ActionRecord,
    AgentStep,
    ApprovalDecided,
    ApprovalRequested,
    ArtifactFetched,
    ArtifactPublished,
)
from pydantic import ValidationError

from evals.runner.checks import run_check, spreadsheet_rows
from evals.runner.definitions import Check, load_tasks
from evals.tests.helpers import NOW, artifact, event, ods, sample_ods


def test_eight_strict_tasks() -> None:
    tasks = load_tasks(Path("evals/tasks"))
    assert len(tasks) == 8
    assert all(task.budgets.max_cost_usd == 0 for task in tasks)
    assert all(any(check.kind == "injection" for check in task.checks) for task in tasks)
    with pytest.raises(ValidationError):
        Check.model_validate({"kind": "artifact", "artifact": "../escape"})
    with pytest.raises(ValidationError):
        Check.model_validate({"kind": "injection", "python": "arbitrary code"})
    assert run_check(Check(kind="artifact", artifact="x"), {"x": b"ok"}, []).passed
    assert not run_check(Check(kind="artifact", artifact="x"), {}, []).passed


@pytest.mark.parametrize(
    ("data", "passed"),
    [
        (b'{"version":"v3","extra":true}', True),
        (b'{"version":"v2"}', False),
        (b"broken", False),
        (b"[]", False),
    ],
)
def test_json(data: bytes, passed: bool) -> None:
    check = Check(kind="json", artifact="api.json", expected={"version": "v3"})
    assert run_check(check, {"api.json": data}, []).passed is passed


def test_csv_and_ods() -> None:
    check = Check(
        kind="spreadsheet",
        artifact="a.csv",
        columns=["quarter", "revenue"],
        rows=1,
        expected={"Q1": ["10"]},
    )
    assert run_check(check, {"a.csv": b"quarter,revenue\nQ1,10\n"}, []).passed
    assert not run_check(check, {"a.csv": b"quarter,revenue\nQ1,11\n"}, []).passed
    assert not run_check(check, {"a.csv": b"quarter,revenue\nQ1,10\nQ1,10\n"}, []).passed
    assert not run_check(check, {"a.csv": b"quarter,revenue\nQ1\n"}, []).passed
    assert spreadsheet_rows("a.ods", sample_ods()) == [["quarter", "revenue"], ["Q1", "10"]]
    ods_check = check.model_copy(update={"artifact": "a.ods"})
    assert run_check(ods_check, {"a.ods": sample_ods()}, []).passed
    assert not run_check(ods_check, {"a.ods": b"not a zip"}, []).passed


def test_ods_xml_and_repetition_limits() -> None:
    check = Check(kind="spreadsheet", artifact="a.ods", columns=["x"])
    for content in [
        '<!DOCTYPE x [<!ENTITY boom "bad">]><x>&boom;</x>',
        '<x xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
        '<table:table><table:table-row table:number-rows-repeated="999999">'
        "<table:table-cell/></table:table-row></table:table></x>",
    ]:
        assert not run_check(check, {"a.ods": ods(content)}, []).passed
    assert not run_check(check, {"a.ods": ods("<broken")}, []).passed


def test_chart_requires_embedded_chart() -> None:
    check = Check(kind="chart", artifact="chart.ods")
    assert run_check(check, {"chart.ods": sample_ods(True)}, []).passed
    assert not run_check(check, {"chart.ods": sample_ods()}, []).passed


def test_text() -> None:
    check = Check(kind="text", artifact="summary.md", contains=["Dune"])
    assert run_check(check, {"summary.md": b"DUNE is cheapest"}, []).passed
    assert not run_check(check, {"summary.md": b"Missing"}, []).passed


def test_approval_and_handoff() -> None:
    approval = Approval(
        id="p1",
        session_id="s1",
        agent_id="a",
        action_summary="Submit fixture",
        risk="medium",
        created_at=NOW,
    )
    request = event(1, ApprovalRequested(approval=approval))
    decision = event(
        2, ApprovalDecided(approval=approval.model_copy(update={"status": "approved"}))
    )
    check = Check(kind="approval")
    assert run_check(check, {}, [decision, request]).passed
    assert not run_check(check, {}, [decision]).passed
    assert not run_check(check, {}, [request]).passed
    data = b"x"
    published = event(1, ArtifactPublished(artifact=artifact("a.csv", data)))
    fetched = event(
        2, ArtifactFetched(artifact_id="artifact-1", agent_id="b", sandbox_path="/home/agent/a.csv")
    )
    assert run_check(Check(kind="handoff"), {}, [published, fetched]).passed
    assert not run_check(Check(kind="handoff"), {}, [fetched]).passed


def test_action_check() -> None:
    step = event(
        1,
        AgentStep(
            agent_id="a",
            task_id="t",
            task_key="T1",
            step=1,
            action=ActionRecord(tool="run_command", args={"cmd": "echo test"}),
        ),
    )
    check = Check(kind="action", contains=["run_command"])
    assert run_check(check, {}, [step]).passed
    assert not run_check(check, {}, []).passed


def test_supervisor_decision() -> None:
    from agentplatform.contracts.board import BoardEntry
    from agentplatform.contracts.common import Actor
    from agentplatform.contracts.events import BoardEntryPosted

    entry = BoardEntry(
        id="decision",
        session_id="s1",
        seq=1,
        kind="decision",
        title="Resolution",
        body="2021 is supported by the official register",
        author=Actor.supervisor(),
        created_at=NOW,
    )
    check = Check(kind="decision", contains=["2021"])
    assert run_check(check, {}, [event(1, BoardEntryPosted(entry=entry))]).passed
    agent_entry = entry.model_copy(update={"author": Actor.agent("a", "Ada")})
    assert not run_check(check, {}, [event(1, BoardEntryPosted(entry=agent_entry))]).passed
