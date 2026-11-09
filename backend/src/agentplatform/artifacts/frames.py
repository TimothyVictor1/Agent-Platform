"""Frame byte helper for consumers of the loop's ArtifactStore interface."""

from agentplatform.artifacts.base import ArtifactStore, StoredObject, frame_key


async def store_frame(
    store: ArtifactStore,
    jpeg: bytes,
    *,
    session_id: str,
    agent_id: str,
    step: int,
    full: bool = False,
) -> StoredObject:
    return await store.put(
        jpeg, content_type="image/jpeg", key=frame_key(session_id, agent_id, step, full=full)
    )
