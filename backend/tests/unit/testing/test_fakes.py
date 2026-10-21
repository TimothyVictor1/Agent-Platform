"""The fakes behave as documented (other tasks build their tests on them)."""

from __future__ import annotations

import asyncio

import pytest

from agentplatform.artifacts.base import ArtifactStore, content_key, frame_key
from agentplatform.bus.base import (
    EventBus,
    LeaseStore,
    agent_queue,
    decode_queue_item,
    encode_queue_item,
)
from agentplatform.contracts import Actor, new_id, utcnow
from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.commands import PostBoardCommand
from agentplatform.contracts.events import (
    BoardEntryPosted,
    EventDraft,
    Heartbeat,
    SessionCreated,
    SessionPaused,
    TaskReady,
)
from agentplatform.contracts.model_io import Message, ModelRequest
from agentplatform.contracts.sandbox import CommandResult, SandboxSpec
from agentplatform.db.event_store import EventStore
from agentplatform.errors import (
    InvalidRequestError,
    ModelError,
    NotFoundError,
    SandboxError,
    SandboxExpiredError,
)
from agentplatform.eventing import EventRecorder
from agentplatform.models.base import ModelClient
from agentplatform.sandboxes.base import SandboxHandle, SandboxProvider
from agentplatform.testing import (
    FakeModel,
    FakeSandbox,
    FakeSandboxProvider,
    InMemoryArtifactStore,
    InMemoryBus,
    InMemoryEventStore,
    InMemoryLeaseStore,
    assert_has_image,
    make_board_entry,
    make_png,
    make_session,
    text_response,
    tool_response,
)


def agent_request(name: str, *, image: bool = True) -> ModelRequest:
    from agentplatform.contracts.model_io import ImagePart, TextPart

    content: list[TextPart | ImagePart] = [TextPart(text="Step 1")]
    if image:
        content.append(ImagePart(jpeg_bytes=b"jpg", width=4, height=3))
    return ModelRequest(
        messages=[
            Message.text("system", f"You are {name}, one member of a team of 3 AI agents."),
            Message(role="user", content=content),
        ]
    )


# --- protocol conformance --------------------------------------------------------------


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeModel(), ModelClient)
    assert isinstance(FakeSandbox(), SandboxHandle)
    assert isinstance(FakeSandboxProvider(), SandboxProvider)
    assert isinstance(InMemoryBus(), EventBus)
    assert isinstance(InMemoryLeaseStore(), LeaseStore)
    assert isinstance(InMemoryEventStore(), EventStore)
    assert isinstance(InMemoryArtifactStore(), ArtifactStore)


# --- FakeModel -------------------------------------------------------------------------


async def test_fake_model_scripts_per_agent_and_step() -> None:
    model = FakeModel(
        {
            "Ada": [tool_response("click", x=1, y=2), tool_response("key", combo="enter")],
            "Brook": {2: text_response("second")},
        },
        default=text_response("default"),
    )
    r1 = await model.complete(agent_request("Ada"))
    r2 = await model.complete(agent_request("Brook"))
    r3 = await model.complete(agent_request("Ada"))
    r4 = await model.complete(agent_request("Brook"))
    assert r1.tool_calls[0].name == "click"
    assert r2.text == "default"  # Brook step 1 not scripted
    assert r3.tool_calls[0].arguments == {"combo": "enter"}
    assert r4.text == "second"
    assert [(c.agent, c.step) for c in model.calls] == [
        ("Ada", 1), ("Brook", 1), ("Ada", 2), ("Brook", 2),
    ]  # fmt: skip
    assert r1.usage.images == 1


async def test_fake_model_exhausted_without_default_raises() -> None:
    model = FakeModel({"Ada": [text_response("only")]})
    await model.complete(agent_request("Ada"))
    with pytest.raises(ModelError, match="no scripted response"):
        await model.complete(agent_request("Ada"))


async def test_fake_model_raises_scripted_exceptions_and_callables() -> None:
    model = FakeModel(
        {"default": [ModelError("rate limited"), lambda req: text_response(str(len(req.messages)))]}
    )
    req = ModelRequest(messages=[Message.text("user", "plan this")])
    with pytest.raises(ModelError, match="rate limited"):
        await model.complete(req)
    assert (await model.complete(req)).text == "1"


