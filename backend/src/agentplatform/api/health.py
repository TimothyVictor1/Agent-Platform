"""``GET /config/providers/health`` and ``GET /config/profiles`` helpers (SPEC §16).

* ``ProvidersHealthChecker`` calls ``health()`` on one client per distinct model
  endpoint (provider + base URL + model) across all profiles, and on every configured
  sandbox provider, concurrently, each with a 5 s timeout. Results are cached for
  30 s and concurrent callers share one in-flight check.
* Error strings are scrubbed of every configured secret value before they leave the
  process, and model clients that cannot be built (missing key env var) are reported
  as failing with the ``ConfigError`` text, which names the variable, never a value.
* ``profile_summaries`` exposes names and role summaries only: no API keys or key
  env var names, and base URLs without userinfo, query or fragment.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import structlog

from agentplatform.config import ModelsConfig, RoleConfig, RoleName
from agentplatform.contracts.api import (
    ProfileList,
    ProfileSummary,
    ProviderHealth,
    ProvidersHealth,
    RoleSummary,
)
from agentplatform.contracts.common import HealthStatus, utcnow
from agentplatform.errors import AppError
from agentplatform.models.base import ModelClient
from agentplatform.sandboxes.base import SandboxProvider

__all__ = [
    "HEALTH_CACHE_S",
    "HEALTH_TIMEOUT_S",
    "ModelClientFactory",
    "ProvidersHealthChecker",
    "profile_summaries",
    "public_base_url",
    "scrub",
]

log = structlog.get_logger(__name__)

HEALTH_TIMEOUT_S = 5.0
HEALTH_CACHE_S = 30.0

ModelClientFactory = Callable[[RoleConfig], ModelClient]
ROLES: tuple[RoleName, ...] = ("planner", "supervisor", "agent", "summarizer")


def public_base_url(url: str | None) -> str | None:
    """``url`` without userinfo, query string or fragment (they may carry keys)."""
    if not url:
        return url
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        netloc = host if parts.port is None else f"{host}:{parts.port}"
    except ValueError:
        return None
    return urlunsplit((parts.scheme, netloc, parts.path, "", ""))


def scrub(text: str, secrets: Sequence[str], *, limit: int = 300) -> str:
    """Replace every secret value in ``text`` and cap its length."""
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text[:limit]


def _role_summary(role: RoleConfig) -> RoleSummary:
    return RoleSummary(
        provider=role.provider,
        model=role.model,
        base_url=public_base_url(role.base_url),
        vision=role.vision,
        coordinate_mode=role.coordinate_mode if role.vision else None,
    )


def profile_summaries(config: ModelsConfig) -> ProfileList:
    return ProfileList(
        items=[
            ProfileSummary(
                name=name,
                roles={r: _role_summary(profile.role(r)) for r in ROLES},
            )
            for name, profile in sorted(config.profiles.items())
        ],
        default_profile=config.default_profile,
    )


def _endpoint_name(role: RoleConfig) -> str:
    base = public_base_url(role.base_url)
    return f"{role.provider}:{role.model}" + (f"@{base}" if base else "")


@dataclass(slots=True)
class _Cache:
    value: ProvidersHealth | None = None
    at: float = 0.0
    inflight: asyncio.Task[ProvidersHealth] | None = None
    clients: dict[str, ModelClient] = field(default_factory=dict)


class ProvidersHealthChecker:
    def __init__(
        self,
        models: ModelsConfig,
        sandbox_providers: Mapping[str, SandboxProvider],
        client_factory: ModelClientFactory,
        *,
        secrets: Callable[[], Sequence[str]] = list,
        timeout_s: float = HEALTH_TIMEOUT_S,
        cache_s: float = HEALTH_CACHE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._models = models
        self._sandboxes = dict(sandbox_providers)
        self._factory = client_factory
        self._secrets = secrets
        self._timeout_s = timeout_s
        self._cache_s = cache_s
        self._clock = clock
        self._state = _Cache()
        self._lock = asyncio.Lock()

    def _model_roles(self) -> dict[str, RoleConfig]:
        roles: dict[str, RoleConfig] = {}
        for _, profile in sorted(self._models.profiles.items()):
            for r in ROLES:
                role = profile.role(r)
                roles.setdefault(_endpoint_name(role), role)
        return roles

    async def check(self) -> ProvidersHealth:
        async with self._lock:
            st = self._state
            if st.value is not None and self._clock() - st.at < self._cache_s:
                return st.value
            if st.inflight is None or st.inflight.done():
                st.inflight = asyncio.create_task(self._run())
            task = st.inflight
        # shield: a cancelled request must not cancel the check other callers await
        return await asyncio.shield(task)

    async def _run(self) -> ProvidersHealth:
        result = await self._check_all()
        self._state.value, self._state.at = result, self._clock()
        return result

    async def _check_all(self) -> ProvidersHealth:
        checks: list[Awaitable[ProviderHealth]] = []
        for name, role in self._model_roles().items():
            checks.append(self._check_model(name, role))
        for name, provider in sorted(self._sandboxes.items()):
            checks.append(self._probe(name, "sandbox", provider.health))
        return ProvidersHealth(items=list(await asyncio.gather(*checks)))

    async def _check_model(self, name: str, role: RoleConfig) -> ProviderHealth:
        client = self._state.clients.get(name)
        if client is None:
            try:
                client = self._factory(role)
            except AppError as exc:
                return self._result(name, "model", ok=False, error=exc.detail)
            except Exception as exc:
                log.warning("health_client_failed", provider=name, error_type=type(exc).__name__)
                return self._result(name, "model", ok=False, error="client could not be created")
            self._state.clients[name] = client
        return await self._probe(name, "model", client.health)

    async def _probe(
        self,
        name: str,
        kind: Literal["model", "sandbox"],
        health: Callable[[], Awaitable[HealthStatus]],
    ) -> ProviderHealth:
        start = self._clock()
        try:
            status = await asyncio.wait_for(health(), timeout=self._timeout_s)
        except TimeoutError:
            return self._result(
                name, kind, ok=False, error=f"health check timed out after {self._timeout_s:g} s"
            )
        except AppError as exc:
            return self._result(name, kind, ok=False, error=exc.detail)
        except Exception as exc:
            log.warning("health_check_failed", provider=name, error_type=type(exc).__name__)
            return self._result(
                name, kind, ok=False, error=f"health check failed ({type(exc).__name__})"
            )
        latency = status.latency_ms
        if latency is None:
            latency = int((self._clock() - start) * 1000)
        return self._result(name, kind, ok=status.ok, error=status.error, latency_ms=latency)

    def _result(
        self,
        name: str,
        kind: Literal["model", "sandbox"],
        *,
        ok: bool,
        error: str | None = None,
        latency_ms: int | None = None,
    ) -> ProviderHealth:
        return ProviderHealth(
            name=name,
            kind=kind,
            ok=ok,
            latency_ms=latency_ms,
            error=None if error is None else scrub(error, self._secrets()),
            checked_at=utcnow(),
        )

    async def aclose(self) -> None:
        clients, self._state.clients = self._state.clients, {}
        for client in clients.values():
            try:
                await client.aclose()
            except Exception:
                log.warning("health_client_close_failed")
