from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast
from unittest.mock import AsyncMock, patch

import pytest

from agentplatform.artifacts.base import ArtifactStore
from agentplatform.artifacts.fs_store import FileSystemArtifactStore
from agentplatform.artifacts.service import ArtifactService, sandbox_path
from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import Actor, new_id, utcnow
from agentplatform.contracts.events import (
    ArtifactFetched,
    ArtifactPublished,
    BoardEntryPosted,
    EventDraft,
)
from agentplatform.errors import (
    ArtifactTooLargeError,
    ConflictError,
    InvalidRequestError,
    NotFoundError,
)
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.loop import AgentRuntime, AgentRuntimeConfig, build_agent_registry
from agentplatform.runtime.agent.tools.artifacts import register_artifact_tools
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.runtime.coordination.prefetch import ArtifactPrefetch, StoreArtifactPrefetch
from agentplatform.testing import (
    FakeModel,
    FakeSandbox,
    InMemoryArtifactStore,
    InMemoryBus,
    InMemoryEventStore,
    make_agent,
    make_session,
    make_task,
    multi_tool_response,
    tool_response,
)


class MemoryMetadata:
    def __init__(self) -> None:
        self.rows: dict[str, Artifact] = {}
        self.fail_insert = False

    async def get(self, artifact_id: str) -> Artifact:
        if artifact_id not in self.rows:
            raise NotFoundError("missing artifact")
        return self.rows[artifact_id]

    async def latest(self, session_id: str, name: str) -> Artifact | None:
        candidates = [
            a for a in self.rows.values() if a.session_id == session_id and a.name == name
        ]
        return max(candidates, key=lambda a: a.version, default=None)

    async def insert(self, artifact: Artifact, tx: object) -> None:
        if self.fail_insert:
            raise ConflictError("injected conflict")
        if any(
            (a.session_id, a.name, a.version)
            == (artifact.session_id, artifact.name, artifact.version)
            for a in self.rows.values()
        ):
            raise ConflictError("duplicate version")
        self.rows[artifact.id] = artifact


class RecordingBoard:
    def __init__(self, events: EventRecorder) -> None:
        self.events = events
        self.entries: list[BoardEntry] = []

    async def post(
        self, session_id: str, draft: BoardEntryDraft, *, author: Actor, step: int | None = None
    ) -> BoardEntry:
        def build(seq: int) -> EventDraft:
            entry = BoardEntry(
                **draft.model_dump(),
                id=new_id(),
                session_id=session_id,
                author=author,
                seq=seq,
                created_at=utcnow(),
            )
            return EventDraft(actor=author, payload=BoardEntryPosted(entry=entry))

        event = await self.events.record(session_id, build)
        assert isinstance(event.payload, BoardEntryPosted)
        self.entries.append(event.payload.entry)
        return event.payload.entry


class Harness:
    def __init__(self, store: ArtifactStore) -> None:
        self.store = store
        self.metadata = MemoryMetadata()
        self.log = InMemoryEventStore()
        self.bus = InMemoryBus()
        self.events = EventRecorder(self.log, self.bus)
        self.recording_board = RecordingBoard(self.events)
        self.board = cast(BoardService, self.recording_board)
        self.service = ArtifactService(self.metadata, self.events, max_bytes=32)
        self.source = FakeSandbox()
        self.target = FakeSandbox()

    async def publish(self, data: bytes = b"hello", name: str = "report.txt") -> Artifact:
        await self.source.write_file("/home/agent/report", data)
        return await self.service.publish(
            store=self.store,
            board=self.board,
            sandbox=self.source,
            session_id="s",
            actor=Actor.agent("source", "Ada"),
            task_key="T1",
            step=1,
            path="/home/agent/report",
            name=name,
        )

    async def fetch(self, artifact_id: str, **kwargs: Any) -> str:
        return await self.service.fetch(
            artifact_id,
            store=self.store,
            sandbox=self.target,
            session_id="s",
            actor=Actor.agent("target", "Ben"),
            agent_id="target",
            **kwargs,
        )


@pytest.fixture(params=["memory", "fs"])
def harness(request: pytest.FixtureRequest, tmp_path: Path) -> Harness:
    return Harness(
        InMemoryArtifactStore()
        if request.param == "memory"
        else FileSystemArtifactStore(tmp_path.resolve())
    )


