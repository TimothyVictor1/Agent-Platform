"""Scripted fakes for the SPEC §21 scenario suite, on top of the runtime "world"
(``runtime.support``): the whole API → runtime path with fakes only (commands on the
in-memory bus, planner, scheduler, pool, agent loop, board + digest, artifacts,
supervisor, approvals, budgets).

* ``ScriptedAgentModel`` records what every agent saw on every step (system prompt,
  last user message, tool names) and lets a test script single steps per task key.
* ``ScriptedSupervisor`` accepts every submitted result (like ``SupervisorModel``)
  and, optionally, answers open questions and messages stuck agents.
* ``ResolvingBoard`` adds the board rule the scenarios rely on and the plain
  ``MemoryBoard`` leaves out: an ``answer`` resolves the open question it refs
  (``PostgresBoardService`` does this in the answer's transaction).
"""

from __future__ import annotations

import asyncio
import re
import traceback
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from runtime.support import (
    _AGENT_RE,
    _STEP_RE,
    _TASK_RE,
    AgentModel,
    MemoryBoard,
    SupervisorModel,
    World,
    payload,
)

from agentplatform.contracts.board import BoardEntry, BoardEntryDraft
from agentplatform.contracts.common import Actor
from agentplatform.contracts.events import AgentSandboxReady
from agentplatform.contracts.model_io import ModelRequest, ModelResponse, ModelUsage
from agentplatform.errors import BoardRuleError
from agentplatform.testing import (
    FakeSandbox,
    make_tasks,
    multi_tool_response,
    text_response,
    tool_response,
)

__all__ = [
    "AgentCall",
    "ResolvingBoard",
    "ScenarioWorld",
    "ScriptedAgentModel",
    "ScriptedSupervisor",
    "StepScript",
    "multi_tool_response",
    "tool_response",
]


@dataclass(frozen=True)
class AgentCall:
    """One agent model request as the model saw it."""

    agent: str
    task: str
    step: int
    system: str
    user: str
    """Text of the last user message (step header, notes, inbox, notebook)."""
    tools: frozenset[str]
    tool_results: tuple[str, ...]
    """Text of the ``tool`` messages that answered the previous step."""

    @property
    def inbox(self) -> str:
        """The inbox digest of this step ('' when there is none)."""
        if "Inbox:\n" not in self.user:
            return ""
        lines = self.user.split("Inbox:\n", 1)[1].splitlines()
        out: list[str] = []
        for line in lines:
            if not line.startswith(("[#", "+")):
                break
            out.append(line)
        return "\n".join(out)


StepScript = Callable[[AgentCall], ModelResponse | Awaitable[ModelResponse]]


class ScriptedAgentModel(AgentModel):
    """``AgentModel`` (``behaviors``: shell steps, then submit) plus per-step scripts
    keyed ``(task_key, step)`` and a record of every request in ``calls``."""

    def __init__(self) -> None:
        super().__init__()
        self.scripts: dict[tuple[str, int], StepScript] = {}
        self.calls: list[AgentCall] = []

    def script(self, task: str, step: int, item: StepScript | ModelResponse) -> None:
        if isinstance(item, ModelResponse):
            response = item
            self.scripts[(task, step)] = lambda _call: response
        else:
            self.scripts[(task, step)] = item

    def of(self, task: str) -> list[AgentCall]:
        return [c for c in self.calls if c.task == task]

    async def complete(self, req: ModelRequest) -> ModelResponse:
        try:
            return await self._complete(req)
        except Exception:  # the loop logs only the type; keep the traceback visible
            traceback.print_exc()
            raise

    async def _complete(self, req: ModelRequest) -> ModelResponse:
        system = req.messages[0].text_content()
        agent_m, task_m = _AGENT_RE.search(system), _TASK_RE.search(system)
        assert agent_m is not None
        assert task_m is not None
        users = [m for m in req.messages if m.role == "user"]
        user = users[-1].text_content() if users else ""
        step_m = _STEP_RE.search(user)
        step = int(step_m.group(1)) if step_m else 1
        # tool messages after the last assistant message answer the previous step
        last_assistant = max(
            (i for i, m in enumerate(req.messages) if m.role == "assistant"), default=-1
        )
        results = tuple(
            m.text_content() for m in req.messages[last_assistant + 1 :] if m.role == "tool"
        )
        call = AgentCall(
            agent=agent_m.group(1),
            task=task_m.group(1),
            step=step,
            system=system,
            user=user,
            tools=frozenset(t.name for t in req.tools or []),
            tool_results=results,
        )
        self.calls.append(call)
        scripted = self.scripts.get((call.task, step))
        if scripted is None:
            return await super().complete(req)
        self.requests.append((call.agent, call.task, step))
        out = scripted(call)
        return await out if isinstance(out, Awaitable) else out


_ENTRY_RE = re.compile(r"\(entry id (\S+), status open\)")
_STUCK_RE = re.compile(r"^(\S+) is stuck \(")


