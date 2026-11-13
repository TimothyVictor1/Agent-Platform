"""AgentRuntime step loop end to end with FakeModel + FakeSandbox (SPEC §10, T06).

The harness wires FakeModel + FakeSandbox + in-memory event store, bus and artifact
store. No network, no Docker, no real model.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import pytest

from agentplatform.contracts.agent import Agent
from agentplatform.contracts.artifact import Approval
from agentplatform.contracts.common import Actor, ApprovalStatus, RiskLevel, utcnow
from agentplatform.contracts.events import AgentStatusChanged, Event, Heartbeat, Thumbnail
from agentplatform.contracts.model_io import ModelRequest, ModelResponse, ToolCall, ToolSpec
from agentplatform.contracts.sandbox import CommandResult
from agentplatform.contracts.session import Budgets, Session
from agentplatform.contracts.task import Task
from agentplatform.errors import ModelError
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.context import (
    RESUME_NOTE,
    ContextSettings,
    UpstreamInputs,
    UpstreamValue,
)
from agentplatform.runtime.agent.loop import (
    NO_TOOL_CORRECTION,
    AgentRuntime,
    AgentRuntimeConfig,
    Digest,
    LocalControlState,
    TaskOutcome,
    build_agent_registry,
    reasoning_excerpt,
)
from agentplatform.runtime.agent.tools.registry import ToolRegistry, ToolResult
from agentplatform.testing import (
    FakeModel,
    FakeSandbox,
    InMemoryArtifactStore,
    InMemoryBus,
    InMemoryEventStore,
    assert_has_image,
    make_agent,
    make_png,
    make_session,
    make_task,
    multi_tool_response,
    text_response,
    tool_response,
)
from agentplatform.testing.fake_model import image_count

# -- harness ------------------------------------------------------------------------------


async def fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


@lru_cache(maxsize=1)
def _screens(n: int) -> tuple[bytes, ...]:
    return tuple(make_png(seed=i + 1) for i in range(n))


def distinct_screens(n: int = 30) -> list[bytes]:
    """Screens that all differ, so the screen-unchanged signal stays quiet (cached:
    rendering full-size PNGs dominates test time)."""
    return list(_screens(n))


@dataclass
class CoordLog:
    """Fake coordination tools (T14 owns the real ones) that record their calls."""

    calls: list[tuple[str, int]] = field(default_factory=list)


def add_fake_coordination(registry: ToolRegistry, sandbox: FakeSandbox, log: CoordLog) -> None:
    def make(name: str) -> Any:
        async def handler(args: dict[str, Any], ctx: Any) -> ToolResult:
            log.calls.append((name, len(sandbox.actions)))
            return ToolResult(text=f"{name} ok")

        return handler

    for name in ("post", "notebook_write", "read_board"):
        registry.register(
            "coordination",
            ToolSpec(
                name=name,
                description=name,
                parameters={"type": "object", "properties": {}, "additionalProperties": True},
            ),
            make(name),
            kind="coordination",
        )


@dataclass
class Harness:
    session: Session
    agent: Agent
    task: Task
    sandbox: FakeSandbox
    model: FakeModel
    store: InMemoryEventStore
    bus: InMemoryBus
    frames: InMemoryArtifactStore
    control: LocalControlState
    runtime: AgentRuntime
    coord: CoordLog

    async def run(self, upstream: UpstreamInputs | None = None, limit_s: float = 10) -> TaskOutcome:
        async with asyncio.timeout(limit_s):
            return await self.runtime.run_task(self.task, upstream)

    def events(self, type_: str | None = None) -> list[Event]:
        evs = self.store.all(self.session.id)
        return [e for e in evs if type_ is None or e.type == type_]

    def steps(self) -> list[Any]:
        return [e.payload for e in self.events("agent.step")]

    def statuses(self) -> list[str]:
        return [e.payload.status for e in self.events("agent.status_changed")]  # type: ignore[union-attr]


HarnessFactory = Callable[..., Harness]


@pytest.fixture
def harness() -> HarnessFactory:
    def factory(
        script: Any = None,
        *,
        default: ModelResponse | None = None,
        screens: list[bytes] | None = None,
        task: Task | None = None,
        budgets: Budgets | None = None,
        mode: Any = "coordinated",
        context: ContextSettings | None = None,
        latency_s: float = 0.0,
        sandbox: FakeSandbox | None = None,
        **kwargs: Any,
    ) -> Harness:
        session = make_session(mode=mode, budgets=budgets or Budgets(), agent_count=2)
        agent = make_agent(0, session_id=session.id)
        sb = sandbox or FakeSandbox(screens if screens is not None else distinct_screens())
        model = FakeModel({"Ada": script} if script is not None else None, default=default,
                          latency_s=latency_s)  # fmt: skip
        store = InMemoryEventStore()
        bus = InMemoryBus()
        frames = InMemoryArtifactStore()
        control = LocalControlState()
        registry = build_agent_registry()
        coord = CoordLog()
        add_fake_coordination(registry, sb, coord)
        config_overrides = kwargs.pop("config", {})
        config = AgentRuntimeConfig(
            error_backoff_s=0.0,
            heartbeat_interval_s=0.02,
            context=context or ContextSettings(),
            **config_overrides,
        )
        runtime = AgentRuntime(
            session=session,
            agent=agent,
            sandbox=sb,
            model=model,
            events=EventRecorder(store, bus),
            bus=bus,
            frames=frames,
            registry=registry,
            config=config,
            control=control,
            sleep=fast_sleep,
            **kwargs,
        )
        return Harness(
            session=session,
            agent=agent,
            task=task or make_task(session_id=session.id),
            sandbox=sb,
            model=model,
            store=store,
            bus=bus,
            frames=frames,
            control=control,
            runtime=runtime,
            coord=coord,
        )

    return factory


# -- script helpers ---------------------------------------------------------------------


def click(n: int) -> ModelResponse:
    """A click at a position that differs per ``n`` (no repetition signal)."""
    return tool_response("click", text=f"clicking {n}", x=20 + 37 * n, y=40 + 11 * n)


def clicks(n: int, start: int = 1) -> list[ModelResponse]:
    return [click(i) for i in range(start, start + n)]


def submit(**outputs: Any) -> ModelResponse:
    return tool_response("submit_result", summary="done", outputs=outputs or {"result": "42"})


def give_up() -> ModelResponse:
    return tool_response("fail_task", reason="test over")


def tool_texts(req: ModelRequest) -> list[str]:
    return [m.text_content() for m in req.messages if m.role == "tool"]


def last_user_text(req: ModelRequest) -> str:
    return [m for m in req.messages if m.role == "user"][-1].text_content()


# -- happy path and phase gating ----------------------------------------------------------


async def test_happy_path_submits_result(harness: Any) -> None:
    h = harness([*clicks(3), submit(result="42")])
    out = await h.run(limit_s=30)

    assert out.status == "submitted"
    assert out.result is not None
    assert out.result.outputs == {"result": "42"}
    assert out.steps == 4
    assert out.usage.steps == 4
    assert out.usage.input_tokens == 400

    steps = h.steps()
    assert [s.step for s in steps] == [1, 2, 3, 4]
    assert [s.action.tool for s in steps] == ["click", "click", "click", "submit_result"]
    assert steps[0].reasoning_excerpt == "clicking 1"
    assert out.result.final_frame_id == steps[3].frame.id
    assert h.sandbox.action_names() == ["click", "click", "click"]

    # frames saved through the ArtifactStore, metadata in agent.step, thumbnails on the bus
    frames = await h.frames.list("frames/")
    assert len(frames) == 4
    assert {s.frame.storage_key_small for s in steps} == {f.key for f in frames}
    assert steps[1].frame.action_label.startswith("click(")
    thumbs = [m for m in h.bus.ephemeral if isinstance(m, Thumbnail)]
    assert len(thumbs) == 4
    assert any(isinstance(m, Heartbeat) for m in h.bus.ephemeral)

    # submit_result hidden for the first 3 steps
    offered = [[t.name for t in c.request.tools] for c in h.model.calls]
    assert all("submit_result" not in names for names in offered[:3])
    assert "submit_result" in offered[3]
    assert all(c.request.tool_choice == "required" for c in h.model.calls)

    # the loop emits only agent.* events, correlated with the task
    assert h.statuses() == ["working", "idle"]
    assert all(e.type.startswith("agent.") for e in h.events())
    assert all(e.correlation_id == h.task.id for e in h.events("agent.step"))
    assert h.runtime.state.total_steps == 4


async def test_submit_before_step_4_is_rejected(harness: Any) -> None:
    h = harness([submit(), *clicks(3), submit()])
    out = await h.run()
    assert out.status == "submitted"
    assert out.steps == 5
    first = h.steps()[0].action
    assert first.is_error
    assert "unknown or unavailable tool 'submit_result'" in first.result


async def test_output_validation_error_then_fix(harness: Any) -> None:
    h = harness([*clicks(3), submit(wrong=1), submit(result="fixed")])
    out = await h.run()
    assert out.status == "submitted"
    assert out.result is not None
    assert out.result.outputs == {"result": "fixed"}
    fifth = h.model.calls[4].request
    feedback = tool_texts(fifth)[-1]
    assert "result not accepted" in feedback
    assert "missing output 'result'" in feedback
    assert "unknown outputs ['wrong']" in feedback


async def test_upstream_inputs_reach_the_system_prompt(harness: Any) -> None:
    h = harness([give_up()])
    await h.run(UpstreamInputs(values=[UpstreamValue("T0", "apps", ["Notion"])]))
    system = h.model.calls[0].request.messages[0]
    assert system.role == "system"
    assert 'T0.apps = ["Notion"]' in system.text_content()


# -- context: images and history ----------------------------------------------------------


async def test_image_every_step_and_only_last_n(harness: Any) -> None:
    h = harness([*clicks(5), submit()], context=ContextSettings(max_images_in_context=3))
    await h.run()
    for i, call in enumerate(h.model.calls, start=1):
        img = assert_has_image(call.request)
        assert (img.width, img.height) == (1280, 800)
        assert image_count(call.request) == min(i, 3)


async def test_history_summary_kicks_in(harness: Any) -> None:
    h = harness([*clicks(21), give_up()])
    await h.run()
    before = h.model.calls[19].request  # step 20
    after = h.model.calls[20].request  # step 21: steps 1..9 summarized after step 20
    assert "Progress so far" not in before.messages[1].text_content()
    assert before.messages[1].text_content().startswith("Earlier steps:")
    assert after.messages[1].text_content().startswith("Progress so far: ")
    assert "[step 1]" in after.messages[1].text_content()
    assert sum(1 for m in after.messages if m.role == "assistant") == 12


async def test_zoom_image_attached_to_next_observation(harness: Any) -> None:
    h = harness([tool_response("zoom", x=0, y=0, w=200, h=100), click(1), give_up()])
    await h.run()
    second = h.model.calls[1].request
    current = [m for m in second.messages if m.role == "user"][-1]
    assert len(current.images()) == 2
    assert "Zoomed region" in current.text_content()
    third = h.model.calls[2].request
    assert len([m for m in third.messages if m.role == "user"][-1].images()) == 1


# -- step composition rules ---------------------------------------------------------------


async def test_two_coordination_calls_then_desktop_in_order(harness: Any) -> None:
    h = harness(
        [
            multi_tool_response(
                ("post", {"title": "x"}), ("notebook_write", {}), ("click", {"x": 5, "y": 6})
            ),
            give_up(),
        ]
    )
    await h.run()
    # both coordination calls ran before the desktop action, in order
    assert h.coord.calls == [("post", 0), ("notebook_write", 0)]
    assert h.sandbox.action_names() == ["click"]
    step = h.steps()[0]
    assert [r.tool for r in step.coordination_calls] == ["post", "notebook_write"]
    assert step.action.tool == "click"
    assert not step.action.is_error


@pytest.mark.parametrize(
    ("calls", "phrase"),
    [
        ([("click", {"x": 1, "y": 1}), ("post", {})], "click must be the last tool call"),
        ([("click", {"x": 1, "y": 1}), ("key", {"combo": "enter"})], "2 desktop/control calls"),
        (
            [("post", {}), ("post", {}), ("read_board", {}), ("click", {"x": 1, "y": 1})],
            "3 coordination calls",
        ),
    ],
)
async def test_bad_step_composition_executes_nothing(
    harness: Any, calls: list[tuple[str, dict[str, Any]]], phrase: str
) -> None:
    h = harness([multi_tool_response(*calls), give_up()])
    await h.run()
    assert h.sandbox.actions == []
    assert h.coord.calls == []
    results = tool_texts(h.model.calls[1].request)
    assert len(results) == len(calls)
    assert all(r.startswith("ERROR: not executed:") and phrase in r for r in results)
    step = h.steps()[0]
    assert step.action is not None
    assert step.action.is_error
    assert all(r.is_error for r in step.coordination_calls)


@pytest.mark.parametrize(
    "calls",
    [
        [("post", {"title": "x"})],
        [("post", {"title": "x"}), ("notebook_write", {})],
        [("post", {"title": "x"}), ("teleport", {})],
    ],
)
async def test_coordination_only_step_is_rejected(
    harness: Any, calls: list[tuple[str, dict[str, Any]]]
) -> None:
    """T06 review #8: coordination calls without a desktop/control call run nothing."""
    h = harness([multi_tool_response(*calls), give_up()])
    await h.run()
    assert h.coord.calls == []
    assert h.sandbox.actions == []
    results = tool_texts(h.model.calls[1].request)
    assert len(results) == len(calls)
    assert all(
        r.startswith("ERROR: not executed:") and "no desktop or control call" in r for r in results
    )


