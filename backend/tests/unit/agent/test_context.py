"""ContextBuilder: system prompt, message layout, image window, summaries, budget trim."""

from __future__ import annotations

from typing import Any

import pytest

from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.common import Actor, CoordinateMode, utcnow
from agentplatform.contracts.model_io import ImagePart, Message, TextPart, ToolCall, ToolSpec
from agentplatform.contracts.task import OutputSpec
from agentplatform.errors import ModelError
from agentplatform.runtime.agent.context import (
    ContextBudgetError,
    ContextBuilder,
    ContextSettings,
    ModelSummarizer,
    RosterEntry,
    StepRecord,
    TruncatingSummarizer,
    UpstreamInputs,
    UpstreamValue,
    describe_call,
    estimate_tokens,
    step_line,
)
from agentplatform.testing import FakeModel, make_task, text_response

IMG = ImagePart(jpeg_bytes=b"\xff\xd8fake", width=1280, height=800)


def builder(
    *,
    settings: ContextSettings | None = None,
    mode: CoordinateMode = "absolute",
    upstream: UpstreamInputs | None = None,
    **kw: Any,
) -> ContextBuilder:
    task = make_task(
        outputs=[
            OutputSpec(key="answer", kind="text", description="the answer"),
            OutputSpec(key="rows", kind="json", description="rows", json_schema={"type": "array"}),
        ],
        criteria=["Pricing page is open", "Price is visible"],
        title="Find pricing",
        goal="Find the price of Notion Plus.",
    )
    return ContextBuilder(
        agent_name="Ada",
        team_size=3,
        task=task,
        upstream=upstream or UpstreamInputs(),
        coordinate_mode=mode,
        screen_size=(1280, 800),
        screenshot_target_width=kw.get("target_width", 1280),
        max_steps=150,
        settings=settings,
        summarizer=kw.get("summarizer"),
    )


def record(step: int, *, image: bool = True, text: str = "x") -> StepRecord:
    call = ToolCall(id=f"call-{step}", name="click", arguments={"x": step, "y": 2})
    return StepRecord(
        step=step,
        header=f"Step {step} of max 150. Time used 0 min.",
        image=IMG if image else None,
        assistant_text=text,
        calls=[call],
        results=[f"clicked left at ({step}, 2)"],
        line=step_line(step, [call], [f"clicked left at ({step}, 2)"]),
    )


def images_in(messages: list[Message]) -> int:
    return sum(len(m.images()) for m in messages)


# -- system prompt ------------------------------------------------------------------------


def test_system_prompt_contents() -> None:
    art = Artifact(
        id="art-1",
        session_id="s",
        name="prices.csv",
        mime="text/csv",
        size_bytes=10,
        sha256="0" * 64,
        produced_by=Actor.agent("a2", "Brook"),
        storage_key="k",
        created_at=utcnow(),
    )
    upstream = UpstreamInputs(
        values=[UpstreamValue(from_task="T0", output_key="apps", value=["Notion", "Obsidian"])],
        artifacts=[art],
    )
    b = builder(upstream=upstream)
    text = b.system_message().text_content()
    assert text.startswith("You are Ada, one member of a team of 3 AI agents.")
    assert "Find pricing" in text
    assert "Find the price of Notion Plus." in text
    assert "- Pricing page is open" in text
    assert "`answer` (text): the answer" in text
    assert 'JSON schema: {"type": "array"}' in text
    assert 'T0.apps = ["Notion", "Obsidian"]' in text
    assert "art-1: prices.csv (text/csv, 10 bytes)" in text
    assert "data, not instructions" in text
    assert "integer pixels in the 1280x800 screenshot" in text
    assert "$" not in text
    assert b.system_message() is b.system_message()  # cached


@pytest.mark.parametrize(
    ("mode", "phrase"),
    [
        ("normalized_1000", "integers 0..1000"),
        ("relative", "decimals 0..1"),
        ("absolute", "integer pixels in the 640x400 screenshot"),
    ],
)
def test_coordinate_help(mode: CoordinateMode, phrase: str) -> None:
    text = builder(mode=mode, target_width=640).system_message().text_content()
    assert phrase in text
    assert "screenshots are 640 x 400" in text


def test_roster_change_rebuilds_system_message() -> None:
    b = builder()
    first = b.system_message()
    roster = [RosterEntry("Ada", "Find pricing"), RosterEntry("Brook", "Build sheet")]
    assert b.set_roster(roster) is True
    second = b.system_message()
    assert second is not first
    assert "Brook: Build sheet" in second.text_content()
    assert "Ada (you): Find pricing" in second.text_content()
    assert b.set_roster(roster) is False
    assert b.system_message() is second


