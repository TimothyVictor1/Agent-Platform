import csv
import io
import json
from pathlib import Path

import httpx
import pytest
from agentplatform.contracts.api import (
    ApprovalList,
    ArtifactList,
    CommandAccepted,
    CreateSessionRequest,
    EventPage,
    SessionDetail,
)
from agentplatform.contracts.artifact import Approval
from agentplatform.contracts.events import SessionCompleted, SessionCreated
from agentplatform.errors import AppError, InvalidRequestError

from evals.runner.__main__ import run_matrix, run_one
from evals.runner.client import API
from evals.runner.definitions import Check, EvalTask, load_tasks
from evals.runner.metrics import Metrics
from evals.runner.report import RunResult, report_text, write_report
from evals.tests.helpers import NOW, artifact, event, session


def response(model: object) -> httpx.Response:
    from pydantic import BaseModel

    assert isinstance(model, BaseModel)
    return httpx.Response(200, content=model.model_dump_json())


@pytest.mark.asyncio
async def test_matrix_contract_lifecycle() -> None:
    created_requests: list[CreateSessionRequest] = []
    approvals: list[int | None] = []
    polls = 0
    data = b'{"code":"ALDER-42"}'

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        assert request.url.path.startswith("/api/v1/")
        path = request.url.path.removeprefix("/api/v1/")
        if path == "sessions":
            created_requests.append(
                CreateSessionRequest.model_validate_json(request.content)
            )
            polls = 0
            return response(session("planning"))
        if path == "sessions/s1":
            polls += 1
            return response(
                SessionDetail(
                    session=session("awaiting_approval" if polls == 1 else "completed")
                )
            )
        if path == "sessions/s1/approve":
            from agentplatform.contracts.api import ApprovePlanRequest

            approvals.append(
                ApprovePlanRequest.model_validate_json(request.content).plan_version
            )
            return response(CommandAccepted(command_id="cmd1"))
        if path == "sessions/s1/events":
            cursor = int(request.url.params["after_seq"])
            if cursor == 0:
                return response(
                    EventPage(
                        items=[event(1, SessionCreated(session=session("planning")))],
                        next_after_seq=1,
                    )
                )
            assert cursor == 1
            return response(EventPage(items=[event(2, SessionCompleted())]))
        if path == "sessions/s1/artifacts":
            return response(ArtifactList(items=[artifact("safe.json", data)]))
        if path == "artifacts/artifact-1/download":
            return httpx.Response(200, content=data)
        raise AssertionError(str(request.url))

    task = next(
        task for task in load_tasks(Path("evals/tasks")) if task.key == "injection-trap"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test/api/v1/"
    ) as client:
        results = await run_matrix(
            API(client),
            [task],
            ["solo", "parallel_isolated", "coordinated"],
            2,
            "local-free",
            "http://eval-fixtures",
            0.001,
        )
    assert len(results) == 6
    assert all(result.success and result.partial_score == 1 for result in results)
    assert [request.agent_count for request in created_requests] == [1, 1, 2, 2, 2, 2]
    assert approvals == [1] * 6
    assert all("{fixtures}" not in request.prompt for request in created_requests)
    assert all(request.budgets == task.budgets for request in created_requests)


@pytest.mark.asyncio
async def test_fixture_approval_opt_in() -> None:
    visited: list[str] = []
    polls = 0
    approval = Approval(
        id="p1",
        session_id="s1",
        agent_id="a",
        action_summary="fixture contact",
        risk="medium",
        created_at=NOW,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal polls
        visited.append(request.url.path)
        if request.url.path.endswith("/s1"):
            polls += 1
            return response(
                SessionDetail(session=session("running" if polls <= 2 else "completed"))
            )
        if request.url.path.endswith("/approvals"):
            return response(ApprovalList(items=[approval]))
        if request.url.path.endswith("/decide"):
            assert json.loads(request.content)["decision"] == "approve"
            return response(CommandAccepted(command_id="c"))
        raise AssertionError(str(request.url))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test/api/v1/"
    ) as client:
        await API(client).wait("s1", 1, 0.001, approve_fixture_forms=True)
        assert visited.count("/api/v1/approvals/p1/decide") == 1
        visited.clear()
        polls = 0
        await API(client).wait("s1", 1, 0.001)
        assert not any("approvals" in path for path in visited)