async def test_tool_errors_do_not_crash_the_loop(harness: Any) -> None:
    def handler(cmd: str) -> CommandResult:
        raise RuntimeError("control server exploded")

    sb = FakeSandbox([make_png(seed=i) for i in range(1, 20)], command_handler=handler)
    h = harness(
        [
            tool_response("click", x=-6, y=10),
            tool_response("run_command", cmd="boom"),
            tool_response("click", x="left", y=1),
            tool_response("teleport", to="moon"),
            submit(),
        ],
        sandbox=sb,
    )
    out = await h.run()
    assert out.status == "submitted"
    actions = [s.action for s in h.steps()]
    assert all(a.is_error for a in actions[:4])
    assert "negative" in actions[0].result
    assert "failed unexpectedly (RuntimeError)" in actions[1].result
    assert "control server exploded" not in actions[1].result  # no internals leak
    assert "invalid arguments for click" in actions[2].result
    assert "Valid tools:" in actions[3].result
    assert all(len(a.result) <= 600 for a in actions)


# -- stuck detection -----------------------------------------------------------------------


async def test_stuck_by_unchanged_screen(harness: Any) -> None:
    h = harness(clicks(10), screens=[make_png(seed=3)], budgets=Budgets(max_steps_per_task=9))
    out = await h.run()
    assert out.status == "failed"
    assert out.reason == "budget"
    assert out.steps == 9  # the loop keeps running while stuck
    stuck = h.events("agent.stuck_detected")
    assert len(stuck) == 1
    assert stuck[0].payload.reason == "screen_unchanged"
    assert len(stuck[0].payload.frame_ids) == 3
    assert "stuck" in h.statuses()


