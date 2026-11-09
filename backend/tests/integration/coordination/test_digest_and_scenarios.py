"""Digest over a real board, posting helpers, and a two-agent scenario (SPEC §21.4/§21.5)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.model_io import ModelRequest, ModelResponse
from agentplatform.db.session import SessionMaker
from agentplatform.errors import BoardRuleError
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.loop import (
    AgentRuntime,
    AgentRuntimeConfig,
    Digest,
    DigestProvider,
    build_agent_registry,
)
from agentplatform.runtime.agent.tools.coordination import register_coordination_tools
from agentplatform.runtime.coordination.digest import DigestBuilder, postgres_task_lister
from agentplatform.runtime.coordination.postgres_board import PostgresBoardService
from agentplatform.runtime.coordination.posting import (
    answer_as_supervisor,
    message_agent,
    post_decision,
    post_user_steering,
)
from agentplatform.testing import (
    FakeModel,
    FakeSandbox,
    InMemoryArtifactStore,
    make_png,
    make_task,
    multi_tool_response,
    tool_response,
)
from agentplatform.testing.memory_bus import InMemoryBus

from .conftest import World

pytestmark = pytest.mark.infra


def draft(kind: Any = "fact", title: str = "A fact", **kw: Any) -> BoardEntryDraft:
    return BoardEntryDraft(kind=kind, title=title, **kw)


@pytest.fixture
def digests(board: PostgresBoardService, sessionmaker: SessionMaker) -> DigestBuilder:
    return DigestBuilder(board, postgres_task_lister(sessionmaker))


async def test_answer_resolves_and_tops_the_askers_next_digest(
    board: PostgresBoardService, world: World, digests: DigestBuilder
) -> None:
    ada, brook = world.actor(world.ada), world.actor(world.brook)
    q = await board.post(world.id, draft("question", "Which currency?"), author=ada, step=1)
    await board.post(world.id, draft(title="Pricing page found", tags=["pricing"]), author=brook)
    await board.post(world.id, draft("progress", "halfway"), author=brook, step=1)
    answer = await answer_as_supervisor(board, world.id, q.id, "Use EUR")
    assert (await board.get(q.id)).status == "resolved"

    task = make_task(session_id=world.id, tags=["pricing"])
    out = await digests.build(world.ada, task, since=0, budget_tokens=700)
    lines = out.text.split("\n")
    assert lines[0].endswith("Use EUR")
    assert "answer · Supervisor" in lines[0]
    assert len(lines) == 3  # answer, fact, progress; the question itself is Ada's own
    assert out.cursor == answer.seq
    again = await digests.build(world.ada, task, since=out.cursor, budget_tokens=700)
    assert again == Digest("", out.cursor)


async def test_direct_messages_are_invisible_to_others(
    board: PostgresBoardService, world: World, digests: DigestBuilder
) -> None:
    dm = await message_agent(board, world.id, world.brook.id, "Brook, check the Notion pricing")
    task = make_task(session_id=world.id)
    for viewer, expected in ((world.brook, 1), (world.ada, 0), (world.cleo, 0)):
        out = await digests.build(viewer, task, since=0, budget_tokens=700)
        assert len(out.text.splitlines()) == expected
        # Entries the viewer cannot see are never fetched, so they do not move its cursor.
        assert out.cursor == (dm.seq if expected else 0)


async def test_decision_and_user_steering_rank_first(
    board: PostgresBoardService, world: World, digests: DigestBuilder
) -> None:
    await board.post(world.id, draft(title="a fact"), author=world.actor(world.brook))
    await post_decision(board, world.id, "Use EUR everywhere")
    await post_user_steering(board, world.id, "Focus on the cheapest plan", to=[world.ada.id])
    out = await digests.build(world.ada, make_task(session_id=world.id), since=0, budget_tokens=700)
    lines = out.text.split("\n")
    assert "Focus on the cheapest plan" in lines[0]
    assert "note · You" in lines[0]
    assert "Use EUR everywhere" in lines[1]
    # the steering note was addressed to Ada only
    other = await digests.build(
        world.brook, make_task(session_id=world.id), since=0, budget_tokens=700
    )
    assert "cheapest plan" not in other.text


async def test_posting_helpers_enforce_board_rules(
    board: PostgresBoardService, world: World
) -> None:
    with pytest.raises(BoardRuleError, match="empty"):
        await post_user_steering(board, world.id, "   ")
    with pytest.raises(BoardRuleError, match="not an entry"):
        await answer_as_supervisor(board, world.id, "no-such-question", "x")
    long = await post_user_steering(board, world.id, "word " * 80)
    assert len(long.title) <= 120
    assert long.body.startswith("word word")


class GatedDigest:
    """Delegates to a DigestBuilder but, on its Nth build, first waits for ``gate``
    so the scenario controls which agent step sees which board state."""

    def __init__(self, inner: DigestProvider, on_call: int, gate: asyncio.Event) -> None:
        self._inner, self._on_call, self._gate = inner, on_call, gate
        self.calls = 0

    async def build(self, agent: Any, task: Any, *, since: int, budget_tokens: int) -> Digest:
        self.calls += 1
        if self.calls == self._on_call:
            await asyncio.wait_for(self._gate.wait(), 10)
        return await self._inner.build(agent, task, since=since, budget_tokens=budget_tokens)


def click(n: int) -> ModelResponse:
    return tool_response("click", x=20 + 37 * n, y=40 + 11 * n)


async def test_two_agents_a_posts_a_fact_at_step_2_and_b_sees_it_at_step_3(
    board: PostgresBoardService,
    world: World,
    digests: DigestBuilder,
    recorder: EventRecorder,
    bus: InMemoryBus,
) -> None:
    a_posted = asyncio.Event()

    def ada_step_3(_: ModelRequest) -> ModelResponse:
        a_posted.set()  # Ada's step-2 post is committed before her step-3 request is built
        return click(3)

    model = FakeModel(
        {
            "Ada": [
                click(1),
                multi_tool_response(
                    (
                        "post",
                        {
                            "kind": "fact",
                            "title": "Obsidian Sync $4/mo (yearly)",
                            "body": "obsidian.md/pricing",
                            "tags": ["pricing"],
                        },
                    ),
                    ("click", {"x": 200, "y": 300}),
                ),
                ada_step_3,
                tool_response("submit_result", summary="done", outputs={"result": "ok"}),
            ],
            "Brook": [
                click(1),
                click(2),
                click(3),
                click(4),
                tool_response("submit_result", summary="done", outputs={"result": "ok"}),
            ],
        }
    )
    registry = build_agent_registry()
    register_coordination_tools(registry)
    shots = [make_png(seed=i + 1) for i in range(8)]

    def runtime(agent: Any, digest: DigestProvider) -> AgentRuntime:
        return AgentRuntime(
            session=world.session,
            agent=agent,
            sandbox=FakeSandbox(list(shots)),
            model=model,
            events=recorder,
            bus=bus,
            frames=InMemoryArtifactStore(),
            registry=registry,
            config=AgentRuntimeConfig(error_backoff_s=0.0, heartbeat_interval_s=60),
            digest=digest,
            board=board,
        )

    ada_task = make_task(session_id=world.id, key="T1", tags=["pricing"])
    brook_task = make_task(session_id=world.id, key="T2", tags=["pricing"])
    ada_rt = runtime(world.ada, digests)
    brook_rt = runtime(world.brook, GatedDigest(digests, on_call=3, gate=a_posted))
    async with asyncio.timeout(60):
        a_out, b_out = await asyncio.gather(
            ada_rt.run_task(ada_task), brook_rt.run_task(brook_task)
        )
    assert (a_out.status, b_out.status) == ("submitted", "submitted")

    brook_calls = model.calls_for("Brook")
    fact = "[#"  # digest lines start with the entry's seq
    assert "Obsidian Sync" not in brook_calls[0].last_user_text()
    assert "Obsidian Sync" not in brook_calls[1].last_user_text()
    step3 = brook_calls[2].last_user_text()
    assert fact in step3
    assert "fact · Ada · T1] Obsidian Sync $4/mo (yearly) — obsidian.md/pricing" in step3
    # the cursor moved on: the same entry is not repeated, and Ada never sees her own post
    assert "Obsidian Sync" not in brook_calls[3].last_user_text()
    assert all("Obsidian Sync" not in c.last_user_text() for c in model.calls_for("Ada"))