async def test_publish_fetch_versions_and_events(harness: Harness) -> None:
    a = await harness.publish()
    b = await harness.publish(b"updated")
    assert (a.version, b.version) == (1, 2)
    assert a.storage_key != b.storage_key
    assert a.mime == "text/plain"
    path = await harness.fetch(a.id)
    assert path == "/home/agent/Shared/report.txt"
    assert await harness.target.read_file(path) == b"hello"
    events = harness.log.all("s")
    assert [e.type for e in events] == [
        "artifact.published",
        "board.entry_posted",
        "artifact.published",
        "board.entry_posted",
        "artifact.fetched",
    ]
    assert isinstance(events[0].payload, ArtifactPublished)
    assert events[0].payload.artifact == a
    assert isinstance(events[-1].payload, ArtifactFetched)
    assert events[-1].payload.artifact_id == a.id
    assert events[-1].payload.sandbox_path == path
    assert harness.recording_board.entries[0].artifact_id == a.id


async def test_size_limit_and_boundary(harness: Harness) -> None:
    await harness.publish(b"x" * 32)
    before = len(harness.log.all("s"))
    with pytest.raises(ArtifactTooLargeError):
        await harness.publish(b"x" * 33)
    assert len(harness.log.all("s")) == before
    assert len(harness.metadata.rows) == 1


async def test_metadata_failure_records_no_event(harness: Harness) -> None:
    harness.metadata.fail_insert = True
    with pytest.raises(ConflictError):
        await harness.publish()
    assert harness.log.all("s") == []
    assert harness.metadata.rows == {}
    assert harness.recording_board.entries == []


async def test_concurrent_versions(harness: Harness) -> None:
    artifacts = await asyncio.gather(*(harness.publish() for _ in range(4)))
    assert sorted(a.version for a in artifacts) == [1, 2, 3, 4]


@pytest.mark.parametrize(
    "path",
    [
        "../x",
        "/etc/passwd",
        "/home/agent/../secret",
        "/home/agentx/x",
        "//home/agent/x",
        "/home/agent/..／x",
        "/home/agent/x\x00",
    ],
)
def test_sandbox_paths(path: str) -> None:
    # Unicode names are data inside the allowed root, never normalized as separators.
    if path == "/home/agent/..／x":
        assert sandbox_path(path) == path
    else:
        with pytest.raises(InvalidRequestError):
            sandbox_path(path)


async def test_fetch_security(harness: Harness) -> None:
    artifact = await harness.publish()
    with pytest.raises(InvalidRequestError):
        await harness.fetch(artifact.id, directory="/home/agent/elsewhere")
    with pytest.raises(NotFoundError):
        await harness.fetch("missing")
    harness.metadata.rows[artifact.id] = artifact.model_copy(update={"session_id": "other"})
    with pytest.raises(NotFoundError):
        await harness.fetch(artifact.id)
    harness.metadata.rows[artifact.id] = artifact.model_copy(update={"name": "../escape"})
    with pytest.raises(InvalidRequestError):
        await harness.fetch(artifact.id)
    harness.metadata.rows[artifact.id] = artifact
    await harness.store.put(b"corrupt", content_type="text/plain", key=artifact.storage_key)
    with pytest.raises(InvalidRequestError):
        await harness.fetch(artifact.id)
    assert harness.target.files == {}
    assert harness.log.of_type("s", "artifact.fetched") == []


async def test_prefetch_before_first_model_step_and_tool_context(harness: Harness) -> None:
    a = await harness.publish()
    prefetch: ArtifactPrefetch = StoreArtifactPrefetch(harness.service, harness.store)
    paths = await prefetch.prefetch(
        session_id="s", agent_id="target", sandbox=harness.target, artifact_ids=[a.id, a.id]
    )
    assert paths == ["/home/agent/Shared/report.txt"]
    session = make_session(session_id="s")
    agent = make_agent(session_id="s", agent_id="target")
    registry = build_agent_registry()
    register_artifact_tools(registry, harness.service)
    calls = 0

    def script(request: Any) -> Any:
        nonlocal calls
        calls += 1
        assert harness.target.files[paths[0]] == b"hello"
        assert len(harness.log.of_type("s", "artifact.fetched")) >= 1
        if calls == 1:
            return multi_tool_response(
                ("publish_artifact", {"sandbox_path": paths[0], "name": "copy.txt"}),
                ("key", {"combo": "ctrl+a"}),
            )
        if calls == 2:
            return multi_tool_response(
                ("fetch_artifact", {"artifact_id": a.id}), ("key", {"combo": "ctrl+a"})
            )
        return tool_response("fail_task", reason="test finished")

    runtime = AgentRuntime(
        session=session,
        agent=agent,
        sandbox=harness.target,
        model=FakeModel({agent.name: [script, script, script]}),
        events=harness.events,
        bus=harness.bus,
        frames=harness.store,
        board=harness.board,
        registry=registry,
        config=AgentRuntimeConfig(error_backoff_s=0),
    )
    outcome = await runtime.run_task(make_task(session_id="s"))
    assert outcome.steps == 3
    assert len(harness.log.of_type("s", "artifact.published")) == 2
    assert len(harness.log.of_type("s", "artifact.fetched")) == 2