async def test_stuck_by_repetition(harness: Any) -> None:
    same = tool_response("click", x=300, y=200)
    h = harness([same, same, same, same, give_up()])
    await h.run()
    stuck = h.events("agent.stuck_detected")
    assert [e.payload.reason for e in stuck] == ["repetition"]


async def test_no_tool_retries_then_stuck(harness: Any) -> None:
    h = harness([text_response("I think..."), text_response("hmm"), text_response("ok"), click(1),
                 give_up()])  # fmt: skip
    out = await h.run()
    assert out.reason == "fail_task"
    assert len(h.model.calls) == 5  # step 1: 1 + 2 retries; then steps 2 and 3
    retry = h.model.calls[1].request
    assert retry.messages[-1].text_content() == NO_TOOL_CORRECTION
    assert retry.messages[-2].role == "assistant"
    steps = h.steps()
    assert steps[0].action is None
    assert [e.payload.reason for e in h.events("agent.stuck_detected")] == ["no_tool"]
    assert h.statuses()[:2] == ["working", "stuck"]


async def test_fallback_parser_is_used(harness: Any) -> None:
    def parser(text: str, names: Sequence[str]) -> list[ToolCall]:
        if "pyautogui.click" in text:
            return [ToolCall(id="fb1", name="click", arguments={"x": 10, "y": 20})]
        return []

    h = harness([text_response("pyautogui.click(10, 20)"), give_up()], fallback_parser=parser)
    await h.run()
    assert h.sandbox.actions[0].args["x"] == 10
    assert len(h.model.calls) == 2