async def test_fake_model_returns_copies() -> None:
    scripted = tool_response("click", x=1, y=2)
    model = FakeModel({"Ada": [scripted]})
    resp = await model.complete(agent_request("Ada"))
    resp.tool_calls[0].arguments["x"] = 99
    assert scripted.tool_calls[0].arguments["x"] == 1


def test_assert_has_image() -> None:
    assert_has_image(agent_request("Ada"))
    with pytest.raises(AssertionError):
        assert_has_image(agent_request("Ada", image=False))


# --- FakeSandbox -----------------------------------------------------------------------


async def test_fake_sandbox_screens_and_actions() -> None:
    a, b = make_png(seed=1), make_png(seed=2)
    box = FakeSandbox([a, b])
    assert await box.screenshot() == a
    assert await box.screenshot() == b
    assert await box.screenshot() == b  # repeats the last
    await box.click(10, 20)
    await box.type_text("hello")
    await box.key("ctrl+l")
    assert box.action_names() == ["click", "type_text", "key"]
    assert box.actions[0].args == {"x": 10, "y": 20, "button": "left", "count": 1}
    with pytest.raises(SandboxError, match="outside"):
        await box.click(1280, 0)


async def test_fake_sandbox_files_and_commands() -> None:
    box = FakeSandbox(command_results={"ls": CommandResult(stdout="a\n", exit_code=0)})
    await box.write_file("/home/agent/Shared/data/x.csv", b"a,b\n1,2\n")
    assert await box.read_file("/home/agent/Shared/data/x.csv") == b"a,b\n1,2\n"
    listing = await box.list_dir("/home/agent/Shared")
    assert [(f.name, f.is_dir) for f in listing] == [("data", True)]
    assert (await box.run_command("ls")).stdout == "a\n"
    cat = await box.run_command("cat /home/agent/Shared/data/x.csv")
    assert cat.stdout == "a,b\n1,2\n"
    assert (await box.run_command("false")).exit_code == 1
    assert (await box.run_command("echo hi there")).stdout == "hi there\n"
    with pytest.raises(SandboxError, match="absolute"):
        await box.write_file("relative.txt", b"")
    with pytest.raises(SandboxError, match="no such file"):
        await box.read_file("/nope")
    with pytest.raises(SandboxError, match="larger"):
        await box.read_file("/home/agent/Shared/data/x.csv", max_bytes=2)


async def test_fake_sandbox_pause_and_kill() -> None:
    box = FakeSandbox()
    await box.pause()
    with pytest.raises(SandboxError, match="paused"):
        await box.click(1, 1)
    await box.resume()
    await box.click(1, 1)
    await box.kill()
    await box.kill()  # idempotent
    with pytest.raises(SandboxExpiredError):
        await box.screenshot()
    assert box.action_names() == ["pause", "resume", "click", "kill"]


async def test_fake_provider_create_connect() -> None:
    provider = FakeSandboxProvider()
    box = await provider.create(SandboxSpec(screen=(1920, 1080), session_id="s"))
    assert box.screen_size == (1920, 1080)
    assert await provider.connect(box.id) is box
    provider.fail_creates = 1
    with pytest.raises(SandboxError):
        await provider.create(SandboxSpec())
    await box.kill()
    with pytest.raises(SandboxExpiredError):
        await provider.connect(box.id)
    urls = await (await provider.create(SandboxSpec())).stream_urls()
    assert "view_only=1" in urls.view_only_url
    assert "view_only" not in urls.interactive_url


# --- InMemoryEventStore + EventRecorder ------------------------------------------------


async def test_event_store_assigns_contiguous_seq_under_concurrency() -> None:
    store = InMemoryEventStore()
    actor = Actor.system()
    events = await asyncio.gather(
        *(store.append("s1", EventDraft(SessionPaused(reason=str(i)), actor)) for i in range(50))
    )
    assert sorted(e.seq for e in events) == list(range(1, 51))
    assert [e.seq for e in await store.read("s1", after_seq=10, limit=5)] == [11, 12, 13, 14, 15]
    assert await store.last_seq("s1") == 50
    assert await store.last_seq("other") == 0


