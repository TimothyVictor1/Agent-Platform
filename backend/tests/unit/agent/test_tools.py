"""Desktop and control tools: coordinate mapping, sandbox calls, ERROR results,
submit_result validation, fail_task, request_approval and wait_for."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import pytest

from agentplatform.contracts.artifact import Approval
from agentplatform.contracts.board import BoardEntry
from agentplatform.contracts.common import (
    Actor,
    AgentStatus,
    ApprovalStatus,
    CoordinateMode,
    RiskLevel,
    utcnow,
)
from agentplatform.contracts.model_io import ToolCall
from agentplatform.contracts.sandbox import CommandResult
from agentplatform.contracts.task import OutputSpec, Task
from agentplatform.errors import NotFoundError
from agentplatform.runtime.agent.actions import prepare_observation
from agentplatform.runtime.agent.loop import (
    AgentState,
    ApprovalGate,
    ToolContext,
    build_agent_registry,
)
from agentplatform.runtime.agent.tools.control import validate_outputs
from agentplatform.runtime.agent.tools.desktop import DESKTOP_TOOL_NAMES
from agentplatform.runtime.agent.tools.registry import (
    MAX_RESULT_CHARS,
    TaskGaveUp,
    TaskSubmitted,
    ToolRegistry,
    ToolResult,
)
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.testing import (
    FakeSandbox,
    RecordedAction,
    make_agent,
    make_board_entry,
    make_png,
    make_task,
)

T = TypeVar("T")


@dataclass
class FakeHooks:
    statuses: list[AgentStatus] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    slept: list[float] = field(default_factory=list)
    stop: bool = False

    async def set_status(self, status: AgentStatus, reason: str | None = None) -> None:
        self.statuses.append(status)

    async def wait_or_stop(self, aw: Awaitable[T]) -> T | None:
        if self.stop:
            if hasattr(aw, "close"):
                aw.close()
            return None
        return await aw

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def add_note(self, text: str) -> None:
        self.notes.append(text)


CtxFactory = Callable[..., ToolContext]


@pytest.fixture
def registry() -> ToolRegistry:
    return build_agent_registry()


@pytest.fixture
def make_ctx() -> CtxFactory:
    def factory(
        *,
        sandbox: FakeSandbox | None = None,
        task: Task | None = None,
        mode: CoordinateMode = "absolute",
        target_width: int = 1280,
        approvals: ApprovalGate | None = None,
        board: BoardService | None = None,
        hooks: FakeHooks | None = None,
        **_: Any,
    ) -> ToolContext:
        sb = sandbox or FakeSandbox()
        png = make_png(*sb.screen_size, seed=1)
        return ToolContext(
            session_id="s-test",
            agent=make_agent(0),
            task=task or make_task(),
            step=5,
            state=AgentState(),
            sandbox=sb,
            observation=prepare_observation(png, target_width),
            screenshot_png=png,
            coordinate_mode=mode,
            hooks=hooks or FakeHooks(),
            frame_id="frame-1",
            board=board,
            approvals=approvals,
        )

    return factory


# == desktop tools ======================================================================


def call(name: str, **args: Any) -> ToolCall:
    return ToolCall(id=f"c-{name}", name=name, arguments=args)


async def run(registry: ToolRegistry, ctx: Any, name: str, **args: Any) -> ToolResult:
    return await registry.execute(call(name, **args), ctx)


def test_all_desktop_tools_registered(registry: ToolRegistry) -> None:
    expected = {"click", "move", "drag", "scroll", "type_text", "key", "run_command", "wait"}
    assert expected | {"zoom"} == DESKTOP_TOOL_NAMES
    for name in DESKTOP_TOOL_NAMES:
        assert registry.kind_of(name) == "desktop"


async def test_click_maps_downscaled_coordinates(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    ctx = make_ctx(sandbox=sb, target_width=640)  # observation is half size
    res = await run(registry, ctx, "click", x=100, y=50)
    assert not res.is_error
    assert res.text == "clicked left at (100, 50)"
    assert sb.actions == [
        RecordedAction("click", {"x": 200, "y": 100, "button": "left", "count": 1})
    ]


async def test_click_right_double(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    res = await run(registry, make_ctx(sandbox=sb), "click", x=10, y=20, button="right", count=2)
    assert "right x2" in res.text
    assert sb.actions[0].args["count"] == 2


async def test_click_negative_is_error_not_crash(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    res = await run(registry, make_ctx(sandbox=sb), "click", x=-6, y=20)
    assert res.is_error
    assert res.text.startswith("ERROR:")
    assert sb.actions == []


async def test_coordinates_are_clamped_to_screen(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    await run(registry, make_ctx(sandbox=sb), "move", x=1500, y=1500)
    assert sb.actions == [RecordedAction("move", {"x": 1279, "y": 799})]


async def test_drag_and_scroll(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    ctx = make_ctx(sandbox=sb)
    await run(registry, ctx, "drag", x1=1, y1=2, x2=30, y2=40)
    await run(registry, ctx, "scroll", x=100, y=100, direction="down")
    assert sb.action_names() == ["drag", "scroll"]
    assert sb.actions[1].args == {"x": 100, "y": 100, "direction": "down", "amount": 3}


async def test_type_text_newline_becomes_enter(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    res = await run(registry, make_ctx(sandbox=sb), "type_text", text="hello\nworld\n")
    assert not res.is_error
    assert sb.actions == [
        RecordedAction("type_text", {"text": "hello"}),
        RecordedAction("key", {"combo": "enter"}),
        RecordedAction("type_text", {"text": "world"}),
        RecordedAction("key", {"combo": "enter"}),
    ]


async def test_type_text_too_long_is_rejected(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    res = await run(registry, make_ctx(sandbox=sb), "type_text", text="x" * 2001)
    assert res.is_error
    assert "run_command" in res.text
    assert sb.actions == []


async def test_key_is_normalized(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    res = await run(registry, make_ctx(sandbox=sb), "key", combo="Ctrl + L")
    assert res.text == "pressed ctrl+l"
    assert sb.actions == [RecordedAction("key", {"combo": "ctrl+l"})]


async def test_empty_key_is_error(registry: ToolRegistry, make_ctx: Any) -> None:
    res = await run(registry, make_ctx(), "key", combo=" + ")
    assert res.is_error


async def test_run_command_formats_output(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    res = await run(registry, make_ctx(sandbox=sb), "run_command", cmd="echo hi")
    assert res.text == "exit 0\nstdout:\nhi\n"
    assert sb.actions == [RecordedAction("run_command", {"cmd": "echo hi", "timeout_s": 60})]


async def test_run_command_output_is_truncated(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox(command_results={"big": CommandResult(stdout="y" * 5000, exit_code=0)})
    res = await run(registry, make_ctx(sandbox=sb), "run_command", cmd="big")
    assert len(res.text) <= MAX_RESULT_CHARS
    assert "truncated" in res.text


async def test_run_command_timeout_is_error(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox(command_results={"sleep 99": CommandResult(exit_code=124, timed_out=True)})
    res = await run(registry, make_ctx(sandbox=sb), "run_command", cmd="sleep 99", timeout_s=5)
    assert res.is_error
    assert "timed out after 5s" in res.text


async def test_wait_uses_loop_sleep_and_is_capped(registry: ToolRegistry, make_ctx: Any) -> None:
    ctx = make_ctx()
    res = await run(registry, ctx, "wait", seconds=2)
    assert res.text == "waited 2 s"
    assert ctx.hooks.slept == [2.0]
    too_long = await run(registry, ctx, "wait", seconds=11)
    assert too_long.is_error
    assert ctx.hooks.slept == [2.0]


async def test_zoom_returns_image_without_touching_sandbox(
    registry: ToolRegistry, make_ctx: Any
) -> None:
    sb = FakeSandbox()
    res = await run(registry, make_ctx(sandbox=sb), "zoom", x=10, y=10, w=100, h=50)
    assert not res.is_error
    assert res.image is not None
    assert res.image[:2] == b"\xff\xd8"  # JPEG
    assert sb.actions == []


async def test_sandbox_error_becomes_error_result(registry: ToolRegistry, make_ctx: Any) -> None:
    sb = FakeSandbox()
    await sb.pause()
    res = await run(registry, make_ctx(sandbox=sb), "click", x=1, y=1)
    assert res.is_error
    assert "paused" in res.text


async def test_unimplemented_coordinate_mode_does_not_crash(
    registry: ToolRegistry, make_ctx: Any
) -> None:
    """normalized_1000 is completed by T03; until then the tool returns an ERROR."""
    res = await run(registry, make_ctx(mode="normalized_1000"), "click", x=500, y=500)
    assert isinstance(res, ToolResult)


# == control tools ======================================================================


PRICES_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"app": {"type": "string"}, "usd": {"type": "number"}},
        "required": ["app", "usd"],
    },
    "minItems": 1,
}

SPECS = [
    OutputSpec(key="summary_text", kind="text", description="short text"),
    OutputSpec(key="prices", kind="json", description="price list", json_schema=PRICES_SCHEMA),
    OutputSpec(key="sheet", kind="artifact", description="the spreadsheet"),
]


# -- validate_outputs -----------------------------------------------------------------


def test_valid_outputs() -> None:
    outputs = {"summary_text": "ok", "prices": [{"app": "Notion", "usd": 10}], "sheet": "a1"}
    assert validate_outputs(SPECS, outputs, ["a1"]) == []


def test_missing_and_unknown_outputs() -> None:
    problems = validate_outputs(SPECS, {"extra": 1}, [])
    assert any("missing output 'summary_text'" in p for p in problems)
    assert any("missing output 'prices'" in p for p in problems)
    assert any("missing output 'sheet'" in p for p in problems)
    assert any("unknown outputs ['extra']" in p for p in problems)


def test_kind_checks() -> None:
    outputs = {"summary_text": "  ", "prices": [{"app": "Notion"}], "sheet": "a9"}
    problems = validate_outputs(SPECS, outputs, ["a1"])
    assert any("'summary_text' must be a non-empty string" in p for p in problems)
    assert any("'prices' at 0" in p and "'usd' is a required property" in p for p in problems)
    assert any("artifact 'a9' is not in artifact_ids" in p for p in problems)


def test_invalid_task_schema_is_reported_not_raised() -> None:
    specs = [OutputSpec(key="data", kind="json", json_schema={"type": 12})]
    problems = validate_outputs(specs, {"data": {}}, [])
    assert problems
    assert "schema is invalid" in problems[0]


# -- submit_result / fail_task ----------------------------------------------------------


async def test_submit_result_success(registry: ToolRegistry, make_ctx: Any) -> None:
    ctx = make_ctx(task=make_task(outputs=["result"]))
    res = await run(registry, ctx, "submit_result", summary="done", outputs={"result": "42"})
    assert not res.is_error
    assert isinstance(res.outcome, TaskSubmitted)
    assert res.outcome.result.outputs == {"result": "42"}
    assert res.outcome.result.final_frame_id == "frame-1"
    assert res.outcome.result.confidence == "medium"


async def test_submit_result_validation_error_lists_problems(
    registry: ToolRegistry, make_ctx: Any
) -> None:
    ctx = make_ctx(task=make_task(outputs=SPECS))
    res = await run(registry, ctx, "submit_result", summary="done", outputs={"prices": []})
    assert res.is_error
    assert res.outcome is None
    assert res.text.startswith("ERROR: result not accepted:")
    assert "summary_text" in res.text
    assert "should be non-empty" in res.text


async def test_submit_result_requires_summary(registry: ToolRegistry, make_ctx: Any) -> None:
    res = await run(registry, make_ctx(), "submit_result", outputs={"result": "x"})
    assert res.is_error
    assert res.outcome is None


async def test_fail_task(registry: ToolRegistry, make_ctx: Any) -> None:
    res = await run(
        registry, make_ctx(), "fail_task", reason="site is down", what_was_tried="reloaded"
    )
    assert isinstance(res.outcome, TaskGaveUp)
    assert res.outcome.reason == "site is down"
    assert res.outcome.what_was_tried == "reloaded"


# -- request_approval -------------------------------------------------------------------


class FakeGate:
    def __init__(self, status: ApprovalStatus, note: str | None = None) -> None:
        self.status = status
        self.note = note
        self.requests: list[dict[str, Any]] = []

    async def request(
        self,
        *,
        session_id: str,
        agent_id: str,
        task_key: str | None,
        action_summary: str,
        risk: RiskLevel,
        frame_id: str | None,
    ) -> Approval:
        self.requests.append(
            {"task_key": task_key, "summary": action_summary, "risk": risk, "frame_id": frame_id}
        )
        return Approval(
            id="ap1",
            session_id=session_id,
            agent_id=agent_id,
            task_key=task_key,
            action_summary=action_summary,
            risk=risk,
            frame_id=frame_id,
            status=self.status,
            note=self.note,
            decided_by=Actor.user(),
            created_at=utcnow(),
            decided_at=utcnow(),
        )


@pytest.mark.parametrize(
    ("status", "note", "expected"),
    [
        ("approved", None, "Approved: submit the form"),
        ("denied", "copy the data instead", "Denied: submit the form. Do this instead: copy"),
        ("expired", None, "expired; continue without doing it"),
    ],
)
async def test_request_approval_decisions(
    registry: ToolRegistry,
    make_ctx: Any,
    status: ApprovalStatus,
    note: str | None,
    expected: str,
) -> None:
    gate = FakeGate(status, note)
    ctx = make_ctx(approvals=gate)
    res = await run(
        registry, ctx, "request_approval", action_summary="submit the form", risk="high"
    )
    assert expected in res.text
    assert ctx.hooks.statuses == ["awaiting_approval", "working"]
    assert ctx.hooks.notes == [res.text]
    assert gate.requests == [
        {"task_key": "T1", "summary": "submit the form", "risk": "high", "frame_id": "frame-1"}
    ]


async def test_request_approval_without_gate(registry: ToolRegistry, make_ctx: Any) -> None:
    res = await run(registry, make_ctx(), "request_approval", action_summary="buy", risk="high")
    assert res.is_error
    assert "do not perform" in res.text


async def test_request_approval_stopped(registry: ToolRegistry, make_ctx: Any) -> None:
    ctx = make_ctx(approvals=FakeGate("approved"))
    ctx.hooks.stop = True
    res = await run(registry, ctx, "request_approval", action_summary="buy", risk="high")
    assert "stopped" in res.text
    assert ctx.hooks.statuses == ["awaiting_approval", "working"]


# -- wait_for ---------------------------------------------------------------------------


class FakeBoard:
    def __init__(self, entries: list[BoardEntry], answer: BoardEntry | None) -> None:
        self.entries = {e.id: e for e in entries}
        self.answer = answer
        self.waits: list[tuple[str, float]] = []

    async def get(self, entry_id: str) -> BoardEntry:
        try:
            return self.entries[entry_id]
        except KeyError:
            raise NotFoundError("missing") from None

    async def wait_for_answer(self, question_id: str, timeout_s: float) -> BoardEntry | None:
        self.waits.append((question_id, timeout_s))
        return self.answer


async def test_wait_for_answer(registry: ToolRegistry, make_ctx: Any) -> None:
    q = make_board_entry("question", title="Which currency?")
    a = make_board_entry(
        "answer", seq=2, title="Use USD", body="per decision", author=Actor.supervisor()
    )
    board = FakeBoard([q], a)
    ctx = make_ctx(board=board)
    res = await run(registry, ctx, "wait_for", entry_id=q.id, timeout_s=30)
    assert res.text.startswith("Answer from Supervisor")
    assert "Use USD — per decision" in res.text
    assert board.waits == [(q.id, 30.0)]
    assert ctx.hooks.statuses == ["waiting", "working"]


async def test_wait_for_timeout(registry: ToolRegistry, make_ctx: Any) -> None:
    q = make_board_entry("question")
    res = await run(registry, make_ctx(board=FakeBoard([q], None)), "wait_for", entry_id=q.id)
    assert "no answer within 60 s" in res.text


async def test_wait_for_rejects_other_sessions_and_kinds(
    registry: ToolRegistry, make_ctx: Any
) -> None:
    foreign = make_board_entry("question", session_id="s-other")
    fact = make_board_entry("fact")
    board = FakeBoard([foreign, fact], None)
    ctx = make_ctx(board=board)
    r1 = await run(registry, ctx, "wait_for", entry_id=foreign.id)
    r2 = await run(registry, ctx, "wait_for", entry_id=fact.id)
    r3 = await run(registry, ctx, "wait_for", entry_id="nope")
    assert r1.is_error
    assert "no board entry" in r1.text
    assert r2.is_error
    assert "not a question" in r2.text
    assert r3.is_error
    assert board.waits == []


async def test_wait_for_without_board(registry: ToolRegistry, make_ctx: Any) -> None:
    res = await run(registry, make_ctx(), "wait_for", entry_id="x")
    assert res.is_error
    assert "board is not available" in res.text


async def test_wait_for_timeout_is_capped(registry: ToolRegistry, make_ctx: Any) -> None:
    q = make_board_entry("question")
    res = await run(
        registry, make_ctx(board=FakeBoard([q], None)), "wait_for", entry_id=q.id, timeout_s=500
    )
    assert res.is_error


# -- $ref resolution in task schemas (T06 review #1) ------------------------------------


@pytest.fixture
def no_io(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record (and refuse) any network or file access made during validation."""
    import builtins
    import socket
    import urllib.request

    attempts: list[str] = []
    real_open = builtins.open

    def refuse(kind: str) -> Callable[..., Any]:
        def _refuse(*args: Any, **kwargs: Any) -> Any:
            attempts.append(f"{kind}: {args[:1]!r}")
            raise AssertionError(f"unexpected {kind} access")

        return _refuse

    def guarded_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        attempts.append(f"open: {file!r}")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", refuse("urlopen"))
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", refuse("opener"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(builtins, "open", guarded_open)
    return attempts


