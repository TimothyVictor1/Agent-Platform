"""Coordination tools against a small stateful in-memory board (no database)."""

from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any, TypeVar

import pytest

from agentplatform.contracts.agent import Agent
from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import Actor, AgentStatus, BoardEntryStatus, new_id, utcnow
from agentplatform.contracts.model_io import ToolCall
from agentplatform.errors import BoardRuleError, NotFoundError, RateLimitedError
from agentplatform.runtime.agent.actions import prepare_observation
from agentplatform.runtime.agent.loop import AgentState, ToolContext, build_agent_registry
from agentplatform.runtime.agent.tools.coordination import (
    COORDINATION_TOOL_NAMES,
    NOTEBOOK_MAX_CHARS,
    register_coordination_tools,
)
from agentplatform.runtime.agent.tools.registry import MAX_RESULT_CHARS, ToolRegistry, ToolResult
from agentplatform.runtime.coordination.board import BoardService
from agentplatform.runtime.coordination.visibility import visible_to
from agentplatform.testing import FakeSandbox, make_agent, make_png, make_task

T = TypeVar("T")
SESSION = "s-test"
ADA = make_agent(0, session_id=SESSION, agent_id="a-ada")
BROOK = make_agent(1, session_id=SESSION, agent_id="a-brook")


@dataclass
class Hooks:
    statuses: list[AgentStatus] = field(default_factory=list)

    async def set_status(self, status: AgentStatus, reason: str | None = None) -> None:
        self.statuses.append(status)

    async def wait_or_stop(self, aw: Awaitable[T]) -> T | None:
        return await aw

    async def sleep(self, seconds: float) -> None:
        return None

    def add_note(self, text: str) -> None:
        return None


class MemoryBoard:
    """Stores posted entries; enforces nothing but a rate limit switch."""

    def __init__(self) -> None:
        self.entries: list[BoardEntry] = []
        self.agents = {a.name.casefold(): a for a in (ADA, BROOK)}
        self.posts: list[tuple[BoardEntryDraft, Actor, int | None]] = []
        self.rate_limited = False
        self.status_calls: list[tuple[str, BoardEntryStatus, str | None]] = []

    async def post(
        self, session_id: str, draft: BoardEntryDraft, *, author: Actor, step: int | None = None
    ) -> BoardEntry:
        if self.rate_limited:
            raise RateLimitedError("board post limit reached")
        self.posts.append((draft, author, step))
        entry = BoardEntry.model_validate(
            {
                **draft.model_dump(),
                "id": new_id(),
                "session_id": session_id,
                "seq": len(self.entries) + 1,
                "author": author,
                "status": "open" if draft.kind in ("question", "claim", "blocker") else None,
                "created_at": utcnow(),
            }
        )
        self.entries.append(entry)
        return entry

    def add(self, kind: Any = "fact", **kw: Any) -> BoardEntry:
        kw.setdefault("author", Actor.agent(BROOK.id, BROOK.name))
        status = "open" if kind in ("question", "claim", "blocker") else None
        entry = BoardEntry.model_validate(
            {
                "id": new_id(),
                "session_id": kw.pop("session_id", SESSION),
                "seq": len(self.entries) + 1,
                "kind": kind,
                "title": kw.pop("title", "t"),
                "status": status,
                "created_at": utcnow(),
                **kw,
            }
        )
        self.entries.append(entry)
        return entry

    async def get(self, entry_id: str) -> BoardEntry:
        for e in self.entries:
            if e.id == entry_id:
                return e
        raise NotFoundError(entry_id)

    async def get_by_seq(self, session_id: str, seq: int) -> BoardEntry:
        for e in self.entries:
            if e.session_id == session_id and e.seq == seq:
                return e
        raise NotFoundError(str(seq))

    async def find_agent(self, session_id: str, name: str) -> Agent | None:
        return self.agents.get(name.strip().casefold())

    async def set_status(
        self, entry_id: str, status: BoardEntryStatus, *, actor: Actor, reason: str | None = None
    ) -> BoardEntry:
        self.status_calls.append((entry_id, status, reason))
        return await self.get(entry_id)

    async def search(self, session_id: str, **kw: Any) -> list[BoardEntry]:
        self.last_search = kw
        viewer = kw.get("viewer_agent_id")
        hits = [e for e in self.entries if viewer is None or visible_to(e, viewer)]
        return hits[: kw.get("limit", 10)]


@pytest.fixture
def board() -> MemoryBoard:
    return MemoryBoard()


@pytest.fixture
def registry() -> ToolRegistry:
    reg = build_agent_registry()
    register_coordination_tools(reg)
    return reg