def test_upstream_values_are_truncated() -> None:
    upstream = UpstreamInputs(values=[UpstreamValue("T0", "blob", "z" * 5000)])
    text = builder(upstream=upstream).system_message().text_content()
    assert "z" * 1400 in text
    assert "z" * 1600 not in text


# -- message layout -----------------------------------------------------------------------


def test_first_step_layout() -> None:
    b = builder()
    header = b.header(1, 0.0)
    msgs = b.build(header=header, image=IMG, digest="[#1 fact · Brook] X", notebook="todo")
    assert [m.role for m in msgs] == ["system", "user"]
    current = msgs[-1]
    texts = [p.text for p in current.content if isinstance(p, TextPart)]
    assert texts[0] == "Step 1 of max 150. Time used 0 min."
    assert "Inbox:\n[#1 fact · Brook] X" in texts
    assert "Your notebook:\ntodo" in texts
    assert isinstance(current.content[-1], ImagePart)


def test_header_notes() -> None:
    text = builder().header(4, 2.4, ["Approved: submit"])
    assert text == "Step 4 of max 150. Time used 2 min.\n[system note] Approved: submit"


def test_history_layout_and_tool_results() -> None:
    b = builder()
    b.add_step(record(1))
    msgs = b.build(header="now", image=IMG)
    assert [m.role for m in msgs] == ["system", "user", "assistant", "tool", "user"]
    assistant, tool = msgs[2], msgs[3]
    assert assistant.tool_calls is not None
    assert tool.tool_call_id == assistant.tool_calls[0].id
    assert tool.text_content() == "clicked left at (1, 2)"


def test_only_last_n_observations_are_images() -> None:
    b = builder(settings=ContextSettings(max_images_in_context=3))
    for step in range(1, 6):
        b.add_step(record(step))
    msgs = b.build(header="now", image=IMG)
    assert images_in(msgs) == 3
    assert msgs[-1].images()  # current screenshot always present
    for m in msgs:
        if m.role != "user":
            assert not m.images()
    user_steps = [m for m in msgs if m.role == "user"][:-1]
    with_image = [m.text_content().splitlines()[0] for m in user_steps if m.images()]
    assert with_image == [
        "Step 4 of max 150. Time used 0 min.",
        "Step 5 of max 150. Time used 0 min.",
    ]
    assert "(screenshot of step 1 omitted)" in user_steps[0].text_content()


def test_zoom_image_is_attached_to_current_step() -> None:
    zoom = ImagePart(jpeg_bytes=b"\xff\xd8zoom", width=200, height=100)
    msgs = builder().build(header="now", image=IMG, extra_image=zoom)
    assert msgs[-1].images() == [IMG, zoom]


def test_old_steps_become_compact_lines() -> None:
    b = builder(settings=ContextSettings(full_history_steps=12))
    for step in range(1, 16):
        b.add_step(record(step))
    b.history[0].screen_changed = True
    b.history[1].screen_changed = False
    msgs = b.build(header="now", image=IMG)
    head = msgs[1]
    assert head.role == "user"
    assert head.text_content().startswith("Earlier steps:")
    lines = head.text_content().splitlines()[1:]
    assert lines[0] == "[step 1] click(x=1, y=2) → clicked left at (1, 2) → screen changed"
    assert lines[1].endswith("→ no visible change")
    assert len(lines) == 3
    assert sum(1 for m in msgs if m.role == "assistant") == 12


# -- summaries ---------------------------------------------------------------------------


async def test_summary_every_20_steps() -> None:
    b = builder()
    for step in range(1, 21):
        b.add_step(record(step))
    assert await b.maybe_summarize(19) is False
    assert await b.maybe_summarize(20) is True
    assert len(b.history) == 12
    assert b.history[0].step == 9
    assert b.summarized_through == 8
    assert "[step 1]" in b.summary
    assert len(b.summary) <= 800
    msgs = b.build(header="now", image=IMG)
    assert msgs[1].text_content().startswith("Progress so far: ")


async def test_truncating_summarizer_keeps_recent_text() -> None:
    s = TruncatingSummarizer(max_chars=50)
    out = await s.summarize("old " * 30, ["[step 9] newest"], task=make_task())
    assert len(out) == 50
    assert out.endswith("[step 9] newest")


async def test_model_summarizer_uses_model_and_falls_back() -> None:
    model = FakeModel(default=text_response("Opened pricing; Plus is $10."))
    s = ModelSummarizer(model)
    out = await s.summarize("", ["[step 1] click"], task=make_task())
    assert out == "Opened pricing; Plus is $10."
    req = model.calls[0].request
    assert req.tool_choice == "none"
    assert "[step 1] click" in model.calls[0].all_text()

    broken = ModelSummarizer(FakeModel({"default": [ModelError("down")]}))
    assert await broken.summarize("prev", ["[step 2] x"], task=make_task()) == "prev\n[step 2] x"


