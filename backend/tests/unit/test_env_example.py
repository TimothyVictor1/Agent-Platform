"""`.env.example` rules (SPEC §22): no inline comments, ports documented, in sync with SPEC."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
ENV_EXAMPLE = ROOT / ".env.example"
SPEC = ROOT / "SPEC.md"
ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def lines() -> list[str]:
    return ENV_EXAMPLE.read_text().splitlines()


def values() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in lines():
        match = ASSIGNMENT.match(line)
        if match:
            out[match.group(1)] = match.group(2)
    return out


def test_every_line_is_blank_comment_or_assignment() -> None:
    for line in lines():
        assert not line.strip() or line.startswith("#") or ASSIGNMENT.match(line), line


def test_no_inline_comments_or_trailing_whitespace() -> None:
    for name, value in values().items():
        assert "#" not in value, f"{name}: put the comment on its own line above it"
        assert value == value.strip(), f"{name}: value has surrounding whitespace"


def test_no_duplicate_variables() -> None:
    names = [m.group(1) for line in lines() if (m := ASSIGNMENT.match(line))]
    assert len(names) == len(set(names))


def test_compose_ports_have_defaults_and_comments_above() -> None:
    env = values()
    assert env["APP_PG_PORT"] == "5432"
    assert env["APP_REDIS_PORT"] == "6379"
    text = lines()
    pg, redis = text.index("APP_PG_PORT=5432"), text.index("APP_REDIS_PORT=6379")
    # APP_REDIS_PORT is the next variable after APP_PG_PORT (only its comment between).
    assert pg < redis
    assert all(t.startswith("#") for t in text[pg + 1 : redis])
    assert text[redis - 1].startswith("#")
    comment_above_pg = " ".join(t for t in text[pg - 2 : pg] if t.startswith("#"))
    assert "must not clash with a local Postgres" in comment_above_pg


def test_spec_section_22_matches_env_example() -> None:
    spec = SPEC.read_text()
    section = spec[spec.index("## 22. Configuration and environment") :]
    block = section[section.index("```\n") + 4 :]
    block = block[: block.index("```")]
    assert block == ENV_EXAMPLE.read_text()