async def test_event_store_strict_mode_and_rollback() -> None:
    store = InMemoryEventStore(strict=True)
    with pytest.raises(NotFoundError):
        await store.append("s1", EventDraft(SessionPaused(), Actor.system()))
    session = make_session(session_id="s1")
    await store.append("s1", EventDraft(SessionCreated(session=session), Actor.user()))

    async def failing_update(event: object, tx: object) -> None:
        raise RuntimeError("read model failed")

    with pytest.raises(RuntimeError):
        await store.append("s1", EventDraft(SessionPaused(), Actor.system()), failing_update)
    assert await store.last_seq("s1") == 1
    ok = await store.append("s1", EventDraft(SessionPaused(), Actor.system()))
    assert ok.seq == 2


async def test_event_store_draft_factory_sees_seq() -> None:
    store = InMemoryEventStore()

    def draft(seq: int) -> EventDraft:
        entry = make_board_entry(seq=seq)
        return EventDraft(BoardEntryPosted(entry=entry), entry.author)

    await store.append("s", EventDraft(SessionPaused(), Actor.system()))
    event = await store.append("s", draft)
    assert isinstance(event.payload, BoardEntryPosted)
    assert event.payload.entry.seq == event.seq == 2


async def test_recorder_appends_then_publishes() -> None:
    store, bus = InMemoryEventStore(), InMemoryBus()
    recorder = EventRecorder(store, bus)
    event = await recorder.record(
        "s", EventDraft(TaskReady(task_id="t", task_key="T1"), Actor.system())
    )
    assert bus.published == [event]
    assert store.all("s") == [event]


async def test_recorder_survives_bus_failure() -> None:
    class BrokenBus(InMemoryBus):
        async def publish(self, event: object) -> str:
            raise ConnectionError("redis down")

    store = InMemoryEventStore()
    recorder = EventRecorder(store, BrokenBus())
    event = await recorder.record("s", EventDraft(SessionPaused(), Actor.system()))
    assert store.all("s") == [event]


# --- InMemoryBus -----------------------------------------------------------------------


async def _collect(bus: InMemoryBus, session_id: str, n: int, **kw: object) -> list[object]:
    out: list[object] = []
    async for record in bus.subscribe(session_id, **kw):  # type: ignore[arg-type]
        out.append(record.item)
        if len(out) == n:
            break
    return out


async def test_bus_subscribe_live_and_replay() -> None:
    store, bus = InMemoryEventStore(), InMemoryBus()
    recorder = EventRecorder(store, bus)
    e1 = await recorder.record("s", EventDraft(SessionPaused(reason="1"), Actor.system()))
    live = asyncio.create_task(_collect(bus, "s", 2))
    await asyncio.sleep(0)
    e2 = await recorder.record("s", EventDraft(SessionPaused(reason="2"), Actor.system()))
    hb = Heartbeat(session_id="s", agent_id="a", ts=utcnow())
    await bus.publish_ephemeral(hb)
    assert await asyncio.wait_for(live, 1) == [e2, hb]

    replay = asyncio.create_task(_collect(bus, "s", 3, from_id="0"))
    await asyncio.sleep(0)
    e3 = await recorder.record("s", EventDraft(SessionPaused(reason="3"), Actor.system()))
    assert await asyncio.wait_for(replay, 1) == [e1, e2, e3]


async def test_bus_subscribe_can_exclude_ephemeral() -> None:
    bus = InMemoryBus()
    store = InMemoryEventStore()
    sub = asyncio.create_task(_collect(bus, "s", 1, include_ephemeral=False))
    await asyncio.sleep(0)
    await bus.publish_ephemeral(Heartbeat(session_id="s", agent_id="a", ts=utcnow()))
    event = await EventRecorder(store, bus).record("s", EventDraft(SessionPaused(), Actor.system()))
    assert await asyncio.wait_for(sub, 1) == [event]