# -- control: pause, takeover, stop --------------------------------------------------------


@pytest.mark.parametrize("mode", ["pause", "takeover"])
async def test_pause_then_resume_adds_note_and_drops_pending_action(
    harness: Any, mode: Any
) -> None:
    h = harness()

    def pause_during_model_call(req: ModelRequest) -> ModelResponse:
        h.control.set(mode)
        asyncio.get_running_loop().call_later(0.05, h.control.set, "run")
        return click(2)

    h.model.set_script("Ada", [click(1), pause_during_model_call, click(3), submit()])
    out = await h.run()

    assert out.status == "submitted"
    # the click computed while the user took over was never executed
    assert len(h.sandbox.actions) == 2
    dropped = h.steps()[1].action
    assert dropped.is_error
    assert "the user paused or stopped you" in dropped.result
    assert RESUME_NOTE in last_user_text(h.model.calls[2].request)
    assert RESUME_NOTE not in last_user_text(h.model.calls[3].request)
    assert h.statuses() == ["working", "paused_by_user", "working", "idle"]
    paused = h.events("agent.status_changed")[1].payload
    assert isinstance(paused, AgentStatusChanged)
    assert paused.reason == mode


async def test_stop_exits_within_one_step(harness: Any) -> None:
    h = harness()

    def stop_now(req: ModelRequest) -> ModelResponse:
        h.control.set("stop")
        return click(2)

    h.model.set_script("Ada", [click(1), stop_now, click(3)])
    out = await h.run()
    assert out.status == "stopped"
    assert len(h.model.calls) == 2
    assert len(h.sandbox.actions) == 1
    assert h.statuses()[-1] == "stopped"