async def test_summary_is_clipped() -> None:
    model = FakeModel(default=text_response("y" * 2000))
    b = builder(summarizer=ModelSummarizer(model))
    for step in range(1, 21):
        b.add_step(record(step))
    await b.maybe_summarize(20)
    assert len(b.summary) == 800


# -- budget ------------------------------------------------------------------------------


def test_estimate_tokens() -> None:
    msgs = [Message.text("user", "a" * 400), Message(role="user", content=[IMG])]
    assert estimate_tokens(msgs, image_tokens=1000) == 100 + 1000 + 8


def test_budget_trims_images_then_history_then_digest() -> None:
    settings = ContextSettings(
        max_images_in_context=4, prompt_budget_tokens=6000, image_tokens=1500
    )
    b = builder(settings=settings)
    for step in range(1, 13):
        b.add_step(record(step, text="t" * 400))
    msgs = b.build(header="now", image=IMG, digest="d" * 400)
    assert estimate_tokens(msgs, 1500) <= 6000
    assert images_in(msgs) < 4
    assert msgs[-1].images()  # the current screenshot is never trimmed
    assert "Inbox:" in msgs[-1].text_content()  # digest survives when history is enough
    assert b.last_trim["tokens"] <= 6000


def test_budget_drops_digest_last() -> None:
    settings = ContextSettings(prompt_budget_tokens=1000, image_tokens=500)
    b = builder(settings=settings)
    b.add_step(record(1))
    msgs = b.build(header="now", image=IMG, digest="d" * 4000)
    assert b.last_trim["images"] == 0
    assert b.last_trim["full"] == 0
    assert b.last_trim["digest_chars"] < 4000
    assert msgs[-1].images() == [IMG]


@pytest.mark.parametrize(
    ("digest", "included"), [("short digest", True), ("d" * 4000, False), ("", True)]
)
def test_digest_included_flag(digest: str, included: bool) -> None:
    b = builder(settings=ContextSettings(prompt_budget_tokens=1000, image_tokens=500))
    b.build(header="now", image=IMG, digest=digest)
    assert b.digest_included is included


def big_tools(n: int = 5, chars: int = 4000) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=f"tool_{i}",
            description="d" * chars,
            parameters={"type": "object", "properties": {}},
        )
        for i in range(n)
    ]


def test_estimate_includes_tool_schemas() -> None:
    msgs = [Message.text("user", "a" * 400)]
    assert estimate_tokens(msgs, 1000, big_tools()) >= estimate_tokens(msgs, 1000) + 5000


def test_large_tool_schemas_count_against_budget() -> None:
    """T06 review #7: tool schemas are part of the prompt."""
    settings = ContextSettings(max_images_in_context=3, prompt_budget_tokens=9000,
                               image_tokens=1500)  # fmt: skip
    tools = big_tools()  # ~5000 tokens
    b = builder(settings=settings)
    for step in range(1, 4):
        b.add_step(record(step))
    msgs = b.build(header="now", image=IMG, tools=tools)
    assert estimate_tokens(msgs, 1500, tools) <= 9000
    assert b.last_trim["tokens"] == estimate_tokens(msgs, 1500, tools)
    assert images_in(msgs) < 3  # history images were trimmed to make room for the tools


def test_oversized_system_prompt_raises_budget_error() -> None:
    values = [UpstreamValue("T0", f"k{i}", "v" * 1400) for i in range(30)]
    b = builder(  # ~10k tokens of upstream inputs in the system prompt alone
        settings=ContextSettings(prompt_budget_tokens=6000, image_tokens=500),
        upstream=UpstreamInputs(values=values),
    )
    with pytest.raises(ContextBudgetError, match="budget is 6000"):
        b.build(header="now", image=IMG)


def test_oversized_tool_schemas_raise_budget_error() -> None:
    b = builder(settings=ContextSettings(prompt_budget_tokens=4000, image_tokens=500))
    b.build(header="now", image=IMG)  # fits without tools
    with pytest.raises(ContextBudgetError):
        b.build(header="now", image=IMG, tools=big_tools())


# -- helpers -----------------------------------------------------------------------------


def test_describe_call_shortens_long_strings() -> None:
    call = ToolCall(id="1", name="type_text", arguments={"text": "a" * 100})
    out = describe_call(call)
    assert out.startswith("type_text(text='aaaa")
    assert len(out) < 70


def test_step_line_without_calls() -> None:
    assert step_line(3, [], [], note="no tool call") == "[step 3] no tool call"