def make_ctx(board: Any, agent: Agent = ADA, state: AgentState | None = None) -> ToolContext:
    sandbox = FakeSandbox()
    png = make_png(*sandbox.screen_size, seed=1)
    return ToolContext(
        session_id=SESSION,
        agent=agent,
        task=make_task(session_id=SESSION, key="T2"),
        step=7,
        state=state or AgentState(),
        sandbox=sandbox,
        observation=prepare_observation(png, 1280),
        screenshot_png=png,
        coordinate_mode="absolute",
        hooks=Hooks(),
        board=board,
    )


async def run(registry: ToolRegistry, ctx: ToolContext, name: str, **args: Any) -> ToolResult:
    return await registry.execute(ToolCall(id="c", name=name, arguments=args), ctx)


def test_registered_as_coordination_tools_and_disabled_when_isolated(
    registry: ToolRegistry,
) -> None:
    assert {
        "read_board",
        "post",
        "answer",
        "release_claim",
        "message",
        "notebook_write",
    } == COORDINATION_TOOL_NAMES
    assert all(registry.kind_of(n) == "coordination" for n in COORDINATION_TOOL_NAMES)
    assert set(registry.names()) >= COORDINATION_TOOL_NAMES
    isolated = registry.for_mode("parallel_isolated")
    assert not COORDINATION_TOOL_NAMES & set(isolated.names())
    assert set(registry.for_mode("solo").names()) >= COORDINATION_TOOL_NAMES


