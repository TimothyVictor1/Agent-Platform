"""The scenario world over the real Postgres stores (marker ``infra``): event store
with the read-model projector, session directory (leases), board, approvals, artifact
metadata and the task lister. Models, sandboxes, the bus, the control store and the
command log stay fakes, so the tests need Postgres only (``make db-up``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runtime.support import FAST_KNOBS
from sqlalchemy import text

from agentplatform.artifacts.service import ArtifactService, DatabaseArtifactMetadata
from agentplatform.contracts.events import Event, EventDraft
from agentplatform.db.event_store import DraftFactory, PostgresEventStore, ReadModelUpdate
from agentplatform.db.session import SessionMaker, transaction
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.container import Container, RuntimeKnobs
from agentplatform.runtime.coordination.digest import postgres_task_lister
from agentplatform.runtime.coordination.postgres_board import PostgresBoardService
from agentplatform.runtime.hitl import HitlApprovalGate, PostgresApprovalStore
from agentplatform.runtime.policies import Policy
from agentplatform.runtime.readmodels import (
    PostgresSessionDirectory,
    ProjectingEventStore,
    project_read_models,
)
from scenarios.harness import ScenarioWorld


class MirroredStore:
    """``PostgresEventStore`` that also keeps every event it appended in memory, so
    the world's synchronous queries (``events``) work unchanged. Postgres stays the
    store the runtime reads from."""

    def __init__(self, inner: PostgresEventStore) -> None:
        self.inner = inner
        self._events: dict[str, list[Event]] = {}

    async def append(
        self,
        session_id: str,
        draft: EventDraft | DraftFactory,
        read_model_update: ReadModelUpdate | None = None,
    ) -> Event:
        event = await self.inner.append(session_id, draft, read_model_update)
        self._events.setdefault(session_id, []).append(event)
        self._events[session_id].sort(key=lambda e: e.seq)
        return event

    async def read(self, session_id: str, after_seq: int = 0, limit: int = 500) -> list[Event]:
        return await self.inner.read(session_id, after_seq, limit)

    async def last_seq(self, session_id: str) -> int:
        return await self.inner.last_seq(session_id)

    def all(self, session_id: str) -> list[Event]:
        return list(self._events.get(session_id, []))

    def of_type(self, session_id: str, event_type: str) -> list[Event]:
        return [e for e in self._events.get(session_id, []) if e.type == event_type]


@dataclass
class PgScenarioWorld(ScenarioWorld):
    """``ScenarioWorld`` whose stores are the Postgres ones; call ``attach`` first."""

    sessionmaker: SessionMaker | None = None
    pg_directory: PostgresSessionDirectory | None = None
    approval_store: PostgresApprovalStore | None = None
    _gates: list[HitlApprovalGate] = field(default_factory=list)

    def attach(self, sessionmaker: SessionMaker) -> None:
        self.sessionmaker = sessionmaker
        mirror = MirroredStore(PostgresEventStore(sessionmaker))
        self.raw = mirror  # type: ignore[assignment]  # same query surface as InMemoryEventStore
        self.store = ProjectingEventStore(mirror, project_read_models)
        self.recorder = EventRecorder(self.store, self.bus)
        self.board = PostgresBoardService(sessionmaker, self.recorder, poll_interval_s=0.05)  # type: ignore[assignment]
        self.pg_directory = PostgresSessionDirectory(sessionmaker)
        self.approval_store = PostgresApprovalStore(sessionmaker)

    def container(self, runtime_id: str, **knobs: Any) -> Container:
        assert self.sessionmaker is not None, "call attach() first"
        assert self.pg_directory is not None
        assert self.approval_store is not None
        gate = HitlApprovalGate(events=self.recorder, store=self.approval_store, board=self.board)
        return Container(
            store=self.store,
            bus=self.bus,
            leases=self.leases,
            recorder=self.recorder,
            directory=self.pg_directory,
            frames=self.frames,
            board=self.board,
            task_lister=postgres_task_lister(self.sessionmaker),
            approvals=gate,
            control_store=self.control_store,
            command_log=self.command_log,
            models=self.models,
            providers={"docker_local": self.provider},
            policy=Policy.from_config(self.policies),
            policies=self.policies,
            knobs=RuntimeKnobs(runtime_id=runtime_id, **{**FAST_KNOBS, **self.knobs, **knobs}),
            artifacts=ArtifactService(DatabaseArtifactMetadata(self.sessionmaker), self.recorder),
            closers=[gate.aclose],
        )

    async def expire_session_lease(self, session_id: str) -> None:
        """What happens when a dead runtime stops renewing its session lease."""
        assert self.sessionmaker is not None
        async with transaction(self.sessionmaker) as tx:
            await tx.execute(
                text(
                    "UPDATE sessions SET runtime_lease_expires_at = now() - interval '1 second'"
                    " WHERE id = :id"
                ),
                {"id": session_id},
            )
