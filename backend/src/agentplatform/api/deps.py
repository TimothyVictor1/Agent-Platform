"""The API's dependency container (``ApiDeps``) and FastAPI accessors.

``create_app(deps)`` stores one ``ApiDeps`` on ``app.state.deps``; routes reach it via
``Depends(get_deps)``. Production code builds it with ``build_deps(settings)`` (in the
app lifespan); tests pass a container made from in-memory fakes. No module globals.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Annotated

import structlog
from fastapi import Depends, Request
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from agentplatform.api.commands import CommandSender
from agentplatform.api.health import ModelClientFactory, ProvidersHealthChecker
from agentplatform.api.metrics import Metrics
from agentplatform.api.tickets import RedisTicketStore, TicketStore
from agentplatform.artifacts.base import ArtifactStore
from agentplatform.bus.base import EventBus
from agentplatform.config import (
    ModelsConfig,
    PoliciesConfig,
    RoleConfig,
    Settings,
    load_models_config,
    load_policies_config,
)
from agentplatform.contracts.common import SandboxProviderName
from agentplatform.db.event_store import EventStore
from agentplatform.db.models import SessionRow
from agentplatform.db.session import SessionMaker
from agentplatform.eventing import EventRecorder
from agentplatform.models.base import ModelClient
from agentplatform.sandboxes.base import SandboxProvider

__all__ = [
    "ApiDeps",
    "Deps",
    "Tx",
    "build_deps",
    "get_deps",
    "get_tx",
]

log = structlog.get_logger(__name__)

Check = Callable[[], Awaitable[bool]]
Closer = Callable[[], Awaitable[None]]

DEFAULT_MAX_UPLOAD_BYTES = 50 * 1024 * 1024
"""SPEC §16: user uploads up to 50 MB."""


class ApiDeps:
    """Everything the routes need, created once per process and injected."""

    def __init__(
        self,
        *,
        settings: Settings,
        models: ModelsConfig,
        policies: PoliciesConfig,
        sessionmaker: SessionMaker,
        events: EventStore,
        bus: EventBus,
        artifacts: ArtifactStore,
        tickets: TicketStore,
        sandbox_providers: Mapping[str, SandboxProvider],
        model_client_factory: ModelClientFactory,
        recorder: EventRecorder | None = None,
        metrics: Metrics | None = None,
        session_exists: Callable[[str], Awaitable[bool]] | None = None,
        readiness: Mapping[str, Check] | None = None,
        ws_check_interval_s: float = 20.0,
        ws_replay_batch: int = 500,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
        closers: list[Closer] | None = None,
    ) -> None:
        self.settings = settings
        self.models = models
        self.policies = policies
        self.sessionmaker = sessionmaker
        self.events = events
        self.bus = bus
        self.artifacts = artifacts
        self.tickets = tickets
        self.sandbox_providers = dict(sandbox_providers)
        self.recorder = recorder or EventRecorder(events, bus)
        self.metrics = metrics or Metrics()
        self.commands = CommandSender(bus, self.metrics)
        self.providers_health = ProvidersHealthChecker(
            models,
            self.sandbox_providers,
            model_client_factory,
            secrets=settings.secret_values,
        )
        self.session_exists = session_exists or self._db_session_exists
        self.readiness: dict[str, Check] = (
            dict(readiness)
            if readiness is not None
            else {"database": self._db_ping, "redis": tickets.ping}
        )
        self.ws_check_interval_s = ws_check_interval_s
        self.ws_replay_batch = ws_replay_batch
        self.max_upload_bytes = max_upload_bytes
        self._closers = list(closers or [])

    @property
    def api_token(self) -> str | None:
        token = self.settings.app_api_token
        value = token.get_secret_value() if token is not None else ""
        return value or None

    @property
    def allowed_origins(self) -> frozenset[str]:
        """Browser origins allowed to call the API (``FRONTEND_ORIGIN``)."""
        origin = self.settings.frontend_origin.strip().rstrip("/")
        return frozenset({origin}) if origin else frozenset()

    @property
    def sandbox_provider_names(self) -> frozenset[SandboxProviderName]:
        names: set[SandboxProviderName] = set()
        for provider in self.sandbox_providers.values():
            names.add(provider.name)
        return frozenset(names)

    async def _db_session_exists(self, session_id: str) -> bool:
        async with self.sessionmaker() as tx:
            found = await tx.scalar(select(SessionRow.id).where(SessionRow.id == session_id))
        return found is not None

    async def _db_ping(self) -> bool:
        try:
            async with self.sessionmaker() as tx:
                await tx.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    async def aclose(self) -> None:
        await self.providers_health.aclose()
        for close in reversed(self._closers):
            try:
                await close()
            except Exception:
                log.warning("api_close_failed")


async def build_deps(settings: Settings) -> ApiDeps:
    """Production wiring: Postgres, Redis, configured artifact store and providers."""
    # Imported lazily so tests that pass their own deps never import SDKs/drivers.
    from agentplatform.artifacts import create_artifact_store
    from agentplatform.bus.redis_bus import RedisEventBus
    from agentplatform.db.event_store import PostgresEventStore
    from agentplatform.db.session import create_engine, create_sessionmaker
    from agentplatform.models.factory import build_client
    from agentplatform.models.shared import SharedModelState
    from agentplatform.sandboxes.docker_local import DockerLocalProvider
    from agentplatform.sandboxes.e2b_provider import E2BProvider

    models = load_models_config(settings.resolve_path(settings.app_models_file))
    policies = load_policies_config(settings.resolve_path(settings.app_policies_file))
    engine = create_engine(settings)
    sessionmaker = create_sessionmaker(engine)
    bus = RedisEventBus.from_url(settings.effective_redis_url)
    tickets = RedisTicketStore.from_url(settings.effective_redis_url)
    artifacts = await create_artifact_store(settings)
    providers: dict[str, SandboxProvider] = {
        "docker_local": DockerLocalProvider.from_settings(settings)
    }
    if settings.e2b_api_key is not None and settings.e2b_api_key.get_secret_value():
        providers["e2b"] = E2BProvider.from_settings(settings)
    shared = SharedModelState()

    def factory(role: RoleConfig) -> ModelClient:
        return build_client(role, models.prices, shared=shared, env=os.environ)

    return ApiDeps(
        settings=settings,
        models=models,
        policies=policies,
        sessionmaker=sessionmaker,
        events=PostgresEventStore(sessionmaker),
        bus=bus,
        artifacts=artifacts,
        tickets=tickets,
        sandbox_providers=providers,
        model_client_factory=factory,
        closers=[engine.dispose, bus.aclose, tickets.aclose],
    )


def get_deps(request: Request) -> ApiDeps:
    deps: ApiDeps = request.app.state.deps
    return deps


Deps = Annotated[ApiDeps, Depends(get_deps)]


async def get_tx(deps: Deps) -> AsyncIterator[AsyncSession]:
    """One read transaction per request (the API never writes read models directly)."""
    async with deps.sessionmaker() as tx, tx.begin():
        yield tx


Tx = Annotated[AsyncSession, Depends(get_tx)]
