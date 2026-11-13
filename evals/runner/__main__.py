"""Module CLI; invoke from repo root: uv run --project backend python -m evals.runner."""

import argparse
import asyncio
import os
from contextlib import suppress
from pathlib import Path

import httpx
from agentplatform.contracts.api import CreateSessionRequest
from agentplatform.contracts.common import SessionMode
from agentplatform.errors import AppError
from pydantic import TypeAdapter, ValidationError

from evals.runner.checks import run_check
from evals.runner.client import API
from evals.runner.definitions import EvalTask, load_tasks
from evals.runner.metrics import Metrics, compute_metrics
from evals.runner.report import RunResult, write_report


async def run_one(
    api: API,
    task: EvalTask,
    mode: SessionMode,
    repeat: int,
    profile: str,
    fixture_url: str,
    poll: float,
) -> RunResult:
    session_id: str | None = None
    try:
        created = await api.create(
            CreateSessionRequest(
                prompt=task.prompt.replace("{fixtures}", fixture_url.rstrip("/")),
                agent_count=1 if mode == "solo" else task.agent_count,
                model_profile=profile,
                mode=mode,
                budgets=task.budgets,
            )
        )
        session_id = created.id
        session = await api.wait(
            session_id,
            task.budgets.max_wall_minutes * 60,
            poll,
            approve_fixture_forms=task.key == "form-approval",
        )
        events = await api.events(session_id)
        artifacts = await api.artifacts(session_id)
        checks = [run_check(check, artifacts, events) for check in task.checks]
        score = sum(check.passed for check in checks) / len(checks)
        return RunResult(
            task=task.key,
            mode=mode,
            repeat=repeat,
            session_id=session_id,
            status=session.status,
            success=session.status == "completed" and score == 1,
            partial_score=score,
            checks=checks,
            metrics=compute_metrics(events),
        )
    except (AppError, ValidationError, ValueError):
        # Persist completed matrix rows even when one API run fails. Error text from
        # API/model data is deliberately excluded from reports.
        if session_id:
            with suppress(AppError):
                await api.stop(session_id)
        return RunResult(
            task=task.key,
            mode=mode,
            repeat=repeat,
            session_id=session_id,
            status="error",
            success=False,
            partial_score=0,
            metrics=Metrics(injection_resisted=False),
            error="API, timeout or contract validation failure; run incomplete",
        )


async def run_matrix(
    api: API,
    tasks: list[EvalTask],
    modes: list[SessionMode],
    repeats: int,
    profile: str,
    fixture_url: str,
    poll: float,
) -> list[RunResult]:
    results = []
    for task in tasks:
        for mode in modes:
            for repeat in range(1, repeats + 1):
                results.append(
                    await run_one(api, task, mode, repeat, profile, fixture_url, poll)
                )
    return results


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--tasks", type=Path, default=Path("evals/tasks"))
    result.add_argument("--modes", default="solo,parallel_isolated,coordinated")
    result.add_argument("--repeats", type=int, default=3)
    result.add_argument("--profile", default="local-free")
    result.add_argument("--api-url", default="http://127.0.0.1:8000/api/v1/")
    result.add_argument("--fixture-url", default="http://eval-fixtures")
    result.add_argument("--reports", type=Path, default=Path("evals/reports"))
    result.add_argument("--poll-seconds", type=float, default=1)
    return result


def main() -> None:
    args = parser().parse_args()
    modes = TypeAdapter(list[SessionMode]).validate_python(args.modes.split(","))
    if args.repeats < 1 or args.poll_seconds <= 0 or not modes:
        parser().error(
            "repeats and poll-seconds must be positive; modes must be nonempty"
        )
    if not args.api_url.startswith(("http://", "https://")):
        parser().error("api-url must be HTTP(S)")
    tasks = load_tasks(args.tasks)

    async def execute() -> list[RunResult]:
        token = os.environ.get("APP_API_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        async with httpx.AsyncClient(
            base_url=args.api_url.rstrip("/") + "/",
            headers=headers,
            timeout=30,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            return await run_matrix(
                API(client),
                tasks,
                modes,
                args.repeats,
                args.profile,
                args.fixture_url,
                args.poll_seconds,
            )

    results = asyncio.run(execute())
    for path in write_report(results, args.reports):
        print(path)
    raise SystemExit(0 if all(result.success for result in results) else 1)


if __name__ == "__main__":
    main()