async def test_stop_interrupts_a_slow_model_call(harness: Any) -> None:
    h = harness([click(1)], latency_s=30)
    asyncio.get_running_loop().call_later(0.05, h.control.set, "stop")
    t0 = time.monotonic()
    out = await h.run(limit_s=5)
    assert time.monotonic() - t0 < 2
    assert out.status == "stopped"
    assert out.steps == 0
    assert h.sandbox.actions == []


async def test_stop_while_paused(harness: Any) -> None:
    h = harness([click(1)])
    h.control.set("pause")
    asyncio.get_running_loop().call_later(0.05, h.control.set, "stop")
    out = await h.run()
    assert out.status == "stopped"
    assert h.model.calls == []


# -- budgets -------------------------------------------------------------------------------


async def test_max_steps_per_task_fails_with_budget(harness: Any) -> None:
    h = harness(clicks(5), budgets=Budgets(max_steps_per_task=3))
    out = await h.run()
    assert out.status == "failed"
    assert out.reason == "budget"
    assert out.detail == "max_steps_per_task (3) reached"
    assert out.steps == 3
    assert len(h.model.calls) == 3


async def test_session_budget_exceeded_fails_with_budget(harness: Any) -> None:
    h = harness(clicks(5), budgets=Budgets(max_total_steps=2))
    out = await h.run()
    assert out.reason == "budget"
    assert "max_total_steps" in out.detail
    assert len(h.model.calls) == 2


async def test_token_budget(harness: Any) -> None:
    h = harness(clicks(5), budgets=Budgets(max_tokens=250))
    out = await h.run()
    assert out.reason == "budget"
    assert "max_tokens" in out.detail
    assert out.steps == 3  # 120 tokens per step


# -- approvals -----------------------------------------------------------------------------


class FakeGate:
    def __init__(self, status: ApprovalStatus, note: str | None = None, hang: bool = False):
        self.status = status
        self.note = note
        self.hang = hang
        self.requests: list[dict[str, Any]] = []
        self.cancelled = False
        self.entered = asyncio.Event()

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
        self.requests.append({"summary": action_summary, "frame_id": frame_id})
        self.entered.set()
        if self.hang:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
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


ASK = tool_response("request_approval", action_summary="submit the form", risk="high")


@pytest.mark.parametrize(
    ("status", "note", "expected"),
    [
        ("approved", None, "[system note] Approved: submit the form"),
        ("denied", "save a draft", "[system note] Denied: submit the form. Do this instead: save"),
    ],
)
async def test_approval_paths(
    harness: Any, status: ApprovalStatus, note: str | None, expected: str
) -> None:
    gate = FakeGate(status, note)
    h = harness([*clicks(3), ASK, submit()], approvals=gate)
    out = await h.run()
    assert out.status == "submitted"
    after = h.model.calls[4].request
    assert expected in last_user_text(after)
    assert tool_texts(after)[-1].startswith(status.capitalize())
    assert gate.requests == [{"summary": "submit the form", "frame_id": h.steps()[3].frame.id}]
    assert h.statuses() == ["working", "awaiting_approval", "working", "idle"]


async def test_stop_while_awaiting_approval(harness: Any) -> None:
    gate = FakeGate("approved", hang=True)
    h = harness([*clicks(3), ASK, submit()], approvals=gate)

    async def stop_when_waiting() -> None:
        await gate.entered.wait()
        assert h.runtime.state.status == "awaiting_approval"
        h.control.set("stop")

    stopper = asyncio.create_task(stop_when_waiting())
    out = await h.run(limit_s=5)
    await stopper
    assert out.status == "stopped"
    assert gate.cancelled
    assert len(h.model.calls) == 4


