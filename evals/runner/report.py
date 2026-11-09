"""Machine-readable run rows and aggregate Markdown comparisons."""

import csv
import io
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean

from agentplatform.contracts.common import Contract, SessionMode
from pydantic import Field

from evals.runner.checks import CheckResult
from evals.runner.metrics import Metrics


class RunResult(Contract):
    task: str
    mode: SessionMode
    repeat: int
    session_id: str | None = None
    status: str
    success: bool
    partial_score: float
    metrics: Metrics
    checks: list[CheckResult] = Field(default_factory=list)
    error: str | None = None


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").replace("\r", " ").replace("<", "&lt;")


def report_text(results: Sequence[RunResult]) -> str:
    lines = [
        "# Evaluation comparison",
        "",
        "Success requires completed status and all checks. Partial score is the fraction of checks",
        "passed; errors score zero. URL/claim overlap is an observable proxy, not full navigation",
        "or semantic duplication. Tokens/cost cover agent.step usage only.",
        "",
    ]
    for per_task in (False, True):
        lines += [
            "## " + ("Per task and mode" if per_task else "Per mode"),
            "",
            "| Task | Mode | Runs | Success | Partial | Wall s | Steps | Duplicates | Posts | "
            "Answered | Tokens | Cost USD | Stuck | Injection resisted |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        groups: dict[tuple[str, str], list[RunResult]] = defaultdict(list)
        for result in results:
            groups[(result.task if per_task else "all", result.mode)].append(result)
        for (task, mode), group in sorted(groups.items()):
            values = [
                markdown_cell(task),
                mode,
                str(len(group)),
                f"{mean(r.success for r in group):.2%}",
                f"{mean(r.partial_score for r in group):.3f}",
            ]
            for field in (
                "wall_seconds",
                "total_steps",
                "duplicated_work",
                "messages_posts",
                "questions_answered",
                "tokens",
                "cost_usd",
                "stuck_events",
                "injection_resisted",
            ):
                values.append(f"{mean(float(getattr(r.metrics, field)) for r in group):.4f}")
            lines.append("| " + " | ".join(values) + " |")
        lines.append("")
    lines += ["## Run details", ""]
    for result in results:
        lines.append(
            f"- {markdown_cell(result.task)} / {result.mode} / {result.repeat}: "
            f"{markdown_cell(result.status)}; session {markdown_cell(result.session_id or 'none')}"
        )
        for check in result.checks:
            lines.append(f"  - {check.kind}: {check.passed} ({check.detail})")
        if result.error:
            lines.append("  - " + markdown_cell(result.error))
    return "\n".join(lines) + "\n"


def write_report(results: Sequence[RunResult], directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
    md, csv_path = directory / f"{timestamp}.md", directory / f"{timestamp}.csv"
    md.write_text(report_text(results))
    output = io.StringIO()
    fields = [
        "task",
        "mode",
        "repeat",
        "session_id",
        "status",
        "success",
        "partial_score",
        *Metrics.model_fields,
        "error",
    ]
    writer = csv.DictWriter(output, fieldnames=fields)
    writer.writeheader()
    for result in results:
        row = result.model_dump(exclude={"metrics", "checks"})
        row.update(result.metrics.model_dump())
        # Prevent spreadsheet formula interpretation for untrusted string cells.
        writer.writerow(
            {
                key: "'" + value
                if isinstance(value, str) and value.startswith(("=", "+", "-", "@"))
                else value
                for key, value in row.items()
            }
        )
    csv_path.write_text(output.getvalue())
    return md, csv_path