@pytest.mark.parametrize(
    "ref",
    [
        "http://169.254.169.254/latest/meta-data/schema.json",
        "https://example.com/schema.json",
        "file:///etc/passwd",
        "other.json#/$defs/x",
    ],
)
def test_external_schema_refs_are_never_retrieved(ref: str, no_io: list[str]) -> None:
    schema = {"type": "object", "properties": {"a": {"$ref": ref}}}
    specs = [OutputSpec(key="data", kind="json", json_schema=schema)]
    problems = validate_outputs(specs, {"data": {"a": 1}}, [])
    assert no_io == []
    assert len(problems) == 1
    assert "cannot be resolved" in problems[0]
    assert "only local references" in problems[0]
    assert len(problems[0]) < 300


def test_long_unresolvable_ref_is_clipped(no_io: list[str]) -> None:
    ref = "https://example.com/" + "x" * 5000
    specs = [OutputSpec(key="data", kind="json", json_schema={"$ref": ref})]
    problems = validate_outputs(specs, {"data": 1}, [])
    assert no_io == []
    assert len(problems) == 1
    assert len(problems[0]) < 300


def test_local_schema_refs_resolve(no_io: list[str]) -> None:
    schema = {
        "$defs": {"price": {"type": "object", "required": ["usd"]}},
        "type": "array",
        "items": {"$ref": "#/$defs/price"},
    }
    specs = [OutputSpec(key="data", kind="json", json_schema=schema)]
    assert validate_outputs(specs, {"data": [{"usd": 1}]}, []) == []
    problems = validate_outputs(specs, {"data": [{}]}, [])
    assert no_io == []
    assert len(problems) == 1
    assert "'usd' is a required property" in problems[0]