# -- failures ------------------------------------------------------------------------------


async def test_model_error_is_retried(harness: Any) -> None:
    h = harness([ModelError("429 after retries"), click(1), give_up()])
    out = await h.run()
    assert out.reason == "fail_task"
    assert [s.step for s in h.steps()] == [1, 2]
    errors = h.events("agent.error")
    assert len(errors) == 1
    assert errors[0].payload.recoverable
    assert "429" in errors[0].payload.error


async def test_repeated_model_errors_fail_the_task(harness: Any) -> None:
    h = harness([ModelError("down"), ModelError("down"), ModelError("down")])
    out = await h.run()
    assert out.status == "failed"
    assert out.reason == "model_error"
    assert [e.payload.recoverable for e in h.events("agent.error")] == [True, True, False]


async def test_expired_sandbox_fails_with_sandbox_lost(harness: Any) -> None:
    h = harness([click(1)])
    await h.sandbox.kill()
    out = await h.run()
    assert out.reason == "sandbox_lost"
    assert h.model.calls == []


async def test_invalid_screenshot_is_a_recoverable_sandbox_error(harness: Any) -> None:
    """A screenshot that is not a valid image is handled like any other sandbox error:
    a recoverable agent.error, backoff, then a fresh screenshot (T06 follow-up)."""
    h = harness([click(1), give_up()], screens=[b"not a png", *distinct_screens(3)])
    out = await h.run()
    assert out.reason == "fail_task"
    errors = h.events("agent.error")
    assert len(errors) == 1
    assert errors[0].payload.recoverable
    assert "not a valid image" in errors[0].payload.error
    assert h.sandbox.screenshots_taken == 3
    assert [s.step for s in h.steps()] == [1, 2]
    assert len(h.model.calls) == 2


async def test_lost_lease_stops_the_loop(harness: Any) -> None:
    async def renew() -> bool:
        return False

    h = harness([click(1)], latency_s=30, renew_lease=renew)
    out = await h.run(limit_s=5)
    assert out.status == "failed"
    assert out.reason == "lease_lost"
    assert h.statuses()[-1] == "error"
    assert h.sandbox.actions == []


async def test_lost_lease_while_paused_does_not_hang(harness: Any) -> None:
    calls = 0

    async def renew() -> bool:
        nonlocal calls
        calls += 1
        return calls < 3

    h = harness([click(1)], renew_lease=renew)
    h.control.set("pause")
    out = await h.run(limit_s=5)
    assert out.reason == "lease_lost"
    assert h.model.calls == []
    assert h.statuses() == ["working", "paused_by_user", "error"]


async def test_heartbeat_renews_lease_while_running(harness: Any) -> None:
    renewals = 0

    async def renew() -> bool:
        nonlocal renewals
        renewals += 1
        return True

    h = harness([click(1), give_up()], latency_s=0.1, renew_lease=renew)
    await h.run()
    beats = [m for m in h.bus.ephemeral if isinstance(m, Heartbeat)]
    assert len(beats) >= 3
    assert renewals == len(beats)
    assert all(b.task_id == h.task.id for b in beats)
    await asyncio.sleep(0.06)
    assert len([m for m in h.bus.ephemeral if isinstance(m, Heartbeat)]) == len(beats)


async def test_repeated_renewal_exceptions_fail_closed(harness: Any) -> None:
    """An unexpected renew exception is not proof the lease is held (T06 review #3)."""
    calls = 0

    async def renew() -> bool:
        nonlocal calls
        calls += 1
        raise ConnectionError("redis down")

    h = harness([click(1)], latency_s=30, renew_lease=renew)
    out = await h.run(limit_s=5)
    assert out.reason == "lease_lost"
    assert calls == 1
    assert h.sandbox.actions == []


async def test_renewal_exceptions_stop_before_confirmed_expiry(harness: Any) -> None:
    """With a known TTL, failures are tolerated only while the last confirmed
    renewal still covers the next attempt; the loop stops before it expires."""
    ttl = 0.3
    confirmed: list[float] = []
    lost_at: list[float] = []

    async def renew() -> bool:
        if not confirmed:
            confirmed.append(time.monotonic())
            return True
        lost_at.append(time.monotonic())
        raise ConnectionError("redis down")

    h = harness(
        [click(1)],
        latency_s=30,
        renew_lease=renew,
        config={"lease_ttl_s": ttl, "lease_renew_timeout_s": 0.05},
    )
    out = await h.run(limit_s=5)
    assert out.reason == "lease_lost"
    assert len(lost_at) >= 2  # transient failures were tolerated for a while
    assert lost_at[-1] < confirmed[0] + ttl  # but it stopped before the lease expired


