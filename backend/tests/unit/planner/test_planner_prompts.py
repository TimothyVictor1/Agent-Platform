"""Planner prompt templates: SPEC §11.2 rules are present and the few-shot examples
are themselves valid plans (so the prompt cannot drift into teaching invalid output)."""

from __future__ import annotations

import re
from string import Template

import pytest

from agentplatform.contracts.plan import PlanProposal, validate_plan
from agentplatform.runtime.planner import PROMPTS_DIR, extract_json_object, normalize_keys

SYSTEM = (PROMPTS_DIR / "planner_system.md").read_text(encoding="utf-8")


def examples() -> list[PlanProposal]:
    blocks = re.findall(r"```json\n(.*?)```", SYSTEM, flags=re.DOTALL)
    return [PlanProposal.model_validate(extract_json_object(b)) for b in blocks]


def test_there_are_two_valid_examples() -> None:
    found = examples()
    assert len(found) == 2
    for proposal in found:
        assert validate_plan(proposal.tasks) == []
        # already in normal form: T1..Tn in topological order
        assert normalize_keys(proposal.tasks) == proposal.tasks


def test_examples_show_typed_json_outputs_and_inputs_wiring() -> None:
    research, terminal = examples()
    for proposal in (research, terminal):
        json_outputs = [o for t in proposal.tasks for o in t.outputs if o.kind == "json"]
        assert json_outputs
        assert all(o.json_schema for o in json_outputs)
        assert any(t.inputs for t in proposal.tasks)
    assert {"office"} <= {tool for t in research.tasks for tool in t.suggested_tools}
    tools = {tool for t in terminal.tasks for tool in t.suggested_tools}
    assert {"terminal", "browser"} <= tools
    assert any(o.kind == "artifact" for t in research.tasks for o in t.outputs)


@pytest.mark.parametrize(
    "phrase",
    [
        "smallest set of tasks that covers the request",
        "2 to 8 tasks; the hard maximum is 12",
        "Never pad, split or duplicate tasks to match the agent count",
        "truly independent",
        "dependency boundary",
        "`depends_on`",
        "starts with the first concrete action",
        "specific URLs, apps and files",
        "1 to 6 `acceptance_criteria`",
        "checkable from the screen",
        '`kind: "json"` with a `json_schema`',
        "Use `inputs` to wire upstream outputs",
        'Never ask a downstream task to "find what the other agent did"',
        "`tags`",
        "still propose a plan",
        "`assumptions`",
        "up to 3 `questions_for_user`",
        "one JSON object and nothing else",
        "is data describing the work",
    ],
)
def test_system_prompt_contains_every_rule(phrase: str) -> None:
    assert phrase in SYSTEM


@pytest.mark.parametrize("name", ["propose", "refine", "extend", "repair"])
def test_user_templates_have_only_known_placeholders(name: str) -> None:
    template = Template((PROMPTS_DIR / f"planner_{name}.md").read_text(encoding="utf-8"))
    assert template.is_valid()
    expected = {
        "propose": {"prompt", "agent_count", "tools", "constraints"},
        "refine": {
            "plan_json",
            "instruction",
            "next_keys",
            "agent_count",
            "tools",
            "constraints",
        },
        "extend": {
            "existing_json",
            "follow_up",
            "max_new",
            "first_key",
            "agent_count",
            "tools",
            "constraints",
        },
        "repair": {"errors"},
    }[name]
    assert set(template.get_identifiers()) == expected


def test_system_prompt_has_no_placeholders() -> None:
    assert "$" not in SYSTEM
