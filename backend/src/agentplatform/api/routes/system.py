"""Health, metrics and config endpoints (SPEC §16 "Config", §19)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from agentplatform.api.deps import Deps
from agentplatform.api.health import profile_summaries
from agentplatform.contracts.api import (
    HealthResponse,
    MetricsResponse,
    ProfileList,
    ProvidersHealth,
)

__all__ = ["READINESS_TIMEOUT_S", "api_version", "router"]

router = APIRouter()

READINESS_TIMEOUT_S = 2.0


def api_version() -> str:
    try:
        return version("agentplatform")
    except PackageNotFoundError:  # pragma: no cover - always installed via uv
        return "0.0.0"


async def _check(probe: Callable[[], Awaitable[bool]]) -> bool:
    try:
        return bool(await asyncio.wait_for(probe(), timeout=READINESS_TIMEOUT_S))
    except Exception:
        return False


@router.get("/health", response_model=HealthResponse, tags=["system"])
async def health(deps: Deps) -> HealthResponse:
    """Liveness plus dependency checks; reachable without the API token."""
    names = list(deps.readiness)
    results = await asyncio.gather(*(_check(deps.readiness[n]) for n in names))
    checks = dict(zip(names, results, strict=True))
    return HealthResponse(
        status="ok" if all(results) else "degraded", version=api_version(), checks=checks
    )


@router.get("/metrics", response_model=MetricsResponse, tags=["system"])
async def metrics(deps: Deps) -> MetricsResponse:
    return deps.metrics.snapshot()


@router.get("/config/profiles", response_model=ProfileList, tags=["config"])
async def profiles(deps: Deps) -> ProfileList:
    """Profile names and role summaries. Never API keys or key variable names."""
    return profile_summaries(deps.models)


@router.get("/config/providers/health", response_model=ProvidersHealth, tags=["config"])
async def providers_health(deps: Deps) -> ProvidersHealth:
    """``health()`` of every model endpoint and sandbox provider (5 s timeout each,
    cached 30 s)."""
    return await deps.providers_health.check()