async def test_hung_renewal_is_bounded(harness: Any) -> None:
    async def renew() -> bool:
        await asyncio.Event().wait()
        return True

    h = harness([click(1)], latency_s=30, renew_lease=renew,
                config={"lease_renew_timeout_s": 0.05})  # fmt: skip
    out = await h.run(limit_s=5)
    assert out.reason == "lease_lost"


async def test_stalled_heartbeat_publish_does_not_delay_renewal(harness: Any) -> None:
    renewals = 0

    async def renew() -> bool:
        nonlocal renewals
        renewals += 1
        return renewals < 3

    h = harness([click(1)], latency_s=30, renew_lease=renew,
                config={"heartbeat_publish_timeout_s": 0.02})  # fmt: skip

    async def stalled_publish(message: Any) -> None:
        await asyncio.Event().wait()

    h.bus.publish_ephemeral = stalled_publish
    out = await h.run(limit_s=5)
    assert out.reason == "lease_lost"
    assert renewals == 3


def _register_slow_coordination(h: Harness, lose: asyncio.Event) -> None:
    async def slow(args: dict[str, Any], ctx: Any) -> ToolResult:
        lose.set()  # the next renewal fails while this call is still running
        await asyncio.sleep(0.2)
        return ToolResult(text="slow ok")

    h.runtime.registry.register(
        "coordination",
        ToolSpec(name="slow_post", description="slow",
                 parameters={"type": "object", "properties": {}}),
        slow,
        kind="coordination",
    )  # fmt: skip


async def test_lease_lost_during_coordination_skips_desktop_call(harness: Any) -> None:
    """T06 review #4: no desktop action after the lease is lost mid-step."""
    lose = asyncio.Event()

    async def renew() -> bool:
        return not lose.is_set()

    step = multi_tool_response(("slow_post", {}), ("click", {"x": 10, "y": 10}))
    h = harness([step, click(2)], renew_lease=renew)
    _register_slow_coordination(h, lose)
    out = await h.run(limit_s=5)
    assert out.reason == "lease_lost"
    assert h.sandbox.actions == []
    action = h.steps()[0].action
    assert action.tool == "click"
    assert "lease was lost" in action.result


async def test_lease_lost_during_coordination_rejects_submit(harness: Any) -> None:
    lose = asyncio.Event()

    async def renew() -> bool:
        return not lose.is_set()

    step = multi_tool_response(
        ("slow_post", {}), ("submit_result", {"summary": "s", "outputs": {"result": "1"}})
    )
    h = harness([*clicks(3), step], renew_lease=renew)
    _register_slow_coordination(h, lose)
    out = await h.run(limit_s=5)
    assert out.status == "failed"
    assert out.reason == "lease_lost"
    assert out.result is None


async def test_irreducible_prompt_fails_task_with_budget(harness: Any) -> None:
    """T06 review #7: an oversized mandatory prompt is a typed budget failure, not
    a model call that is bound to overflow the context window."""
    h = harness([click(1)], context=ContextSettings(prompt_budget_tokens=1000))
    out = await h.run()
    assert out.status == "failed"
    assert out.reason == "budget"
    assert "budget is" in out.detail
    assert h.model.calls == []
    errors = h.events("agent.error")
    assert len(errors) == 1
    assert not errors[0].payload.recoverable


# -- digest and modes ----------------------------------------------------------------------


class FakeDigest:
    def __init__(self) -> None:
        self.since: list[int] = []

    async def build(self, agent: Agent, task: Task, *, since: int, budget_tokens: int) -> Digest:
        self.since.append(since)
        if since < 5:
            return Digest("[#5 fact · Brook · T2] Notion Plus $10/mo", cursor=5)
        return Digest("", cursor=since)


class OversizedFirstDigest:
    """Entries #1-#5: the first digest is too big for the prompt budget (it gets
    trimmed); later digests from the same cursor fit."""

    def __init__(self) -> None:
        self.since: list[int] = []

    async def build(self, agent: Agent, task: Task, *, since: int, budget_tokens: int) -> Digest:
        self.since.append(since)
        if since >= 5:
            return Digest("", cursor=since)
        if len(self.since) == 1:
            return Digest("[#1 fact] " + "x" * 400_000 + " END-OF-DIGEST", cursor=5)
        return Digest("[#1 fact] compact END-OF-DIGEST", cursor=5)


