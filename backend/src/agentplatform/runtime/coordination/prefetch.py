"""Scheduler pre-step artifact transfer port (T13 will wire this into T11).

The scheduler resolves upstream TaskResult.artifact_ids and awaits prefetch before
calling AgentRuntime.run_task. A failure propagates: step 1 must not start with
partial inputs. IDs are deduplicated in input order. On name collisions, upstream
order determines the final file (last distinct artifact wins), like explicit fetch.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from agentplatform.artifacts.base import ArtifactStore
from agentplatform.artifacts.service import ArtifactService
from agentplatform.contracts.common import Actor
from agentplatform.sandboxes.base import SandboxHandle


class ArtifactPrefetch(Protocol):
    async def prefetch(
        self, *, session_id: str, agent_id: str, sandbox: SandboxHandle, artifact_ids: Sequence[str]
    ) -> list[str]:
        """Fetch upstream IDs to Shared; finish before the task's first step."""
        ...


class StoreArtifactPrefetch:
    def __init__(self, service: ArtifactService, store: ArtifactStore) -> None:
        self._service = service
        self._store = store

    async def prefetch(
        self, *, session_id: str, agent_id: str, sandbox: SandboxHandle, artifact_ids: Sequence[str]
    ) -> list[str]:
        paths: list[str] = []
        for artifact_id in dict.fromkeys(artifact_ids):
            paths.append(
                await self._service.fetch(
                    artifact_id,
                    store=self._store,
                    sandbox=sandbox,
                    session_id=session_id,
                    actor=Actor.agent(agent_id, agent_id),
                    agent_id=agent_id,
                )
            )
        return paths
