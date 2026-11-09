"""Build a fully wrapped ``ModelClient`` from one profile role (SPEC §8.2).

Usage in the runtime wiring (T13)::

    shared = SharedModelState()                       # one per process
    cfg = load_models_config(...)
    agent_model = build_client(cfg.profile(name).agent, cfg.prices, shared=shared)

Stack, outermost first: ``DebugLoggingClient`` (only with ``debug_dir``) ->
``AccountingClient`` -> ``RetryingClient`` -> ``ConcurrencyLimitedClient`` ->
provider client. See wrappers.py for why the semaphore sits inside the retries.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

import httpx2

from agentplatform.config import ModelPrice, RoleConfig
from agentplatform.errors import ConfigError
from agentplatform.models.anthropic_client import AnthropicClient
from agentplatform.models.base import ModelClient
from agentplatform.models.openai_compatible import OpenAICompatibleClient
from agentplatform.models.shared import SharedModelState
from agentplatform.models.wrappers import (
    AccountingClient,
    ConcurrencyLimitedClient,
    DebugLoggingClient,
    RetryingClient,
)

__all__ = ["build_client", "concurrency_key", "resolve_api_key"]


def resolve_api_key(role: RoleConfig, env: Mapping[str, str]) -> str | None:
    """The API key named by ``role.api_key_env``; ``None`` if the role names none.

    Raises ``ConfigError`` (naming the variable, never a value) if it is unset/empty.
    """
    if role.api_key_env is None:
        return None
    key = env.get(role.api_key_env, "").strip()
    if not key:
        raise ConfigError(
            f"environment variable {role.api_key_env} is not set "
            f"(needed by the {role.provider} model {role.model!r})"
        )
    return key


def concurrency_key(role: RoleConfig) -> str:
    """One semaphore per server: the base URL for OpenAI-compatible servers (one
    local GPU box), per model for Anthropic (rate limits are per model)."""
    if role.provider == "openai_compatible":
        return f"openai_compatible:{(role.base_url or '').rstrip('/')}"
    return f"anthropic:{role.base_url or 'default'}:{role.model}"


def build_client(
    role: RoleConfig,
    prices: Mapping[str, ModelPrice],
    *,
    shared: SharedModelState | None = None,
    env: Mapping[str, str] | None = None,
    debug_dir: Path | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> ModelClient:
    """A provider client for ``role`` with retries, a shared concurrency limit, cost
    accounting and (if ``debug_dir`` is given) JSONL debug logging.

    ``shared`` should be the process-wide ``SharedModelState``; without it the client
    gets private state (its own semaphore). ``env`` defaults to ``os.environ``.
    ``http_client`` is for tests (mock transports).
    """
    shared = shared or SharedModelState()
    key = resolve_api_key(role, os.environ if env is None else env)

    provider: ModelClient
    if role.provider == "openai_compatible":
        if not role.base_url:  # RoleConfig validates this too
            raise ConfigError(f"openai_compatible model {role.model!r} needs base_url")
        provider = OpenAICompatibleClient(
            base_url=role.base_url,
            model=role.model,
            api_key=key,
            shared=shared,
            http_client=http_client,
        )
    else:
        if key is None:
            raise ConfigError(f"anthropic model {role.model!r} needs api_key_env")
        provider = AnthropicClient(
            model=role.model,
            api_key=key,
            base_url=role.base_url,
            shared=shared,
            http_client=http_client,
        )

    client: ModelClient = ConcurrencyLimitedClient(
        provider, shared.semaphore(concurrency_key(role), role.concurrency)
    )
    client = RetryingClient(client)
    client = AccountingClient(client, prices.get(role.model, ModelPrice()))
    if debug_dir is not None:
        client = DebugLoggingClient(client, debug_dir)
    return client