async def test_trimmed_digest_does_not_advance_cursor(harness: Any) -> None:
    """T06 review #6: entries cut from the digest reappear in the next step."""
    digest = OversizedFirstDigest()
    h = harness([click(1), click(2), give_up()], digest=digest)
    await h.run()
    first = last_user_text(h.model.calls[0].request)
    second = last_user_text(h.model.calls[1].request)
    assert "END-OF-DIGEST" not in first
    assert digest.since[:2] == [0, 0]
    assert "[#1 fact] compact END-OF-DIGEST" in second
    assert digest.since[2] == 5


async def test_digest_in_user_message_and_cursor_moves(harness: Any) -> None:
    digest = FakeDigest()
    h = harness([click(1), click(2), give_up()], digest=digest)
    await h.run()
    assert "Inbox:\n[#5 fact · Brook · T2] Notion Plus $10/mo" in last_user_text(
        h.model.calls[0].request
    )
    assert "Inbox:" not in last_user_text(h.model.calls[1].request)
    assert digest.since == [0, 5, 5]
    assert h.runtime.state.digest_cursor == 5


async def test_parallel_isolated_has_no_digest_or_coordination(harness: Any) -> None:
    digest = FakeDigest()
    h = harness([click(1), give_up()], digest=digest, mode="parallel_isolated")
    await h.run()
    assert digest.since == []
    names = {t.name for t in h.model.calls[0].request.tools}
    assert "post" not in names
    assert "click" in names
    assert "Inbox:" not in last_user_text(h.model.calls[0].request)


async def test_full_frames_kept_when_policy_says_so(harness: Any) -> None:
    h = harness([click(1), give_up()], config={"keep_full_frames": True})
    await h.run()
    keys = [f.key for f in await h.frames.list("frames/")]
    assert sum(k.endswith(".full.jpg") for k in keys) == 2
    assert all(s.frame.storage_key_full for s in h.steps())


async def test_agent_runs_one_task_at_a_time(harness: Any) -> None:
    h = harness([click(1), give_up()], latency_s=0.05)
    first = asyncio.create_task(h.run())
    await asyncio.sleep(0.01)
    with pytest.raises(RuntimeError, match="already running"):
        await h.runtime.run_task(h.task)
    await first


async def test_step_excerpt_falls_back_to_redacted_reasoning(harness: Any) -> None:
    """T13: with ``content`` empty and the thinking in ``reasoning``, the step excerpt
    shows the reasoning (redacted, whitespace-collapsed, <= 400 chars)."""
    secret = "sk-abcdefghijklmnopqrstuvwxyz123456"  # gitleaks:allow
    thinking = f"The Continue\n button  is centred; key {secret} " + "x" * 600
    first = tool_response("click", x=500, y=470).model_copy(update={"reasoning": thinking})
    h = harness([first, *clicks(2, start=2), submit(result="42")])
    out = await h.run(limit_s=30)
    assert out.status == "submitted"
    steps = h.steps()
    excerpt = steps[0].reasoning_excerpt
    assert excerpt.startswith("The Continue button is centred; key sk-[redacted] x")
    assert secret not in excerpt
    assert len(excerpt) == 400
    assert steps[1].reasoning_excerpt == "clicking 2"
    assert steps[3].reasoning_excerpt == ""  # submit: neither text nor reasoning


def test_text_wins_over_reasoning_for_the_excerpt() -> None:
    assert reasoning_excerpt("clicking 1", "long thinking") == "clicking 1"
    assert reasoning_excerpt(None, None) == ""


@pytest.mark.parametrize(
    ("mode", "phrase"),
    [("normalized_1000", "integers 0..1000"), ("absolute", "integer pixels in the")],
)
async def test_requests_state_the_configured_coordinate_scale(
    harness: Any, mode: str, phrase: str
) -> None:
    """T13: the scale of the configured ``coordinate_mode`` is in the system prompt and
    on every coordinate parameter of the tools sent to the model."""
    h = harness([*clicks(3), submit(result="42")], config={"coordinate_mode": mode})
    await h.run(limit_s=30)
    req = h.model.calls[0].request
    assert phrase in req.messages[0].text_content()
    click = next(t for t in req.tools if t.name == "click")
    assert phrase in click.description
    expected = "0..1000, from the left edge" if mode == "normalized_1000" else "pixels 0.."
    assert click.parameters["properties"]["x"]["description"].startswith(expected)