async def test_bus_queue_ack_and_redelivery() -> None:
    now = [1000.0]
    bus = InMemoryBus(visibility_timeout_s=10, clock=lambda: now[0])
    store = InMemoryEventStore()
    event = await store.append(
        "s", EventDraft(TaskReady(task_id="t", task_key="T1"), Actor.system())
    )
    queue = agent_queue("a1")
    await bus.enqueue(queue, event)

    first = await bus.dequeue(queue, "a1", timeout_s=0.1)
    assert first is not None
    assert first.attempt == 1
    assert first.item == event
    assert await bus.dequeue(queue, "a1", timeout_s=0.05) is None  # in flight

    now[0] += 11  # visibility timeout passes without ack
    again = await bus.dequeue(queue, "a1", timeout_s=0.1)
    assert again is not None
    assert again.attempt == 2
    assert again.delivery_id == first.delivery_id
    await bus.ack(again)
    now[0] += 100
    assert await bus.dequeue(queue, "a1", timeout_s=0.05) is None
    assert bus.pending_count(queue) == 0


async def test_bus_dequeue_wakes_on_enqueue() -> None:
    bus = InMemoryBus()
    waiter = asyncio.create_task(bus.dequeue("q", "c", timeout_s=2))
    await asyncio.sleep(0.01)
    command = PostBoardCommand(
        id=new_id(),
        session_id="s",
        issued_at=utcnow(),
        issued_by=Actor.user(),
        entry=BoardEntryDraft(kind="note", title="hi"),
    )
    await bus.enqueue("q", command)
    delivery = await asyncio.wait_for(waiter, 1)
    assert delivery is not None
    assert delivery.item == command


def test_queue_item_codec_roundtrip() -> None:
    command = PostBoardCommand(
        id=new_id(),
        session_id="s",
        issued_at=utcnow(),
        issued_by=Actor.user(),
        entry=BoardEntryDraft(kind="note", title="hi"),
    )
    assert decode_queue_item(encode_queue_item(command)) == command


# --- InMemoryLeaseStore ----------------------------------------------------------------


async def test_leases() -> None:
    now = [100.0]
    leases = InMemoryLeaseStore(clock=lambda: now[0])
    assert await leases.acquire("lease:task:1", "agent-a", 45)
    assert not await leases.acquire("lease:task:1", "agent-b", 45)
    assert await leases.acquire("lease:task:1", "agent-a", 45)  # re-acquire renews
    assert await leases.renew("lease:task:1", "agent-a", 45)
    assert not await leases.renew("lease:task:1", "agent-b", 45)
    holder = await leases.get("lease:task:1")
    assert holder is not None
    assert holder.owner == "agent-a"

    now[0] += 46
    assert await leases.get("lease:task:1") is None
    assert not await leases.renew("lease:task:1", "agent-a", 45)
    expired = await leases.expired("lease:task:")
    assert [e.key for e in expired] == ["lease:task:1"]
    await leases.forget("lease:task:1")
    assert await leases.expired("lease:task:") == []

    assert await leases.acquire("lease:task:2", "x", 10)
    assert not await leases.release("lease:task:2", "y")
    assert await leases.release("lease:task:2", "x")
    assert await leases.acquire("lease:task:2", "y", 10)


# --- InMemoryArtifactStore -------------------------------------------------------------


async def test_artifact_store() -> None:
    store = InMemoryArtifactStore()
    obj = await store.put(b"hello", content_type="text/plain")
    assert obj.key == content_key(obj.sha256)
    assert await store.get(obj.key) == b"hello"
    frame = await store.put(b"jpg", content_type="image/jpeg", key=frame_key("s", "a", 3))
    assert frame.key == "frames/s/a/3.jpg"
    assert [o.key for o in await store.list("frames/")] == ["frames/s/a/3.jpg"]
    assert await store.stat("frames/s/a/4.jpg") is None
    await store.delete(frame.key)
    with pytest.raises(NotFoundError):
        await store.get(frame.key)
    for bad in ("../etc/passwd", "a//b", "/abs", "a/./b", "sp ace"):
        with pytest.raises(InvalidRequestError):
            await store.put(b"x", content_type="text/plain", key=bad)
