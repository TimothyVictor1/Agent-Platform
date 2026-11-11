"""Scenario 12 (SPEC §21): in ``parallel_isolated`` mode digests are always empty and
the coordination (and artifact) tools are absent, even when the board has entries."""

from __future__ import annotations

from runtime.support import Behavior

from agentplatform.contracts.board import BoardEntryDraft
from agentplatform.contracts.common import Actor
from agentplatform.testing import make_tasks
from scenarios.harness import ScenarioWorld, multi_tool_response

COORDINATION = {"read_board", "post", "answer", "release_claim", "message", "notebook_write"}
ARTIFACTS = {"publish_artifact", "fetch_artifact"}


async def test_isolated_agents_get_no_digest_and_no_coordination_tools(
    world: ScenarioWorld,
) -> None:
    m = world.agent_model
    m.behaviors = {"T1": Behavior(steps=5), "T2": Behavior(steps=5)}
    m.script(
        "T2",
        2,
        multi_tool_response(
            ("post", {"kind": "fact", "title": "smuggled"}),
            ("run_command", {"cmd": "echo hi"}),
        ),
    )
    await world.runtime()
    world.models.planner.plans = [make_tasks({"T1": [], "T2": []})]
    session = await world.create_session(agent_count=2, mode="parallel_isolated")
    sid = session.id
    await world.board.post(
        sid, BoardEntryDraft(kind="note", title="Everyone: use EUR"), author=Actor.user()
    )
    await world.send("command.plan_session", sid)
    await world.wait_event(sid, "session.plan_proposed")
    await world.send("command.approve_plan", sid, plan_version=1)
    await world.wait_event(sid, "session.completed", limit_s=20)

    calls = m.of("T1") + m.of("T2")
    assert len({c.agent for c in calls}) == 2
    for call in calls:
        assert call.inbox == ""
        assert "Inbox:" not in call.user
        assert not call.tools & (COORDINATION | ARTIFACTS), call.tools
        assert "Use EUR" not in call.user
    # the smuggled post was refused as an unknown tool and nothing reached the board
    assert any(r.startswith("ERROR") for r in m.of("T2")[2].tool_results)
    posted = [e for e in world.events(sid, "board.entry_posted") if e.actor.type == "agent"]
    assert posted == []
