"""Planner (SPEC §11): turns a request into a validated task DAG.

One planner call is: build the prompt (``prompts/planner_*.md``) -> ask the model for
a JSON object -> extract it tolerantly -> validate (Pydantic, then
``validate_plan`` and the mode's own rules) -> on any problem make exactly **one**
repair call that lists the exact problems -> if that fails too, raise
``PlannerError`` whose ``errors`` have the shape of ``PlanError`` dumps
(``code``, ``message``, ``task_key``, ``field``, ``cycle``). There is no silent
fallback.

Keys are normalised after validation:

* ``propose``: every task is renamed to ``T1..Tn`` in topological order.
* ``refine``: keys that exist in the current plan are kept (so the diff stays
  meaningful); new tasks get the next free ``T<n>`` keys in topological order.
* ``extend``: the model returns only the new tasks; they get the next free keys
  and are appended to the existing tasks, which are copied from the input and never
  from the model.

The agent count is passed to the model as a parallelism hint only; nothing here adds
or splits tasks to match it.

Planner-only error codes (not in ``contracts.plan.PlanErrorCode``): see
``PlannerErrorCode``.

Model errors (``ModelError``/``ModelTimeoutError``) propagate unchanged; the retry
wrapper around the client has already run. Logs carry counts, keys and error codes,
never prompt or reply text.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from string import Template
from typing import Any, Final, Literal

import structlog
from pydantic import ValidationError

from agentplatform.contracts.common import SuggestedTool, TaskStatus
from agentplatform.contracts.model_io import Message, ModelRequest
from agentplatform.contracts.plan import (
    MAX_TASKS,
    FieldChange,
    PlanDiff,
    PlanError,
    PlanProposal,
    TaskChange,
    topological_order,
    validate_plan,
)
from agentplatform.contracts.task import InputRef, TaskDraft
from agentplatform.errors import InvalidRequestError, PlannerError
from agentplatform.models.base import ModelClient

__all__ = [
    "ALL_TOOLS",
    "EXTEND_MAX_TOTAL_TASKS",
    "MAX_QUESTIONS",
    "PROMPTS_DIR",
    "PlanRevision",
    "Planner",
    "PlannerErrorCode",
    "PlannerSettings",
    "diff_plans",
    "extract_json_object",
    "next_key_number",
    "normalize_keys",
]

log = structlog.get_logger(__name__)

PROMPTS_DIR: Final = Path(__file__).parent / "prompts"

ALL_TOOLS: Final[tuple[SuggestedTool, ...]] = ("browser", "terminal", "office", "files")

MAX_QUESTIONS: Final = 3
"""``questions_for_user`` longer than this are trimmed (and logged), not repaired."""

EXTEND_MAX_TOTAL_TASKS: Final = MAX_TASKS
"""Cap on the merged plan after ``extend``. Today ``validate_plan`` enforces
``MAX_TASKS`` on the whole plan, so follow-ups share the 12-task budget. Once
``validate_plan`` accepts a limit (contracts patch), change this one line to allow up
to ``MAX_TASKS`` *new* tasks per follow-up."""

MAX_EXTRACT_CHARS: Final = 100_000
"""Replies longer than this are rejected before scanning (bounds the work)."""

MAX_EXTRACT_ATTEMPTS: Final = 50
"""At most this many ``{`` positions are tried by ``extract_json_object``."""

UNUSABLE_DEPENDENCY_STATUSES: Final[frozenset[TaskStatus]] = frozenset({"failed", "cancelled"})

PlannerErrorCode = Literal[
    "invalid_json",  # no JSON object could be extracted from the reply
    "invalid_plan",  # top-level field of the proposal failed Pydantic validation
    "invalid_task",  # a task failed Pydantic validation
    "key_collision",  # extend: a new task reuses an existing task's key
    "depends_on_unusable_task",  # extend: a new task depends on a failed/cancelled task
    "no_new_tasks",  # extend: the reply added no tasks
]
"""Codes the planner adds on top of ``PlanErrorCode`` in ``PlannerError.errors``."""

_DATA_TAGS: Final = (
    "user_request",
    "instruction",
    "follow_up",
    "current_plan",
    "existing_tasks",
)
_DATA_TAG_RE: Final = re.compile(r"<\s*/?\s*(?:" + "|".join(_DATA_TAGS) + r")\s*>", re.IGNORECASE)
_FENCE_RE: Final = re.compile(r"```[^\n`]*\n(.*?)(?:```|\Z)", re.DOTALL)
_SCAN_RE: Final = re.compile(r'[{}"\\]')
_T_KEY_RE: Final = re.compile(r"^T(\d{1,6})$")
_DIFF_FIELDS: Final = tuple(name for name in TaskDraft.model_fields if name != "key")


# --- settings and results ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannerSettings:
    max_output_tokens: int = 8192
    temperature: float = 0.0
    timeout_s: float = 180.0
    max_reply_chars_in_repair: int = 20_000
    """The invalid reply is echoed back in the repair call, clipped to this length."""
    max_errors_in_repair: int = 30


@dataclass(frozen=True, slots=True)
class PlanRevision:
    """Result of ``refine``/``extend``: the new full proposal and its diff against the
    plan it was derived from."""

    proposal: PlanProposal
    diff: PlanDiff


@dataclass(frozen=True, slots=True)
class _Issue:
    """One problem with a reply; same shape as ``PlanError``."""

    code: str
    message: str
    task_key: str | None = None
    field: str | None = None
    cycle: tuple[str, ...] | None = None

    @classmethod
    def from_plan_error(cls, err: PlanError) -> _Issue:
        return cls(
            code=err.code,
            message=err.message,
            task_key=err.task_key,
            field=err.field,
            cycle=tuple(err.cycle) if err.cycle is not None else None,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "task_key": self.task_key,
            "field": self.field,
            "cycle": list(self.cycle) if self.cycle is not None else None,
        }

    def render(self) -> str:
        where = f" (task {self.task_key})" if self.task_key else ""
        return f"[{self.code}]{where} {self.message}"


_Checked = PlanProposal | list[_Issue]


@dataclass(frozen=True, slots=True)
class _Prompts:
    system: str
    propose: Template
    refine: Template
    extend: Template
    repair: Template


def _load_prompts(directory: Path = PROMPTS_DIR) -> _Prompts:
    def read(name: str) -> str:
        return (directory / f"planner_{name}.md").read_text(encoding="utf-8")

    return _Prompts(
        system=read("system"),
        propose=Template(read("propose")),
        refine=Template(read("refine")),
        extend=Template(read("extend")),
        repair=Template(read("repair")),
    )


# --- pure helpers ---------------------------------------------------------------------


def extract_json_object(text: str) -> dict[str, Any]:
    """Return the first JSON object found in a model reply.

    Code fences are tried first (their bodies, in order), then the whole text. In each
    source every ``{`` is a candidate start; the matching ``}`` is found by a scan
    that respects JSON strings and backslash escapes, and the span must parse as a
    JSON object. Raises ``ValueError`` (with a message fit for the repair prompt) when
    nothing is found or the text exceeds ``MAX_EXTRACT_CHARS``.
    """
    if len(text) > MAX_EXTRACT_CHARS:
        raise ValueError(
            f"The reply is too long ({len(text)} characters; the limit is {MAX_EXTRACT_CHARS})."
        )
    sources = [m.group(1) for m in _FENCE_RE.finditer(text)]
    sources.append(text)
    attempts = 0
    for source in sources:
        start = source.find("{")
        while start != -1 and attempts < MAX_EXTRACT_ATTEMPTS:
            attempts += 1
            end = _balanced_end(source, start)
            if end is not None:
                try:
                    value: object = json.loads(source[start : end + 1])
                except json.JSONDecodeError:
                    value = None
                if isinstance(value, dict):
                    return value
            start = source.find("{", start + 1)
    raise ValueError("The reply does not contain a valid JSON object.")


def _balanced_end(source: str, start: int) -> int | None:
    """Index of the ``}`` closing the ``{`` at ``start``, or ``None``."""
    depth = 0
    in_string = False
    skip_before = -1
    for match in _SCAN_RE.finditer(source, start):
        i = match.start()
        if i < skip_before:
            continue  # the character after a backslash inside a string
        char = match.group()
        if in_string:
            if char == "\\":
                skip_before = i + 2
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def next_key_number(tasks: Iterable[TaskDraft]) -> int:
    """One more than the highest ``T<n>`` key in ``tasks`` (1 if there is none)."""
    numbers = [int(m.group(1)) for t in tasks if (m := _T_KEY_RE.match(t.key))]
    return max(numbers, default=0) + 1


def normalize_keys(
    tasks: Sequence[TaskDraft], *, keep: Iterable[str] = (), start: int = 1
) -> list[TaskDraft]:
    """Rename task keys to ``T<n>`` in topological order and rewrite references.

    Keys listed in ``keep`` keep their name and are never given to another task (pass
    the keys of tasks outside ``tasks`` here too, so new keys cannot collide with
    them). Other tasks get ``T<start>``, ``T<start+1>``, ... skipping taken names.
    ``depends_on`` (de-duplicated, order kept) and ``inputs.from_task`` are rewritten;
    references to keys outside ``tasks`` are left as they are. Tasks are returned in
    topological order.

    Precondition: ``tasks`` passed ``validate_plan`` (unique keys, no cycles).
    """
    reserved = set(keep)
    order = topological_order(tasks)
    mapping: dict[str, str] = {}
    taken = set(reserved)
    number = start
    for key in order:
        if key in reserved:
            mapping[key] = key
            continue
        while f"T{number}" in taken:
            number += 1
        mapping[key] = f"T{number}"
        taken.add(mapping[key])
        number += 1

    by_key = {t.key: t for t in tasks}
    renamed: list[TaskDraft] = []
    for key in order:
        task = by_key[key]
        depends_on = list(dict.fromkeys(mapping.get(d, d) for d in task.depends_on))
        inputs = [
            InputRef(from_task=mapping.get(i.from_task, i.from_task), output_key=i.output_key)
            for i in task.inputs
        ]
        renamed.append(
            task.model_copy(
                update={"key": mapping[key], "depends_on": depends_on, "inputs": inputs}
            )
        )
    return renamed


def diff_plans(before: Sequence[TaskDraft], after: Sequence[TaskDraft]) -> PlanDiff:
    """Differences matched by task key.

    ``added`` (in ``after`` order) are tasks whose key is new, ``removed`` (in
    ``before`` order) are keys that disappeared, and ``changed`` (in ``after`` order)
    lists every field whose JSON value differs, with before/after values.
    """
    old = {t.key: t for t in before}
    new_keys = {t.key for t in after}
    added = [t for t in after if t.key not in old]
    removed = [t.key for t in before if t.key not in new_keys]
    changed: list[TaskChange] = []
    for task in after:
        previous = old.get(task.key)
        if previous is None:
            continue
        a = previous.model_dump(mode="json")
        b = task.model_dump(mode="json")
        fields = [
            FieldChange(field=name, before=a[name], after=b[name])
            for name in _DIFF_FIELDS
            if a[name] != b[name]
        ]
        if fields:
            changed.append(TaskChange(key=task.key, fields=fields))
    return PlanDiff(added=added, removed=removed, changed=changed)


def _as_data(text: str) -> str:
    """Neutralise tags that would open or close a data block early."""
    return _DATA_TAG_RE.sub(lambda m: m.group(0).replace("<", "&lt;"), text)


def _loc(loc: Sequence[int | str]) -> str:
    out = ""
    for part in loc:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else str(part))
    return out


def _pydantic_issues(exc: ValidationError, raw: Mapping[str, Any]) -> list[_Issue]:
    raw_tasks = raw.get("tasks")
    issues: list[_Issue] = []
    for err in exc.errors(include_url=False, include_input=False, include_context=False):
        loc = err["loc"]
        path = _loc(loc)
        if len(loc) >= 2 and loc[0] == "tasks" and isinstance(loc[1], int):
            task_key: str | None = None
            if isinstance(raw_tasks, list) and loc[1] < len(raw_tasks):
                item = raw_tasks[loc[1]]
                key = item.get("key") if isinstance(item, dict) else None
                task_key = key if isinstance(key, str) and key else None
            issues.append(
                _Issue(
                    code="invalid_task",
                    message=f"{path}: {err['msg']}",
                    task_key=task_key,
                    field=str(loc[2]) if len(loc) >= 3 else None,
                )
            )
        else:
            issues.append(
                _Issue(
                    code="invalid_plan",
                    message=f"{path or 'reply'}: {err['msg']}",
                    field=str(loc[0]) if loc else None,
                )
            )
    return issues


def _bullets(items: Iterable[str], empty: str = "none") -> str:
    lines = [f"- {_as_data(i.strip())}" for i in items if i.strip()]
    return "\n".join(lines) if lines else f"- {empty}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n[... reply truncated ...]"


# --- the planner ----------------------------------------------------------------------


class Planner:
    """Proposes, refines and extends plans with one ``ModelClient`` (the planner role).

    Safe to use concurrently: it holds no per-call state.
    """

    def __init__(
        self,
        model: ModelClient,
        *,
        settings: PlannerSettings | None = None,
        prompts_dir: Path = PROMPTS_DIR,
    ) -> None:
        self._model = model
        self._settings = settings or PlannerSettings()
        self._prompts = _load_prompts(prompts_dir)

    # -- public API --------------------------------------------------------------------

    async def propose(
        self,
        prompt: str,
        *,
        agent_count_hint: int = 1,
        tools: Sequence[SuggestedTool] = ALL_TOOLS,
        constraints: Sequence[str] = (),
        session_id: str | None = None,
    ) -> PlanProposal:
        """A new plan for ``prompt`` with keys ``T1..Tn`` in topological order."""
        _require_text(prompt, "prompt")
        blog = _bind(session_id, mode="propose")
        user = self._prompts.propose.substitute(
            prompt=_as_data(prompt.strip()),
            **_common_vars(agent_count_hint, tools, constraints),
        )

        def check(text: str | None) -> _Checked:
            parsed = self._parse(text, tools, blog)
            if isinstance(parsed, list):
                return parsed
            errors = validate_plan(parsed.tasks)
            if errors:
                return [_Issue.from_plan_error(e) for e in errors]
            return parsed.model_copy(update={"tasks": normalize_keys(parsed.tasks)})

        return await self._run(user, check, blog)

    async def refine(
        self,
        plan: PlanProposal,
        instruction: str,
        *,
        agent_count_hint: int = 1,
        tools: Sequence[SuggestedTool] = ALL_TOOLS,
        constraints: Sequence[str] = (),
        session_id: str | None = None,
    ) -> PlanRevision:
        """A new full proposal following ``instruction``, plus its diff to ``plan``.

        Keys of tasks the model keeps are preserved; new tasks get keys after the
        highest existing ``T<n>`` (removed keys are never reused).
        """
        _require_text(instruction, "instruction")
        blog = _bind(session_id, mode="refine")
        old_keys = [t.key for t in plan.tasks]
        first = next_key_number(plan.tasks)
        user = self._prompts.refine.substitute(
            plan_json=_as_data(json.dumps(plan.model_dump(mode="json"), indent=2)),
            instruction=_as_data(instruction.strip()),
            next_keys=", ".join(f"T{n}" for n in range(first, first + 3)) + ", ...",
            **_common_vars(agent_count_hint, tools, constraints),
        )

        def check(text: str | None) -> _Checked:
            parsed = self._parse(text, tools, blog)
            if isinstance(parsed, list):
                return parsed
            errors = validate_plan(parsed.tasks)
            if errors:
                return [_Issue.from_plan_error(e) for e in errors]
            tasks = normalize_keys(parsed.tasks, keep=old_keys, start=first)
            return parsed.model_copy(update={"tasks": tasks})

        proposal = await self._run(user, check, blog)
        return PlanRevision(proposal=proposal, diff=diff_plans(plan.tasks, proposal.tasks))

    async def extend(
        self,
        plan: PlanProposal,
        follow_up: str,
        *,
        statuses: Mapping[str, TaskStatus] | None = None,
        agent_count_hint: int = 1,
        tools: Sequence[SuggestedTool] = ALL_TOOLS,
        constraints: Sequence[str] = (),
        session_id: str | None = None,
    ) -> PlanRevision:
        """Existing tasks (unchanged, copied from ``plan``) followed by new tasks for
        ``follow_up``. New tasks may depend on existing ones except those whose status
        in ``statuses`` is ``failed``/``cancelled``. The merged plan is capped at
        ``EXTEND_MAX_TOTAL_TASKS``."""
        _require_text(follow_up, "follow_up")
        blog = _bind(session_id, mode="extend")
        statuses = statuses or {}
        existing = list(plan.tasks)
        existing_by_key = {t.key: t for t in existing}
        room = EXTEND_MAX_TOTAL_TASKS - len(existing)
        if room <= 0:
            issue = _Issue(
                code="too_many_tasks",
                message=(
                    f"The plan already has {len(existing)} tasks; a follow-up cannot add "
                    f"more (the maximum is {EXTEND_MAX_TOTAL_TASKS})."
                ),
            )
            blog.warning("planner_plan_full", task_count=len(existing))
            raise PlannerError(issue.message, errors=[issue.as_dict()])
        first = next_key_number(existing)
        existing_json = [
            {
                "key": t.key,
                "status": statuses.get(t.key, "unknown"),
                **t.model_dump(mode="json", exclude={"key"}),
            }
            for t in existing
        ]
        user = self._prompts.extend.substitute(
            existing_json=_as_data(json.dumps(existing_json, indent=2)),
            follow_up=_as_data(follow_up.strip()),
            max_new=room,
            first_key=f"T{first}",
            **_common_vars(agent_count_hint, tools, constraints),
        )

        def check(text: str | None) -> _Checked:
            parsed = self._parse(text, tools, blog)
            if isinstance(parsed, list):
                return parsed
            new = [t for t in parsed.tasks if existing_by_key.get(t.key) != t]
            if len(new) < len(parsed.tasks):
                blog.info("planner_echoed_tasks_dropped", count=len(parsed.tasks) - len(new))
            issues: list[_Issue] = []
            if not new:
                issues.append(_Issue(code="no_new_tasks", message="The reply adds no new tasks."))
            for task in new:
                if task.key in existing_by_key:
                    issues.append(
                        _Issue(
                            code="key_collision",
                            task_key=task.key,
                            field="key",
                            message=(
                                f"Key {task.key!r} belongs to an existing task; new tasks "
                                f"need new keys starting at T{first}."
                            ),
                        )
                    )
                for dep in task.depends_on:
                    status = statuses.get(dep)
                    if dep in existing_by_key and status in UNUSABLE_DEPENDENCY_STATUSES:
                        issues.append(
                            _Issue(
                                code="depends_on_unusable_task",
                                task_key=task.key,
                                field="depends_on",
                                message=(
                                    f"Task {task.key} depends on existing task {dep}, "
                                    f"which is {status}."
                                ),
                            )
                        )
            if issues:
                return issues
            errors = validate_plan([*existing, *new])
            if errors:
                return [_Issue.from_plan_error(e) for e in errors]
            renamed = normalize_keys(new, keep=existing_by_key, start=first)
            return parsed.model_copy(update={"tasks": [*existing, *renamed]})

        proposal = await self._run(user, check, blog)
        return PlanRevision(proposal=proposal, diff=diff_plans(existing, proposal.tasks))

    # -- internals ---------------------------------------------------------------------

    async def _run(
        self,
        user_text: str,
        check: Callable[[str | None], _Checked],
        blog: structlog.typing.FilteringBoundLogger,
    ) -> PlanProposal:
        messages = [Message.text("system", self._prompts.system), Message.text("user", user_text)]
        reply = await self._complete(messages, blog, attempt=1)
        result = check(reply)
        if isinstance(result, PlanProposal):
            return self._done(result, blog, attempts=1)

        blog.warning("planner_reply_invalid", attempt=1, codes=_codes(result))
        repair = self._prompts.repair.substitute(errors=self._render_issues(result))
        echoed = _clip(reply, self._settings.max_reply_chars_in_repair) if reply else "(empty)"
        repair_messages = [
            *messages,
            Message.text("assistant", echoed),
            Message.text("user", repair),
        ]
        reply = await self._complete(repair_messages, blog, attempt=2)
        result = check(reply)
        if isinstance(result, PlanProposal):
            return self._done(result, blog, attempts=2)

        blog.warning("planner_failed", attempt=2, codes=_codes(result))
        summary = result[0].render() if result else "unknown problem"
        detail = (
            f"The planner could not produce a valid plan after one repair attempt "
            f"({len(result)} problem{'s' if len(result) != 1 else ''}; first: {summary})"
        )
        raise PlannerError(_clip(detail, 500), errors=[i.as_dict() for i in result])

    async def _complete(
        self,
        messages: list[Message],
        blog: structlog.typing.FilteringBoundLogger,
        *,
        attempt: int,
    ) -> str | None:
        s = self._settings
        blog.info("planner_request", attempt=attempt, model=self._model.name)
        response = await self._model.complete(
            ModelRequest(
                messages=messages,
                response_format="json",
                max_output_tokens=s.max_output_tokens,
                temperature=s.temperature,
                timeout_s=s.timeout_s,
            )
        )
        return response.text

    def _parse(
        self,
        text: str | None,
        tools: Sequence[SuggestedTool],
        blog: structlog.typing.FilteringBoundLogger,
    ) -> _Checked:
        if text is None or not text.strip():
            return [_Issue(code="invalid_json", message="The reply contained no text.")]
        try:
            raw = extract_json_object(text)
        except ValueError as exc:
            return [_Issue(code="invalid_json", message=str(exc))]
        questions = raw.get("questions_for_user")
        if isinstance(questions, list) and len(questions) > MAX_QUESTIONS:
            blog.info("planner_questions_trimmed", count=len(questions), kept=MAX_QUESTIONS)
            raw = {**raw, "questions_for_user": questions[:MAX_QUESTIONS]}
        try:
            proposal = PlanProposal.model_validate(raw)
        except ValidationError as exc:
            return _pydantic_issues(exc, raw)
        return self._filter_tools(proposal, tools, blog)

    @staticmethod
    def _filter_tools(
        proposal: PlanProposal,
        tools: Sequence[SuggestedTool],
        blog: structlog.typing.FilteringBoundLogger,
    ) -> PlanProposal:
        allowed = set(tools)
        tasks: list[TaskDraft] = []
        dropped = 0
        for task in proposal.tasks:
            kept = [t for t in task.suggested_tools if t in allowed]
            dropped += len(task.suggested_tools) - len(kept)
            tasks.append(
                task
                if len(kept) == len(task.suggested_tools)
                else task.model_copy(update={"suggested_tools": kept})
            )
        if not dropped:
            return proposal
        blog.info("planner_tools_filtered", dropped=dropped)
        return proposal.model_copy(update={"tasks": tasks})

    def _render_issues(self, issues: Sequence[_Issue]) -> str:
        limit = self._settings.max_errors_in_repair
        lines = [f"{n}. {issue.render()}" for n, issue in enumerate(issues[:limit], start=1)]
        if len(issues) > limit:
            lines.append(f"... and {len(issues) - limit} more problems of the same kinds.")
        return "\n".join(lines)

    @staticmethod
    def _done(
        proposal: PlanProposal,
        blog: structlog.typing.FilteringBoundLogger,
        *,
        attempts: int,
    ) -> PlanProposal:
        blog.info(
            "planner_plan_ready",
            attempts=attempts,
            task_count=len(proposal.tasks),
            keys=[t.key for t in proposal.tasks],
        )
        return proposal


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise InvalidRequestError(f"{name} must not be empty")


def _bind(session_id: str | None, *, mode: str) -> structlog.typing.FilteringBoundLogger:
    bound: structlog.typing.FilteringBoundLogger = log.bind(planner_mode=mode)
    return bound.bind(session_id=session_id) if session_id else bound


def _common_vars(
    agent_count_hint: int, tools: Sequence[SuggestedTool], constraints: Sequence[str]
) -> dict[str, str]:
    return {
        "agent_count": str(max(1, agent_count_hint)),
        "tools": ", ".join(tools) if tools else "none",
        "constraints": _bullets(constraints),
    }


def _codes(issues: Sequence[_Issue]) -> list[str]:
    return sorted({i.code for i in issues})
