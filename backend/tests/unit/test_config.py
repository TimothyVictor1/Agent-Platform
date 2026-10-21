"""Settings and YAML config loading/validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentplatform.config import (
    DEFAULT_DATABASE_URL,
    Settings,
    find_repo_root,
    load_models_config,
    load_policies_config,
)
from agentplatform.errors import ConfigError

REPO = find_repo_root(Path(__file__).parent)


def test_example_models_config_is_valid() -> None:
    cfg = load_models_config(REPO / "config" / "models.example.yaml")
    assert set(cfg.profiles) >= {"local-free", "cloud-best", "openrouter-free"}
    local = cfg.profile("local-free")
    assert local.agent.vision
    assert local.agent.coordinate_mode == "normalized_1000"
    assert local.agent.effective_prompt_budget == 24_000
    cloud = cfg.profile("cloud-best")
    assert cloud.supervisor.api_key_env == "ANTHROPIC_API_KEY"
    assert cloud.agent.effective_prompt_budget == 60_000
    assert cfg.price("unknown-model").input == 0.0
    with pytest.raises(ConfigError, match="unknown model profile"):
        cfg.profile("nope")


def test_example_policies_config_is_valid() -> None:
    cfg = load_policies_config(REPO / "config" / "policies.example.yaml")
    assert cfg.budgets.default_max_cost_usd == 5.0
    assert cfg.require_human_for_plan_changes is False
    assert any("payment" in item for item in cfg.approval_required_for)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "models.yaml"
    path.write_text(text)
    return path


def _profile(**roles: str) -> str:
    lines = ["profiles:", "  p:"] + [f"    {name}: {value}" for name, value in roles.items()]
    return "\n".join(lines) + "\n"


ROLE = "{provider: openai_compatible, base_url: http://x/v1, model: m}"
VISION = "{provider: openai_compatible, base_url: http://x/v1, model: m, vision: true}"


def test_agent_role_must_be_vision(tmp_path: Path) -> None:
    text = _profile(planner=ROLE, supervisor=ROLE, agent=ROLE, summarizer=ROLE)
    with pytest.raises(ConfigError, match="vision"):
        load_models_config(_write(tmp_path, text))


def test_missing_role_and_unknown_keys_are_reported(tmp_path: Path) -> None:
    text = _profile(planner=ROLE, agent=VISION, summarizer=ROLE, extra="1")
    with pytest.raises(ConfigError) as exc:
        load_models_config(_write(tmp_path, text))
    assert "profiles.p.supervisor" in str(exc.value)
    assert "profiles.p.extra" in str(exc.value)


def test_openai_compatible_needs_base_url(tmp_path: Path) -> None:
    bad = "{provider: openai_compatible, model: m}"
    text = _profile(planner=bad, supervisor=ROLE, agent=VISION, summarizer=ROLE)
    with pytest.raises(ConfigError, match="base_url"):
        load_models_config(_write(tmp_path, text))


def test_missing_and_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_models_config(tmp_path / "missing.yaml")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_models_config(_write(tmp_path, "profiles: [unclosed"))


def test_settings_database_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DB_NAME", raising=False)
    s = Settings(_env_file=None)
    assert s.effective_database_url == DEFAULT_DATABASE_URL
    s = Settings(
        _env_file=None, database_url="postgresql+asyncpg://u:p@db:5432/x", app_db_name="wt1"
    )
    assert s.effective_database_url == "postgresql+asyncpg://u:p@db:5432/wt1"


def test_settings_secrets_are_hidden(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-123")
    s = Settings(_env_file=None)
    assert "sk-test-123" not in repr(s)
    assert "sk-test-123" in s.secret_values()
