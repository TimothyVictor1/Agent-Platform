"""``AgentRuntime.add_note`` between tasks: kept for the first step of the next task."""

from __future__ import annotations

from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.loop import MAX_IDLE_NOTES, AgentRuntime, AgentRuntimeConfig
from agentplatform.testing import (
    FakeModel,
    FakeSandbox,
    InMemoryArtifactStore,
    InMemoryBus,
    InMemoryEventStore,
    make_agent,
    make_session,
    make_task,
    tool_response,
)


def _runtime(model: FakeModel) -> AgentRuntime:
    session = make_session(agent_count=1)
    store, bus = InMemoryEventStore(), InMemoryBus()
    return AgentRuntime(
        session=session,
        agent=make_agent(0, session_id=session.id),
        sandbox=FakeSandbox(),
        model=model,
        events=EventRecorder(store, bus),
        bus=bus,
        frames=InMemoryArtifactStore(),
        config=AgentRuntimeConfig(error_backoff_s=0.0),
    )


def _last_user(model: FakeModel, call: int) -> str:
    users = [m for m in model.calls[call].request.messages if m.role == "user"]
    return users[-1].text_content()


async def test_note_added_while_idle_reaches_the_next_task_once() -> None:
    model = FakeModel(default=tool_response("fail_task", reason="done"))
    runtime = _runtime(model)
    runtime.add_note("Message from the user: use EUR prices.")
    await runtime.run_task(make_task())
    assert "[system note] Message from the user: use EUR prices." in _last_user(model, 0)
    await runtime.run_task(make_task())
    assert "use EUR prices" not in _last_user(model, 1)


async def test_idle_notes_are_capped() -> None:
    model = FakeModel(default=tool_response("fail_task", reason="done"))
    runtime = _runtime(model)
    for i in range(MAX_IDLE_NOTES + 3):
        runtime.add_note(f"note {i}")
    await runtime.run_task(make_task())
    text = _last_user(model, 0)
    assert "note 0\n" not in text
    assert "note 2\n" not in text
    assert f"note {MAX_IDLE_NOTES + 2}" in text
    assert text.count("[system note]") == MAX_IDLE_NOTES
