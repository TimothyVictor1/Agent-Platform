"""Scenario 4 (SPEC §21): agent A posts a fact; agent B sees it in its next step's
inbox digest, exactly once (B's digest cursor moves past it)."""

from __future__ import annotations

import asyncio

from runtime.support import Behavior, payload

from agentplatform.contracts.events import BoardEntryPosted
from agentplatform.contracts.model_io import ModelResponse
from scenarios.harness import ScenarioWorld, gate, multi_tool_response, shell


async def test_fact_posted_by_a_reaches_bs_next_digest(world: ScenarioWorld) -> None:
    m = world.agent_model
    m.behaviors = {"T1": Behavior(steps=5), "T2": Behavior(steps=6)}
    b_blocked, release = asyncio.Event(), asyncio.Event()
    fact = multi_tool_response(
        (
            "post",
            {
                "kind": "fact",
                "title": "Obsidian Sync costs 4 USD a month",
                "body": "obsidian.md/pricing",
                "tags": ["pricing"],
            },
        ),
        ("run_command", {"cmd": "echo posted"}),
    )
    # A posts only once B's step-2 digest is built and B waits in its model call;
    # B's step 3 starts after the post, so its digest must carry it
    m.script("T1", 2, lambda _call: gate(b_blocked, fact))

    async def b_step2(_call: object) -> ModelResponse:
        b_blocked.set()
        return await gate(release, shell("waiting"))

    m.script("T2", 2, b_step2)
    await world.runtime()
    sid = await world.approved_session({"T1": [], "T2": []}, agent_count=2)

    posted = await world.wait_event(
        sid, "board.entry_posted", lambda e: payload(e, BoardEntryPosted).entry.kind == "fact"
    )
    entry = payload(posted, BoardEntryPosted).entry
    release.set()  # B's step 3 digest is built after the post
    await world.wait_event(sid, "session.completed", limit_s=20)

    b_calls = m.of("T2")
    a_name = m.of("T1")[0].agent
    assert b_calls[0].agent != a_name
    line = f"[#{entry.seq} fact · {a_name} · T1] Obsidian Sync costs 4 USD a month"
    assert line in b_calls[2].inbox
    assert sum(line in c.inbox for c in b_calls) == 1  # shown once, then the cursor moved
    assert all(line not in c.inbox for c in m.of("T1"))  # never to its own author