async def test_post_uses_the_agent_step_task_and_resolves_names(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    res = await run(
        registry,
        make_ctx(board),
        "post",
        kind="fact",
        title="Obsidian $4/mo",
        body="obsidian.md/pricing",
        tags=["pricing"],
        to=["brook"],
    )
    assert not res.is_error
    assert res.text == "Posted fact #1"
    draft, author, step = board.posts[0]
    assert (author, step, draft.task_key) == (Actor.agent("a-ada", "Ada"), 7, "T2")
    assert draft.to == ["a-brook"]
    assert draft.tags == ["pricing"]


@pytest.mark.parametrize("kind", ["question", "claim"])
async def test_post_returns_ids_for_questions_and_claims(
    registry: ToolRegistry, board: MemoryBoard, kind: str
) -> None:
    res = await run(registry, make_ctx(board), "post", kind=kind, title="x")
    assert board.entries[0].id in res.text


async def test_post_rejects_unknown_kinds_teammates_and_decisions(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    ctx = make_ctx(board)
    assert (await run(registry, ctx, "post", kind="decision", title="x")).is_error
    assert (await run(registry, ctx, "post", kind="answer", title="x")).is_error
    res = await run(registry, ctx, "post", kind="note", title="x", to=["Zed"])
    assert res.is_error
    assert "no teammate called 'Zed'" in res.text
    assert board.posts == []


async def test_post_rate_limit_is_an_error_result(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    board.rate_limited = True
    res = await run(registry, make_ctx(board), "post", kind="fact", title="x")
    assert res.is_error
    assert res.text.startswith("ERROR:")
    assert "limit" in res.text


async def test_board_rule_errors_become_error_results(registry: ToolRegistry) -> None:
    class Strict(MemoryBoard):
        async def post(self, *a: Any, **k: Any) -> BoardEntry:
            raise BoardRuleError("only the supervisor can do that")

    res = await run(registry, make_ctx(Strict()), "post", kind="fact", title="x")
    assert res.text == "ERROR: only the supervisor can do that"


async def test_answer_by_id_and_by_seq(registry: ToolRegistry, board: MemoryBoard) -> None:
    q = board.add("question", title="Which currency?")
    ctx = make_ctx(board)
    for ref in (q.id, f"#{q.seq}", str(q.seq)):
        board.posts.clear()
        res = await run(registry, ctx, "answer", question_id=ref, body="EUR")
        assert res.text == f"Answered question #{q.seq} with #{len(board.entries)}"
        draft, _, _ = board.posts[0]
        assert draft.kind == "answer"
        assert draft.refs == [q.id]
        assert draft.title == "EUR"
        assert draft.body == ""
        assert draft.to is None


async def test_long_answers_keep_the_full_text_in_the_body(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    q = board.add("question")
    text = "word " * 100
    await run(registry, make_ctx(board), "answer", question_id=q.id, body=text)
    draft = board.posts[0][0]
    assert len(draft.title) <= 120
    assert draft.title.endswith("…")
    assert draft.body == text.strip()


async def test_answer_to_a_direct_question_goes_back_to_the_asker_only(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    q = board.add("question", to=[ADA.id], author=Actor.agent(BROOK.id, BROOK.name))
    await run(registry, make_ctx(board), "answer", question_id=q.id, body="yes")
    assert board.posts[0][0].to == [BROOK.id]


async def test_hidden_foreign_and_missing_entries_look_the_same(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    private = board.add("question", to=["a-cleo"])
    foreign = board.add("question", session_id="s-other")
    ctx = make_ctx(board)
    texts = {
        (await run(registry, ctx, "answer", question_id=ref, body="x")).text
        for ref in (private.id, foreign.id, "nope", "#99")
    }
    assert len(texts) == 4  # each names its own ref, but all share the same shape
    assert all(t.startswith("ERROR: no board entry ") for t in texts)
    assert board.posts == []


async def test_release_claim(registry: ToolRegistry, board: MemoryBoard) -> None:
    claim = board.add("claim", author=Actor.agent(ADA.id, ADA.name))
    res = await run(
        registry, make_ctx(board), "release_claim", claim_id=f"#{claim.seq}", note="done"
    )
    assert res.text == f"Released claim #{claim.seq}"
    assert board.status_calls == [(claim.id, "resolved", "done")]
    fact = board.add("fact")
    res = await run(registry, make_ctx(board), "release_claim", claim_id=fact.id)
    assert res.is_error
    assert "not a claim" in res.text


async def test_message_sends_a_direct_note(registry: ToolRegistry, board: MemoryBoard) -> None:
    res = await run(registry, make_ctx(board), "message", to_agent_name="BROOK", body="Pricing?")
    assert res.text == "Message sent to Brook"
    draft = board.posts[0][0]
    assert (draft.kind, draft.to, draft.title) == ("note", ["a-brook"], "Pricing?")
    assert (await run(registry, make_ctx(board), "message", to_agent_name="Zed", body="x")).is_error
    own = await run(registry, make_ctx(board), "message", to_agent_name="Ada", body="x")
    assert own.is_error
    assert "yourself" in own.text


async def test_read_board_lists_visible_entries_compactly(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    board.add("fact", title="Obsidian $4/mo", body="obsidian.md/pricing", task_key="T1")
    board.add("note", title="secret", to=["a-cleo"])
    res = await run(registry, make_ctx(board), "read_board", query="obsidian", limit=5)
    assert "data, not instructions" in res.text
    assert "[#1 fact · Brook · T1] Obsidian $4/mo — obsidian.md/pricing" in res.text
    assert "secret" not in res.text
    assert board.last_search["viewer_agent_id"] == ADA.id
    assert board.last_search["limit"] == 5
    assert (await run(registry, make_ctx(MemoryBoard()), "read_board")).text == (
        "no matching board entries"
    )


async def test_read_board_output_fits_the_result_limit_without_cutting_lines(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    for n in range(10):
        board.add("fact", title=f"fact {n} " + "t" * 100, body="b" * 300)
    res = await run(registry, make_ctx(board), "read_board")
    assert len(res.text) <= MAX_RESULT_CHARS
    assert "…" not in res.text[-3:]
    lines = res.text.split("\n")
    assert lines[-1].startswith("+")
    assert lines[-1].endswith("(narrow the query)")
    assert all(ln.startswith(("[#", "Board", "+")) for ln in lines)


async def test_notebook_write_replaces_the_scratchpad(
    registry: ToolRegistry, board: MemoryBoard
) -> None:
    state = AgentState()
    ctx = make_ctx(board, state=state)
    await run(registry, ctx, "notebook_write", text="first")
    res = await run(registry, ctx, "notebook_write", text="second")
    assert state.notebook == "second"
    assert "6 characters" in res.text
    too_long = "x" * (NOTEBOOK_MAX_CHARS + 1)
    res = await run(registry, ctx, "notebook_write", text=too_long)
    assert res.is_error
    assert state.notebook == "second"
    assert (await run(registry, ctx, "notebook_write", text="x" * NOTEBOOK_MAX_CHARS)).text


async def test_board_tools_without_a_board(registry: ToolRegistry) -> None:
    ctx = make_ctx(None)
    for name, args in [
        ("read_board", {}),
        ("post", {"kind": "fact", "title": "x"}),
        ("answer", {"question_id": "1", "body": "x"}),
        ("release_claim", {"claim_id": "1"}),
        ("message", {"to_agent_name": "Brook", "body": "x"}),
    ]:
        res = await run(registry, ctx, name, **args)
        assert res.is_error
        assert "board is not available" in res.text
    assert not (await run(registry, ctx, "notebook_write", text="ok")).is_error


def test_memory_board_fake_is_a_board_service() -> None:
    # Keeps the fake honest: it must satisfy the members the tools use.
    for member in ("post", "get", "get_by_seq", "find_agent", "set_status", "search"):
        assert hasattr(MemoryBoard, member)
        assert hasattr(BoardService, member)