@pytest.mark.asyncio
async def test_timeout_stops_session() -> None:
    calls: list[str] = []
    times = iter([0.0, 0.0, 0.0, 2.0])

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/stop"):
            return response(CommandAccepted(command_id="stop"))
        return response(SessionDetail(session=session("running")))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test/api/v1/"
    ) as client:
        with pytest.raises(AppError, match="timed out"):
            await API(client).wait("s1", 1, 0.001, clock=lambda: next(times))
    assert calls[-1].endswith("/stop")


@pytest.mark.asyncio
async def test_http_error_matrix_continues() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="sensitive detail must not enter report")

    task = load_tasks(Path("evals/tasks"))[0]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test/api/v1/"
    ) as client:
        results = await run_matrix(
            API(client), [task], ["solo"], 2, "local-free", "http://f", 0.001
        )
    assert len(results) == 2
    assert all(result.status == "error" and not result.success for result in results)
    assert "sensitive detail" not in report_text(results)


@pytest.mark.asyncio
async def test_bad_pagination() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return response(EventPage(items=[], next_after_seq=1))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test/api/v1/"
    ) as client:
        with pytest.raises(InvalidRequestError, match="cursor"):
            await API(client).events("s1")


@pytest.mark.asyncio
async def test_download_latest_and_hash_validation() -> None:
    content = b"latest"
    old, latest = (
        artifact("../unsafe.csv", b"old"),
        artifact("../unsafe.csv", content, 2),
    )
    downloads: list[str] = []
    corrupt = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/artifacts"):
            return response(ArtifactList(items=[latest, old]))
        downloads.append(request.url.path)
        return httpx.Response(200, content=b"broken" if corrupt else content)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test/api/v1/"
    ) as client:
        api = API(client)
        assert await api.artifacts("s1") == {"../unsafe.csv": content}
        assert downloads == ["/api/v1/artifacts/artifact-2/download"]
        corrupt = True
        with pytest.raises(InvalidRequestError, match="hash"):
            await api.artifacts("s1")


def test_reports(tmp_path: Path) -> None:
    rows = [
        RunResult(
            task="safe",
            mode="solo",
            repeat=1,
            status="completed",
            success=True,
            partial_score=1,
            metrics=Metrics(total_steps=2, tokens=15),
        ),
        RunResult(
            task="safe",
            mode="solo",
            repeat=2,
            status="failed",
            success=False,
            partial_score=0.5,
            metrics=Metrics(total_steps=4, tokens=25),
        ),
        RunResult(
            task="=untrusted|<x>",
            mode="coordinated",
            repeat=1,
            status="error",
            success=False,
            partial_score=0,
            metrics=Metrics(),
        ),
    ]
    md, csv_path = write_report(rows, tmp_path)
    text = md.read_text()
    assert "Per mode" in text
    assert "Per task and mode" in text
    assert "50.00%" in text
    assert "0.750" in text
    assert "20.0000" in text
    assert "\\|&lt;x>" in text
    parsed = list(csv.DictReader(io.StringIO(csv_path.read_text())))
    assert len(parsed) == 3
    assert parsed[0]["tokens"] == "15"
    assert parsed[2]["task"].startswith("'=")


@pytest.mark.asyncio
async def test_cancel_wait_requests_stop() -> None:
    import asyncio

    stopped = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal stopped
        if request.url.path.endswith("/stop"):
            stopped = True
            return response(CommandAccepted(command_id="c"))
        raise asyncio.CancelledError

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test/api/v1/"
    ) as client:
        with pytest.raises(asyncio.CancelledError):
            await API(client).wait("s1", 1, 0.001)
    assert stopped


@pytest.mark.asyncio
async def test_partial_score_does_not_pass_failed_session() -> None:
    task = EvalTask(
        key="partial",
        prompt="Test",
        agent_count=1,
        budgets=load_tasks(Path("evals/tasks"))[0].budgets,
        checks=[Check(kind="injection"), Check(kind="artifact", artifact="missing")],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/sessions"):
            return response(session("planning"))
        if request.url.path.endswith("/s1"):
            return response(SessionDetail(session=session("failed")))
        if request.url.path.endswith("/events"):
            return response(EventPage(items=[]))
        if request.url.path.endswith("/artifacts"):
            return response(ArtifactList(items=[]))
        raise AssertionError(str(request.url))

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test/api/v1/"
    ) as client:
        result = await run_one(
            API(client), task, "solo", 1, "local-free", "http://f", 0.001
        )
    assert result.status == "failed"
    assert result.partial_score == 0.5
    assert not result.success
