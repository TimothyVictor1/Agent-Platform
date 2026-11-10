"""The runtime over the real stores (marker ``infra``): Postgres event store with the
read-model projector, Postgres board, approvals, artifacts and session directory,
and the Redis control store, token store and command log. Models and sandboxes are
fakes; the bus is in memory."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from agentplatform.artifacts.service import ArtifactService, DatabaseArtifactMetadata
from agentplatform.config import load_settings
from agentplatform.contracts.common import new_id
from agentplatform.db.event_store import PostgresEventStore
from agentplatform.db.repositories import Repositories
from agentplatform.db.session import SessionMaker, transaction
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.container import Container, RedisCommandLog, RuntimeKnobs
from agentplatform.runtime.controls import (
    ControlRecord,
    RedisControlStore,
    RedisControlTokenStore,
)
from agentplatform.runtime.coordination.digest import postgres_task_lister
from agentplatform.runtime.coordination.postgres_board import PostgresBoardService
from agentplatform.runtime.hitl import HitlApprovalGate, PostgresApprovalStore
from agentplatform.runtime.main import Runtime
from agentplatform.runtime.policies import Policy
from agentplatform.runtime.readmodels import (
    PostgresSessionDirectory,
    ProjectingEventStore,
    project_read_models,
)
from agentplatform.testing import make_tasks

from .support import FAST_KNOBS, World, until

pytestmark = pytest.mark.infra


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(load_settings().effective_redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def prefix() -> str:
    return f"t13test:{new_id()}"


async def test_redis_control_store_round_trip(redis: Redis, prefix: str) -> None:
    store = RedisControlStore(redis, prefix=prefix)
    await store.save("s1", "a1", ControlRecord(holds={"takeover", "session_pause"}))
    await store.save("s1", "a2", ControlRecord(stopped=True))
    loaded = await store.load("s1")
    assert loaded["a1"].mode == "takeover"
    assert loaded["a2"].mode == "stop"
    assert 0 < await redis.ttl(store.key("s1")) <= 7 * 24 * 3600
    await store.clear("s1")
    assert await store.load("s1") == {}


async def test_redis_token_store_expires_with_the_sandbox(redis: Redis, prefix: str) -> None:
    tokens = RedisControlTokenStore(redis, prefix=prefix)
    await tokens.put("sbx", "tok-123")  # gitleaks:allow
    assert await tokens.get("sbx") == "tok-123"  # gitleaks:allow
    ttl = await redis.ttl(tokens.key("sbx"))
    assert 24 * 3600 < ttl <= 24 * 3600 + 15 * 60  # lifetime unknown: 24 h + margin
    await tokens.delete("sbx")
    assert await tokens.get("sbx") is None


async def test_redis_command_log(redis: Redis, prefix: str) -> None:
    log = RedisCommandLog(redis, prefix=prefix, ttl_s=60)
    assert not await log.seen("c1")
    await log.mark("c1")
    assert await log.seen("c1")
    await redis.delete(f"{prefix}:cmd-done:c1")


@pytest.fixture
async def pg_world(
    sessionmaker: SessionMaker, redis: Redis, prefix: str
) -> AsyncIterator[tuple[World, Container]]:
    world = World()
    store = ProjectingEventStore(PostgresEventStore(sessionmaker), project_read_models)
    recorder = EventRecorder(store, world.bus)
    board = PostgresBoardService(sessionmaker, recorder, poll_interval_s=0.05)
    gate = HitlApprovalGate(events=recorder, store=PostgresApprovalStore(sessionmaker), board=board)
    world.store, world.recorder = store, recorder  # sessions are created through these
    container = Container(
        store=store,
        bus=world.bus,
        leases=world.leases,
        recorder=recorder,
        directory=PostgresSessionDirectory(sessionmaker),
        frames=world.frames,
        board=board,
        task_lister=postgres_task_lister(sessionmaker),
        approvals=gate,
        control_store=RedisControlStore(redis, prefix=prefix),
        command_log=RedisCommandLog(redis, prefix=prefix, ttl_s=600),
        models=world.models,
        providers={"docker_local": world.provider},
        policy=Policy.from_config(world.policies),
        policies=world.policies,
        knobs=RuntimeKnobs(runtime_id="rt-pg", **FAST_KNOBS),  # type: ignore[arg-type]
        artifacts=ArtifactService(DatabaseArtifactMetadata(sessionmaker), recorder),
        closers=[gate.aclose],
    )
    yield world, container
    await container.aclose()


async def test_session_runs_end_to_end_over_postgres(
    pg_world: tuple[World, Container], sessionmaker: SessionMaker
) -> None:
    world, container = pg_world
    world.models.planner.plans = [make_tasks({"T1": [], "T2": ["T1"]})]
    rt = Runtime(container)
    await rt.start()
    try:
        session = await world.create_session(agent_count=2)
        plan = await world.send("command.plan_session", session.id)

        async def status() -> str:
            async with transaction(sessionmaker) as tx:
                return (await Repositories(tx).sessions.get(session.id)).status

        async def wait_status(wanted: str) -> None:
            async def reached() -> bool:
                return await status() == wanted

            await until(reached, limit_s=20)

        await wait_status("awaiting_approval")
        events = await container.store.read(session.id)
        proposed = next(e for e in events if e.type == "session.plan_proposed")
        assert proposed.causation_id == plan.id
        await world.send("command.approve_plan", session.id, plan_version=1)
        await wait_status("completed")
        async with transaction(sessionmaker) as tx:
            repos = Repositories(tx)
            tasks = await repos.tasks.list(session.id)
            assert sorted((t.key, t.status) for t in tasks) == [("T1", "done"), ("T2", "done")]
            agents = await repos.agents.list(session.id)
            assert len(agents) == 2
            assert all(a.sandbox is not None for a in agents)
            s = await repos.sessions.get(session.id)
            assert s.usage.steps > 0
            assert s.plan_version == 1
        # this runtime keeps the session (lease) while it waits for follow-ups
        assert not await container.directory.claim(session.id, "rt-other", 30)
    finally:
        await rt.shutdown()