class ScriptedSupervisor(SupervisorModel):
    """Accepts every result; with ``answers`` it answers each open question named in
    a ``question`` trigger with that text; with ``stuck_message`` it sends that note
    to every agent named in a ``stuck`` trigger."""

    def __init__(self, *, answers: str | None = None, stuck_message: str | None = None) -> None:
        super().__init__()
        self.answers = answers
        self.stuck_message = stuck_message
        self.messages: list[str] = []

    async def complete(self, req: ModelRequest) -> ModelResponse:
        text = req.messages[1].text_content() if len(req.messages) > 1 else ""
        if req.messages[-1].role != "tool":
            self.messages.append(text)
        calls: list[tuple[str, dict[str, Any]]] = []
        if req.messages[-1].role != "tool":
            blocks = text.split("\n- [")
            for block in blocks:
                reason = block.split("]", 1)[0].lstrip("- [")
                if reason in ("question", "unanswered_question") and self.answers:
                    for entry_id in _ENTRY_RE.findall(block):
                        calls.append(
                            (
                                "answer",
                                {
                                    "question_id": entry_id,
                                    "body": self.answers,
                                    "why": "The goal says so.",
                                },
                            )
                        )
                if reason == "stuck" and self.stuck_message:
                    detail = block.split("] ", 1)[1] if "] " in block else ""
                    m = _STUCK_RE.match(detail)
                    if m:
                        calls.append(
                            (
                                "message_agent",
                                {
                                    "agent_id": m.group(1),
                                    "body": self.stuck_message,
                                    "why": "The agent repeats itself.",
                                },
                            )
                        )
        if not calls:
            return await super().complete(req)
        # results to review in the same batch are accepted as well
        accept = await super().complete(req)
        calls += [(c.name, c.arguments) for c in accept.tool_calls if c.name != "noop"]
        usage = ModelUsage(input_tokens=50, output_tokens=10)
        resp = (
            tool_response(calls[0][0], **calls[0][1])
            if len(calls) == 1
            else multi_tool_response(*calls)
        )
        return resp.model_copy(update={"usage": usage})


class ResolvingBoard(MemoryBoard):
    """``MemoryBoard`` whose answers resolve the question they ref, at most once."""

    async def post(
        self, session_id: str, draft: BoardEntryDraft, *, author: Actor, step: int | None = None
    ) -> BoardEntry:
        question: BoardEntry | None = None
        if draft.kind == "answer":
            if len(draft.refs) != 1:
                raise BoardRuleError("an answer needs exactly one ref")
            question = await self.get(draft.refs[0])
            if question.kind != "question" or question.status != "open":
                raise BoardRuleError("an answer must ref an open question")
        entry = await super().post(session_id, draft, author=author, step=step)
        if question is not None:
            await self.set_status(question.id, "resolved", actor=author)
        return entry


@dataclass
class ScenarioWorld(World):
    """``World`` with the scripted agent model and the resolving board."""

    agent_model: ScriptedAgentModel = field(default_factory=ScriptedAgentModel)
    supervisor_model: ScriptedSupervisor = field(default_factory=ScriptedSupervisor)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.models.agent = self.agent_model
        self.models.supervisor = self.supervisor_model
        self.board = ResolvingBoard(self.recorder, self._agents)

    async def approved_session(
        self, edges: dict[str, Sequence[str]], *, agent_count: int = 1, **kw: Any
    ) -> str:
        """A session planned as ``edges`` (``make_tasks``) and approved; its id."""
        self.models.planner.plans = [make_tasks(edges)]
        session = await self.create_session(agent_count=agent_count, **kw)
        await self.send("command.plan_session", session.id)
        await self.wait_event(session.id, "session.plan_proposed")
        await self.send("command.approve_plan", session.id, plan_version=1)
        await self.wait_event(session.id, "session.approved")
        return session.id

    def sandbox_of(self, session_id: str, agent_id: str) -> FakeSandbox:
        ready = [
            payload(e, AgentSandboxReady)
            for e in self.events(session_id, "agent.sandbox_ready")
            if payload(e, AgentSandboxReady).agent_id == agent_id
        ]
        assert ready, f"agent {agent_id} has no sandbox"
        return self.provider.sandboxes[ready[-1].sandbox.sandbox_id]

    def agent_named(self, session_id: str, name: str) -> str:
        from agentplatform.contracts.events import AgentCreated

        for e in self.events(session_id, "agent.created"):
            agent = payload(e, AgentCreated).agent
            if agent.name == name:
                return agent.id
        raise AssertionError(f"no agent named {name}")


async def gate(event: asyncio.Event, response: ModelResponse) -> ModelResponse:
    """Hold a scripted step until ``event`` is set (orders steps across agents)."""
    await event.wait()
    return response


def submit(key: str, **outputs: Any) -> ModelResponse:
    return tool_response(
        "submit_result", summary=f"{key} done", outputs=outputs or {"result": f"{key} ok"}
    )


def shell(text: str) -> ModelResponse:
    return tool_response("run_command", cmd=f"echo {text}")


__all__ += ["gate", "shell", "submit", "text_response"]
