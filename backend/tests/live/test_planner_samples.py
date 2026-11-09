"""Live: run 5 sample prompts through the configured planner model (SPEC §11).

Skipped unless ``APP_LIVE=1`` (tests/conftest.py marks everything under tests/live/
as ``live``). Uses the planner role of ``default_profile`` in ``APP_MODELS_FILE``
(``config/models.yaml``). Each result (proposal or error) is written to
``data/planner-samples/<run>/NN-<slug>.json`` for manual review; the test fails if
any prompt did not yield a valid plan.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any

import pytest

from agentplatform.config import load_models_config, load_settings
from agentplatform.contracts.plan import validate_plan
from agentplatform.errors import AppError
from agentplatform.models.factory import build_client
from agentplatform.runtime.planner import Planner

pytestmark = pytest.mark.live

SAMPLES: list[tuple[str, int]] = [
    ("Compare the paid plans of Notion, Coda and Airtable and put them in a spreadsheet.", 3),
    (
        "Check that the latest release of the Python library httpx installs on Python 3.12, "
        "then summarise what changed in that release.",
        4,
    ),
    ("Find the opening hours of the Louvre for next Monday.", 3),
    (
        "Clone https://github.com/pallets/click, run its test suite, and write a short report "
        "of failing tests with links to the relevant source files.",
        2,
    ),
    (
        "Research three open-source vector databases, compare licence, language and latest "
        "version, then draft a one-page recommendation document.",
        5,
    ),
]


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


async def test_planner_samples() -> None:
    settings = load_settings()
    models_file = settings.resolve_path(settings.app_models_file)
    if not models_file.exists():
        pytest.skip(f"{models_file} not found (copy config/models.example.yaml)")
    config = load_models_config(models_file)
    profile_name = config.default_profile or next(iter(config.profiles))
    client = build_client(config.profile(profile_name).planner, config.prices)
    planner = Planner(client)

    run = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = settings.repo_root / "data" / "planner-samples" / run
    out_dir.mkdir(parents=True, exist_ok=True)

    failures: list[str] = []
    try:
        for n, (prompt, agents) in enumerate(SAMPLES, start=1):
            record: dict[str, Any] = {
                "prompt": prompt,
                "agent_count_hint": agents,
                "profile": profile_name,
                "model": client.name,
            }
            try:
                proposal = await planner.propose(prompt, agent_count_hint=agents)
            except AppError as exc:
                record["error"] = {"code": exc.code, "detail": exc.detail, "errors": exc.errors}
                failures.append(f"{n}: {exc.code}: {exc.detail}")
            else:
                record["proposal"] = proposal.model_dump(mode="json")
                record["task_count"] = len(proposal.tasks)
                if validate_plan(proposal.tasks):
                    failures.append(f"{n}: returned plan does not validate")
            path = out_dir / f"{n:02d}-{_slug(prompt)}.json"
            path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    finally:
        await client.aclose()

    assert not failures, "planner failed on samples:\n" + "\n".join(failures)
