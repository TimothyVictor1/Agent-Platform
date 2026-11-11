"""Configuration: environment variables (SPEC §22) and YAML files (SPEC §8.2, §15.1).

* ``Settings`` reads the environment (and ``.env`` in the repo root or the current
  directory). Secret values are ``SecretStr`` so they never show up in reprs/logs.
* ``ModelsConfig`` is ``config/models.yaml``: named profiles mapping the four roles
  (planner, supervisor, agent, summarizer) to providers/models, plus a price table.
* ``PoliciesConfig`` is ``config/policies.yaml``.

Use ``load_settings()``, ``load_models_config()`` and ``load_policies_config()``;
create them once in ``main.py`` and inject them (no global singletons).
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal, Self
from urllib.parse import quote, unquote, urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from agentplatform.contracts.common import CoordinateMode
from agentplatform.errors import ConfigError

__all__ = [
    "DEFAULT_DATABASE_URL",
    "DEFAULT_REDIS_URL",
    "ModelPrice",
    "ModelsConfig",
    "PoliciesConfig",
    "Profile",
    "RoleConfig",
    "RoleName",
    "Settings",
    "find_repo_root",
    "load_models_config",
    "load_policies_config",
    "load_settings",
]

DEFAULT_DATABASE_URL = (
    "postgresql+asyncpg://agentplatform:agentplatform@localhost:5432/agentplatform"
)
DEFAULT_REDIS_URL = "redis://localhost:6379/0"
"""Local development defaults (the compose services). Real deployments set
``DATABASE_URL``/``REDIS_URL`` or at least ``APP_PG_PASSWORD``/``APP_REDIS_PASSWORD``
to strong values."""

RoleName = Literal["planner", "supervisor", "agent", "summarizer"]
ROLE_NAMES: tuple[RoleName, ...] = ("planner", "supervisor", "agent", "summarizer")


def find_repo_root(start: Path | None = None) -> Path:
    """The nearest ancestor containing ``SPEC.md`` (falls back to the cwd)."""
    here = (start or Path.cwd()).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "SPEC.md").exists():
            return candidate
    return here


class Settings(BaseSettings):
    """Process settings from the environment. Field names map to upper-case env vars."""

    model_config = SettingsConfigDict(
        env_file=(str(find_repo_root() / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        str_strip_whitespace=True,
    )

    database_url: str = ""
    redis_url: str = ""
    app_api_token: SecretStr | None = None
    app_models_file: str = "config/models.yaml"
    app_policies_file: str = "config/policies.yaml"

    artifact_store: Literal["fs", "s3"] = "fs"
    artifact_dir: str = "./data/artifacts"
    s3_endpoint: str = ""
    s3_bucket: str = ""
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None

    e2b_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    local_llm_key: SecretStr | None = None

    frontend_origin: str = "http://localhost:3000"
    app_api_host: str = "127.0.0.1"
    """Interface the API binds to. Keep ``127.0.0.1`` unless a reverse proxy in front
    of it handles TLS and ``APP_API_TOKEN`` is set."""
    app_api_port: int = 8000
    app_db_name: str = ""
    app_pg_password: SecretStr | None = None
    """Password of the compose Postgres; used in the default ``DATABASE_URL``."""
    app_redis_password: SecretStr | None = None
    """Password of the compose Redis (``requirepass``); used in the default ``REDIS_URL``."""
    app_worktree: str = ""
    app_live: bool = False
    app_docker: bool = False

    # -- runtime (T13) --
    app_runtime_id: str = ""
    """Owner name of this runtime process in session leases (default: host-pid-random)."""
    app_docker_mem_limit: str = Field(default="2g", pattern=r"^[1-9][0-9]*[bkmg]$")
    # docker_local egress: "open" (today's default) or "proxy" (no default route; only
    # the compose egress-proxy is reachable, docs/security.md)
    app_sandbox_egress: Literal["open", "proxy"] = "open"
    app_egress_proxy_container: str = Field(
        default="agentplatform-egress-proxy-1", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$"
    )
    """Memory (and memory+swap, so no swap) limit of each ``docker_local`` desktop."""
    app_e2b_max_lifetime_min: int = Field(default=60, ge=1, le=24 * 60)
    """Upper bound of an E2B sandbox lifetime; longer requests are capped (warning)."""
    app_adopt_completed_hours: float = Field(default=24.0, ge=0)
    """A restarted runtime adopts completed sessions this recent (for follow-ups)."""
    app_planning_sweep_s: float = Field(default=60.0, ge=5)
    """A session still in ``planning`` with no plan after this long is planned again."""
    app_orphan_sweep_min: float = Field(default=10.0, ge=1)
    """Period of the orphaned-sandbox cleanup (also run at startup)."""

    @property
    def effective_database_url(self) -> str:
        """``DATABASE_URL`` (or the compose default) with the database name replaced by
        ``APP_DB_NAME`` when set, so each worktree gets its own database."""
        url = self.database_url or _with_password(DEFAULT_DATABASE_URL, self.app_pg_password)
        if not self.app_db_name:
            return url
        parts = urlsplit(url)
        return urlunsplit(parts._replace(path="/" + self.app_db_name))

    @property
    def effective_redis_url(self) -> str:
        """``REDIS_URL``, else the compose default (with ``APP_REDIS_PASSWORD``)."""
        return self.redis_url or _with_password(DEFAULT_REDIS_URL, self.app_redis_password)

    @property
    def repo_root(self) -> Path:
        return find_repo_root()

    def resolve_path(self, value: str) -> Path:
        """Resolve a configured path relative to the repo root."""
        path = Path(value)
        return path if path.is_absolute() else self.repo_root / path

    def secret_values(self) -> list[str]:
        """All configured secret values (non-empty), for leak checks. Never log these."""
        secrets = [
            self.app_api_token,
            self.s3_access_key_id,
            self.s3_secret_access_key,
            self.e2b_api_key,
            self.anthropic_api_key,
            self.openrouter_api_key,
            self.gemini_api_key,
            self.local_llm_key,
            self.app_pg_password,
            self.app_redis_password,
        ]
        values = [s.get_secret_value() for s in secrets if s and s.get_secret_value()]
        for url in (self.database_url, self.redis_url):
            password = urlsplit(url).password if url else None
            if password:
                values.append(unquote(password))
        return list(dict.fromkeys(values))


def model_key_values(models: ModelsConfig, env: Mapping[str, str] | None = None) -> list[str]:
    """Values of every variable a model profile names in ``api_key_env`` (e.g. a
    provider key that ``Settings`` has no field for), for the same leak checks as
    ``Settings.secret_values``. Never log these."""
    source = os.environ if env is None else env
    names = {
        role.api_key_env
        for profile in models.profiles.values()
        for role in (profile.planner, profile.supervisor, profile.agent, profile.summarizer)
        if role.api_key_env
    }
    return [v for v in (source.get(n, "").strip() for n in sorted(names)) if v]


def _with_password(url: str, password: SecretStr | None) -> str:
    """``url`` with its password replaced (user kept; ``""`` for Redis' default user)."""
    if password is None or not password.get_secret_value():
        return url
    parts = urlsplit(url)
    user = parts.username or ""
    host = parts.hostname or "localhost"
    netloc = f"{quote(user, safe='')}:{quote(password.get_secret_value(), safe='')}@{host}"
    if parts.port is not None:
        netloc += f":{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def load_settings() -> Settings:
    return Settings()


# --- models.yaml -------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RoleConfig(_Strict):
    """One role of a profile (SPEC §8.2)."""

    provider: Literal["openai_compatible", "anthropic"]
    model: str = Field(min_length=1)
    base_url: str | None = None
    api_key_env: str | None = None
    """Name of the env var holding the API key (never the key itself). Defaults to
    ``ANTHROPIC_API_KEY`` for the anthropic provider; none for openai_compatible."""
    vision: bool = False
    max_images_in_context: int = Field(default=3, ge=1, le=10)
    coordinate_mode: CoordinateMode = "absolute"
    screenshot_target_width: int = Field(default=1280, ge=320, le=3840)
    concurrency: int = Field(default=4, ge=1, le=64)
    max_output_tokens: int = Field(default=1024, ge=16)
    temperature: float = Field(default=0.0, ge=0, le=2)
    timeout_s: float = Field(default=120.0, gt=0)
    prompt_budget_tokens: int | None = Field(default=None, ge=1000)
    """Total prompt budget per step (SPEC §10.4). Default: 24k local, 60k cloud."""
    image_tokens: int = Field(default=1500, ge=1)
    """Approximate tokens one screenshot costs, for context budgeting."""

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.provider == "openai_compatible" and not self.base_url:
            raise ValueError("openai_compatible roles need base_url")
        if self.provider == "anthropic" and self.api_key_env is None:
            self.api_key_env = "ANTHROPIC_API_KEY"
        if self.coordinate_mode != "absolute" and not self.vision:
            raise ValueError("coordinate_mode only applies to vision roles")
        return self

    @property
    def is_local(self) -> bool:
        """Heuristic: a non-HTTPS base URL means a local server (cost 0, smaller budget)."""
        return bool(self.base_url) and not str(self.base_url).startswith("https://")

    @property
    def effective_prompt_budget(self) -> int:
        if self.prompt_budget_tokens is not None:
            return self.prompt_budget_tokens
        return 24_000 if self.is_local else 60_000


class Profile(_Strict):
    planner: RoleConfig
    supervisor: RoleConfig
    agent: RoleConfig
    summarizer: RoleConfig

    @model_validator(mode="after")
    def _agent_needs_vision(self) -> Self:
        if not self.agent.vision:
            raise ValueError("the agent role must use a vision model (vision: true)")
        return self

    def role(self, name: RoleName) -> RoleConfig:
        role: RoleConfig = getattr(self, name)
        return role


class ModelPrice(_Strict):
    """USD per 1M tokens (and per image, if the provider bills images separately)."""

    input: float = Field(default=0.0, ge=0)
    output: float = Field(default=0.0, ge=0)
    image: float = Field(default=0.0, ge=0)


class ModelsConfig(_Strict):
    default_profile: str | None = None
    profiles: dict[str, Profile] = Field(min_length=1)
    prices: dict[str, ModelPrice] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _default_exists(self) -> Self:
        if self.default_profile is not None and self.default_profile not in self.profiles:
            raise ValueError(f"default_profile {self.default_profile!r} is not a profile")
        return self

    def profile(self, name: str) -> Profile:
        try:
            return self.profiles[name]
        except KeyError:
            raise ConfigError(
                f"unknown model profile {name!r}; known: {', '.join(sorted(self.profiles))}"
            ) from None

    def price(self, model: str) -> ModelPrice:
        return self.prices.get(model, ModelPrice())


# --- policies.yaml -----------------------------------------------------------------------


class NetworkPolicy(_Strict):
    mode: Literal["open", "allowlist"] = "open"
    allowlist: list[str] = Field(default_factory=list)


class FramesPolicy(_Strict):
    keep_full_resolution: bool = False


class BudgetsPolicy(_Strict):
    default_max_cost_usd: float | None = Field(default=5.0, ge=0)


class PoliciesConfig(_Strict):
    approval_required_for: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    frames: FramesPolicy = Field(default_factory=FramesPolicy)
    budgets: BudgetsPolicy = Field(default_factory=BudgetsPolicy)
    require_human_for_plan_changes: bool = False
    auto_approve_followups: bool = False


# --- loaders -----------------------------------------------------------------------------


def _load_yaml(path: Path) -> object:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from None


def _format_errors(path: Path, exc: ValidationError) -> str:
    lines = [f"invalid config in {path}:"]
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        lines.append(f"  {loc}: {err['msg']}")
    return "\n".join(lines)


def load_models_config(path: Path | str) -> ModelsConfig:
    """Load and validate ``models.yaml``; raises ``ConfigError`` with readable errors."""
    p = Path(path)
    try:
        return ModelsConfig.model_validate(_load_yaml(p))
    except ValidationError as exc:
        raise ConfigError(_format_errors(p, exc)) from None


def load_policies_config(path: Path | str) -> PoliciesConfig:
    """Load and validate ``policies.yaml``; raises ``ConfigError`` with readable errors."""
    p = Path(path)
    try:
        return PoliciesConfig.model_validate(_load_yaml(p) or {})
    except ValidationError as exc:
        raise ConfigError(_format_errors(p, exc)) from None