async def test_prefetch_collision_order_and_failure(harness: Harness) -> None:
    a = await harness.publish(b"first")
    b = await harness.publish(b"second")
    prefetch = StoreArtifactPrefetch(harness.service, harness.store)
    assert (
        await prefetch.prefetch(
            session_id="s", agent_id="target", sandbox=harness.target, artifact_ids=[]
        )
        == []
    )
    await prefetch.prefetch(
        session_id="s", agent_id="target", sandbox=harness.target, artifact_ids=[a.id, b.id, a.id]
    )
    assert harness.target.files["/home/agent/Shared/report.txt"] == b"second"
    with pytest.raises(NotFoundError):
        await prefetch.prefetch(
            session_id="s", agent_id="target", sandbox=harness.target, artifact_ids=["missing"]
        )


@pytest.mark.parametrize("name", ["../escape", "/absolute", "．．", "a∕b", "a\\b", "bad\x00"])
async def test_publish_rejects_unsafe_names(harness: Harness, name: str) -> None:
    with pytest.raises(InvalidRequestError):
        await harness.publish(name=name)
    assert harness.metadata.rows == {}
    assert harness.log.all("s") == []


async def test_publish_rejects_path_before_sandbox_io(harness: Harness) -> None:
    with pytest.raises(InvalidRequestError):
        await harness.service.publish(
            store=harness.store,
            board=harness.board,
            sandbox=harness.source,
            session_id="s",
            actor=Actor.agent("source", "Ada"),
            task_key="T1",
            step=1,
            path="/etc/passwd",
        )
    assert harness.source.actions == []
    assert harness.log.all("s") == []


async def test_fetch_size_limit(harness: Harness) -> None:
    artifact = await harness.publish()
    harness.metadata.rows[artifact.id] = artifact.model_copy(update={"size_bytes": 33})
    with pytest.raises(ArtifactTooLargeError):
        await harness.fetch(artifact.id)
    harness.metadata.rows[artifact.id] = artifact
    await harness.store.put(b"x" * 33, content_type="text/plain", key=artifact.storage_key)
    with pytest.raises(ArtifactTooLargeError):
        await harness.fetch(artifact.id)
    assert harness.target.files == {}
    assert harness.log.of_type("s", "artifact.fetched") == []


@pytest.mark.parametrize(
    ("data", "name", "mime"),
    [
        (b"%PDF-1.7", "report.txt", "application/pdf"),
        (b"\x89PNG\r\n\x1a\n", "image.bin", "image/png"),
        (b"\xff\xd8\xff", "photo.bin", "image/jpeg"),
        (b"\x00\x01", "data.txt", "application/octet-stream"),
        (b"{}", "data.json", "application/json"),
    ],
)
async def test_mime_sniffing(harness: Harness, data: bytes, name: str, mime: str) -> None:
    artifact = await harness.publish(data, name)
    assert artifact.mime == mime


@pytest.mark.parametrize("prefetch", [False, True], ids=["fetch", "prefetch"])
async def test_missing_and_cross_session_artifacts_are_indistinguishable(
    harness: Harness, prefetch: bool
) -> None:
    artifact = await harness.publish()
    harness.metadata.rows[artifact.id] = artifact.model_copy(update={"session_id": "other"})
    transfer = StoreArtifactPrefetch(harness.service, harness.store)
    errors: list[tuple[str, int, str]] = []
    with patch.object(harness.store, "get", new_callable=AsyncMock) as read_blob:
        for artifact_id in ("missing", artifact.id):
            attempt = (
                transfer.prefetch(
                    session_id="s",
                    agent_id="target",
                    sandbox=harness.target,
                    artifact_ids=[artifact_id],
                )
                if prefetch
                else harness.fetch(artifact_id)
            )
            with pytest.raises(NotFoundError) as caught:
                await attempt
            error = caught.value
            errors.append((error.code, error.status, str(error)))
        read_blob.assert_not_awaited()
    assert errors == [("not_found", 404, "artifact not found in this session")] * 2
    assert harness.target.files == {}
    assert harness.target.actions == []
    assert harness.log.of_type("s", "artifact.fetched") == []
