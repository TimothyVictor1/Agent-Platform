"""RedisEventBus: session streams, ephemeral fan-out, acked work queues (SPEC §7.2, §12.4)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from redis.asyncio import Redis

from agentplatform.api.deps import build_deps
from agentplatform.bus.base import (
    NEW_SESSIONS_QUEUE,
    BusRecord,
    EventBus,
    agent_queue,
    session_command_queue,
)
from agentplatform.bus.redis_bus import RedisEventBus, redis_prefix
from agentplatform.config import Settings, load_settings
from agentplatform.contracts.commands import PlanSessionCommand
from agentplatform.contracts.common import Actor, new_id, utcnow
from agentplatform.contracts.events import (
    Event,
    EventDraft,
    EventEnvelope,
    Heartbeat,
    SessionPaused,
    TaskReady,
    build_event,
)
from agentplatform.runtime.container import build_container

pytestmark = pytest.mark.infra

BusFactory = Callable[..., RedisEventBus]


@pytest.fixture
async def make_bus(redis: Redis, prefix: str) -> AsyncIterator[BusFactory]:
    buses: list[RedisEventBus] = []

    def factory(**kwargs: object) -> RedisEventBus:
        bus = RedisEventBus(redis, prefix=prefix, **kwargs)  # type: ignore[arg-type]
        buses.append(bus)
        return bus

    yield factory
    for bus in buses:
        await bus.aclose()


@pytest.fixture
def bus(make_bus: BusFactory) -> RedisEventBus:
    return make_bus(subscribe_block_s=0.2, claim_interval_s=0.05)


def event(session_id: str, seq: int, reason: str = "") -> Event:
    draft = EventDraft(payload=SessionPaused(reason=reason or f"e{seq}"), actor=Actor.system())
    return build_event(draft, session_id=session_id, seq=seq)


def heartbeat(session_id: str, step: int = 0) -> Heartbeat:
    return Heartbeat(session_id=session_id, agent_id="a-ada", step=step, ts=utcnow())


def command(session_id: str) -> PlanSessionCommand:
    return PlanSessionCommand(
        id=new_id(), session_id=session_id, issued_at=utcnow(), issued_by=Actor.user("local")
    )


async def take(agen: AsyncIterator[BusRecord], n: int, within: float = 5.0) -> list[BusRecord]:
    async def collect() -> list[BusRecord]:
        return [await anext(agen) for _ in range(n)]

    return await asyncio.wait_for(collect(), within)


def test_implements_protocol(bus: RedisEventBus) -> None:
    assert isinstance(bus, EventBus)


def test_empty_worktree_uses_default_redis_prefix() -> None:
    assert redis_prefix("") == "agentplatform"


async def test_worktrees_do_not_consume_each_others_commands(redis: Redis, tmp_path: Path) -> None:
    base = load_settings()
    worktrees = [f"test-{new_id()}" for _ in range(2)]
    async with contextlib.AsyncExitStack() as stack:
        apis, runtimes = [], []
        for worktree in worktrees:
            settings = Settings(
                app_worktree=worktree,
                redis_url=base.effective_redis_url,
                database_url=base.effective_database_url,
                app_models_file="config/models.example.yaml",
                app_policies_file="config/policies.example.yaml",
                artifact_store="fs",
                artifact_dir=str(tmp_path / worktree),
                e2b_api_key=None,
            )
            api = await build_deps(settings)
            stack.push_async_callback(api.aclose)
            runtime = await build_container(settings)
            stack.push_async_callback(runtime.aclose)
            apis.append(api)
            runtimes.append(runtime)

        queue = NEW_SESSIONS_QUEUE  # identical queue name in both worktrees
        try:
            for idx, api in enumerate(apis):
                cmd = command(new_id())
                await api.commands.send(cmd)
                assert await runtimes[1 - idx].bus.dequeue(queue, "runtime", timeout_s=0) is None
                bus = runtimes[idx].bus
                delivery = await bus.dequeue(queue, "runtime", timeout_s=0)
                assert delivery is not None
                assert delivery.item == cmd
                await bus.ack(delivery)
        finally:
            for worktree in worktrees:
                key = RedisEventBus(redis, prefix=redis_prefix(worktree)).queue_key(queue)
                await redis.delete(key)


# --- session streams --------------------------------------------------------------------


async def test_subscribe_replays_from_position_in_order(bus: RedisEventBus) -> None:
    sid = new_id()
    published = [event(sid, i) for i in range(1, 21)]
    positions = [await bus.publish(e) for e in published]
    assert positions == sorted(positions, key=lambda p: tuple(map(int, p.split("-"))))

    everything = bus.subscribe(sid, from_id="0-0")
    records = await take(everything, 20)
    await everything.aclose()
    assert [r.item for r in records] == published
    assert [r.position for r in records] == positions

    resumed = bus.subscribe(sid, from_id=positions[9])
    rest = await take(resumed, 10)
    await resumed.aclose()
    assert [r.item for r in rest] == published[10:]


async def test_live_subscription_starts_after_existing_records_and_keeps_order(
    bus: RedisEventBus,
) -> None:
    sid = new_id()
    old = event(sid, 1, "before subscribe")
    await bus.publish(old)
    received: list[BusRecord] = []
    first_seen = asyncio.Event()
    done = asyncio.Event()
    live = [event(sid, s, "live") for s in range(1000, 1030)]
    agen = bus.subscribe(sid, include_ephemeral=False)

    async def consume() -> None:
        async for record in agen:
            received.append(record)
            first_seen.set()
            if record.item == live[-1]:
                done.set()

    consumer = asyncio.create_task(consume())
    try:
        # Publish sentinels until the subscriber is demonstrably live.
        seq = 2
        while not first_seen.is_set():
            await bus.publish(event(sid, seq, "sentinel"))
            seq += 1
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(first_seen.wait(), 0.05)
        for e in live:
            await bus.publish(e)
        await asyncio.wait_for(done.wait(), 5)
    finally:
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)

    items = [r.item for r in received]
    assert old not in items
    assert items[-30:] == live
    seqs = [i.seq for i in items if isinstance(i, EventEnvelope)]
    assert seqs == sorted(seqs)


async def test_ephemeral_messages_are_fanned_out_but_not_replayed(bus: RedisEventBus) -> None:
    sid = new_id()
    await bus.publish_ephemeral(heartbeat(sid, 1))  # before subscribing: never delivered
    first = event(sid, 1)
    await bus.publish(first)

    with_ephemeral = bus.subscribe(sid, from_id="0-0")
    only_events = bus.subscribe(sid, from_id="0-0", include_ephemeral=False)
    assert (await take(with_ephemeral, 1))[0].item == first
    assert (await take(only_events, 1))[0].item == first

    beat = heartbeat(sid, 2)
    second = event(sid, 2)
    await bus.publish_ephemeral(beat)
    await bus.publish(second)

    got = await take(with_ephemeral, 2)
    assert {(r.position is None, r.item.type) for r in got} == {
        (True, "agent.heartbeat"),
        (False, "session.paused"),
    }
    assert next(r.item for r in got if r.position is None) == beat
    assert [r.item for r in await take(only_events, 1)] == [second]
    for agen in (with_ephemeral, only_events):
        await agen.aclose()


async def test_streams_are_trimmed(make_bus: BusFactory, redis: Redis) -> None:
    bus = make_bus(event_maxlen=10, ephemeral_maxlen=10)
    sid = new_id()
    for i in range(1, 501):
        await bus.publish(event(sid, i))
        await bus.publish_ephemeral(heartbeat(sid, i))
    # MAXLEN ~ trims whole macro nodes, so the length is bounded but not exact.
    assert await redis.xlen(bus.events_key(sid)) < 500
    assert await redis.xlen(bus.ephemeral_key(sid)) < 500


async def test_undecodable_stream_entries_are_skipped(bus: RedisEventBus, redis: Redis) -> None:
    sid = new_id()
    await redis.xadd(bus.events_key(sid), {"e": "{not json"})
    good = event(sid, 1)
    await bus.publish(good)
    agen = bus.subscribe(sid, from_id="0-0")
    assert [r.item for r in await take(agen, 1)] == [good]
    await agen.aclose()


# --- work queues ------------------------------------------------------------------------


async def test_queue_is_fifo_and_round_trips_commands_and_events(bus: RedisEventBus) -> None:
    sid = new_id()
    queue = session_command_queue(sid)
    cmd = command(sid)
    ready = build_event(
        EventDraft(payload=TaskReady(task_id="t-1", task_key="T1"), actor=Actor.system()),
        session_id=sid,
        seq=3,
    )
    ids = [await bus.enqueue(queue, cmd), await bus.enqueue(queue, ready)]

    first = await bus.dequeue(queue, "runtime-1", timeout_s=1)
    second = await bus.dequeue(queue, "runtime-1", timeout_s=1)
    assert first is not None
    assert second is not None
    assert (first.item, first.attempt, first.delivery_id, first.queue) == (cmd, 1, ids[0], queue)
    assert (second.item, second.attempt, second.delivery_id) == (ready, 1, ids[1])
    await bus.ack(first)
    await bus.ack(second)
    await bus.ack(second)  # idempotent
    assert await bus.pending_count(queue) == 0


async def test_dequeue_times_out_on_empty_queue(bus: RedisEventBus) -> None:
    start = time.monotonic()
    assert await bus.dequeue(agent_queue(new_id()), "a", timeout_s=0.3) is None
    assert 0.25 <= time.monotonic() - start < 2
    assert await bus.dequeue(agent_queue(new_id()), "a", timeout_s=0) is None


async def test_unacked_item_is_redelivered_after_visibility_timeout(
    make_bus: BusFactory,
) -> None:
    bus = make_bus(visibility_timeout_s=0.4, claim_interval_s=0.05)
    queue = agent_queue(new_id())
    cmd = command(new_id())
    await bus.enqueue(queue, cmd)

    first = await bus.dequeue(queue, "agent-a", timeout_s=1)
    assert first is not None
    assert first.attempt == 1
    # Still invisible before the timeout.
    assert await bus.dequeue(queue, "agent-b", timeout_s=0.1) is None

    again = await bus.dequeue(queue, "agent-b", timeout_s=3)
    assert again is not None
    assert (again.delivery_id, again.item, again.attempt) == (first.delivery_id, cmd, 2)
    third = await bus.dequeue(queue, "agent-b", timeout_s=3)
    assert third is not None
    assert third.attempt == 3

    await bus.ack(third)
    assert await bus.dequeue(queue, "agent-a", timeout_s=0.6) is None
    assert await bus.pending_count(queue) == 0


async def test_acked_item_is_never_redelivered(make_bus: BusFactory) -> None:
    bus = make_bus(visibility_timeout_s=0.2, claim_interval_s=0.05)
    queue = agent_queue(new_id())
    await bus.enqueue(queue, command(new_id()))
    delivery = await bus.dequeue(queue, "a", timeout_s=1)
    assert delivery is not None
    await bus.ack(delivery)
    assert await bus.dequeue(queue, "a", timeout_s=0.5) is None


async def test_expired_item_behind_many_in_flight_items_is_found(make_bus: BusFactory) -> None:
    """XAUTOCLAIM scans the pending list in slices; the bus must follow the cursor."""
    bus = make_bus(visibility_timeout_s=0.3, claim_interval_s=0.05)
    queue = agent_queue(new_id())
    for _ in range(13):
        await bus.enqueue(queue, command(new_id()))
    taken = [await bus.dequeue(queue, "a", timeout_s=1) for _ in range(13)]
    assert all(d is not None and d.attempt == 1 for d in taken)
    await asyncio.sleep(0.35)  # all 13 expire
    # Re-claim the first 12: they become fresh again and sit before the 13th.
    reclaimed = [await bus.dequeue(queue, "b", timeout_s=1) for _ in range(12)]
    assert [d.delivery_id for d in reclaimed if d] == [d.delivery_id for d in taken[:12] if d]
    last = await bus.dequeue(queue, "b", timeout_s=0.2)
    assert last is not None
    assert taken[12] is not None
    assert (last.delivery_id, last.attempt) == (taken[12].delivery_id, 2)


async def test_concurrent_consumers_get_each_item_once(bus: RedisEventBus) -> None:
    queue = agent_queue(new_id())
    ids = {await bus.enqueue(queue, command(new_id())) for _ in range(20)}

    async def worker(name: str) -> list[str]:
        got: list[str] = []
        while (d := await bus.dequeue(queue, name, timeout_s=0.3)) is not None:
            got.append(d.delivery_id)
            await bus.ack(d)
        return got

    results = await asyncio.gather(*(worker(f"w{i}") for i in range(4)))
    flat = [i for r in results for i in r]
    assert sorted(flat) == sorted(ids)


async def test_items_enqueued_before_any_consumer_are_delivered(
    make_bus: BusFactory, redis: Redis
) -> None:
    producer, consumer = make_bus(), make_bus()
    queue = agent_queue(new_id())
    cmd = command(new_id())
    await producer.enqueue(queue, cmd)
    delivery = await consumer.dequeue(queue, "late", timeout_s=1)
    assert delivery is not None
    assert delivery.item == cmd


async def test_poison_queue_entry_is_dropped(bus: RedisEventBus, redis: Redis) -> None:
    queue = agent_queue(new_id())
    await bus.enqueue(queue, command(new_id()))  # creates the group
    first = await bus.dequeue(queue, "a", timeout_s=1)
    assert first is not None
    await bus.ack(first)
    await redis.xadd(bus.queue_key(queue), {"d": '{"kind":"nope"}'})
    cmd = command(new_id())
    await bus.enqueue(queue, cmd)
    delivery = await bus.dequeue(queue, "a", timeout_s=1)
    assert delivery is not None
    assert delivery.item == cmd
    await bus.ack(delivery)
    assert await bus.pending_count(queue) == 0


@pytest.mark.parametrize(
    "raw",
    [
        "null",
        "[]",
        "[1, 2]",
        "1",
        '"text"',
        "{}",
        '{"kind": "command"}',
        '{"data": {}}',
        '{"kind": "command", "data": null}',
        '{"kind": "command", "data": {"type": "plan_session"}}',
        '{"kind": ["x"], "data": {}}',
        "{not json",
    ],
)
async def test_malformed_queue_envelopes_are_dropped(
    bus: RedisEventBus, redis: Redis, raw: str
) -> None:
    """T04 review #9: valid JSON that is not an envelope must not wedge the queue."""
    queue = agent_queue(new_id())
    await bus.enqueue(queue, command(new_id()))  # creates the group
    first = await bus.dequeue(queue, "a", timeout_s=1)
    assert first is not None
    await bus.ack(first)
    await redis.xadd(bus.queue_key(queue), {"d": raw})
    cmd = command(new_id())
    await bus.enqueue(queue, cmd)
    delivery = await bus.dequeue(queue, "a", timeout_s=1)
    assert delivery is not None
    assert delivery.item == cmd
    await bus.ack(delivery)
    assert await bus.pending_count(queue) == 0


async def test_from_url_owns_and_closes_client(prefix: str) -> None:
    bus = RedisEventBus.from_url(load_settings().effective_redis_url, prefix=prefix)
    await bus.publish(event(new_id(), 1))
    await bus.aclose()
    await bus.aclose()  # idempotent


def test_rejects_non_positive_timeouts(redis: Redis) -> None:
    with pytest.raises(ValueError, match="positive"):
        RedisEventBus(redis, visibility_timeout_s=0)
