"""``extra_rules``: optional rules (e.g. session policies, T18) rendered under ``## Rules``."""

from __future__ import annotations

from string import Template

from agentplatform.contracts.model_io import ModelRequest
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.context import ContextBuilder, UpstreamInputs
from agentplatform.runtime.agent.loop import AgentRuntime, AgentRuntimeConfig
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
from agentplatform.testing.images import solid_png

EXTRA = "- Approval required: sending emails or messages\n- Forbidden: entering passwords"


def _builder(extra: str = "", template: Template | None = None) -> ContextBuilder:
    return ContextBuilder(
        agent_name="Ada",
        team_size=1,
        task=make_task(),
        upstream=UpstreamInputs(),
        coordinate_mode="absolute",
        screen_size=(1280, 800),
        screenshot_target_width=1280,
        max_steps=10,
        template=template,
        extra_rules=extra,
    )


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end < 0 else text[start:end]


def test_default_is_unchanged() -> None:
    assert _builder().system_message().text_content() == (
        _builder("   ").system_message().text_content()
    )


def test_extra_rules_render_at_the_end_of_the_rules_section() -> None:
    text = _builder(EXTRA).system_message().text_content()
    rules = _section(text, "## Rules")
    assert rules.rstrip().endswith(EXTRA)
    assert "Exactly one desktop action" in rules
    assert text.count(EXTRA) == 1
    assert "## Screen" in text.split(EXTRA, 1)[1]


def test_template_without_rules_section_gets_one() -> None:
    text = _builder(EXTRA, Template("You are $agent_name.")).system_message().text_content()
    assert text == f"You are Ada.\n\n## Rules\n{EXTRA}"


def test_rules_section_last_in_template() -> None:
    text = _builder(EXTRA, Template("Hi $agent_name\n\n## Rules\n- one")).system_message()
    assert text.text_content() == f"Hi Ada\n\n## Rules\n- one\n{EXTRA}"


async def test_agent_runtime_passes_extra_rules_to_the_system_prompt() -> None:
    session = make_session(agent_count=1)
    agent = make_agent(0, session_id=session.id)
    store, bus = InMemoryEventStore(), InMemoryBus()
    model = FakeModel(default=tool_response("fail_task", reason="done"))
    runtime = AgentRuntime(
        session=session,
        agent=agent,
        sandbox=FakeSandbox([solid_png(color=(10, 20, 30))]),
        model=model,
        events=EventRecorder(store, bus),
        bus=bus,
        frames=InMemoryArtifactStore(),
        config=AgentRuntimeConfig(error_backoff_s=0.0),
        extra_rules=EXTRA,
    )
    await runtime.run_task(make_task(session_id=session.id))
    request: ModelRequest = model.calls[0].request
    system = request.messages[0]
    assert system.role == "system"
    assert EXTRA in _section(system.text_content(), "## Rules")
