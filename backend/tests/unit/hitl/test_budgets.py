"""Budgets (SPEC §12.8, §15.1): policy default, per-agent view over the shared
SessionBudget, levels, and the budget stop of running agents."""

from __future__ import annotations

from agentplatform.config import BudgetsPolicy, PoliciesConfig
from agentplatform.contracts.session import Budgets, Usage
from agentplatform.eventing import EventRecorder
from agentplatform.runtime.agent.loop import AgentRuntime, AgentRuntimeConfig
from agentplatform.runtime.budgets import AgentBudget, budget_level, effective_budgets
from agentplatform.runtime.scheduler import SessionBudget
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

STEP = Usage(steps=1, input_tokens=100, output_tokens=10, cost_usd=0.5)


def test_policy_default_cost_applies_only_without_a_session_limit() -> None:
    policies = PoliciesConfig()  # default_max_cost_usd = 5.0
    assert effective_budgets(Budgets(), policies).max_cost_usd == 5.0
    assert effective_budgets(Budgets(max_cost_usd=1.0), policies).max_cost_usd == 1.0
    none = PoliciesConfig(budgets=BudgetsPolicy(default_max_cost_usd=None))
    assert effective_budgets(Budgets(), none).max_cost_usd is None


def test_agent_budgets_share_the_session_budget() -> None:
    shared = SessionBudget(Budgets(max_total_steps=100, max_cost_usd=2.0))
    a, b = AgentBudget(shared, "a"), AgentBudget(shared, "b")
    a.record(STEP)
    b.record(STEP)
    b.record(STEP)
    assert shared.usage.steps == 3
    assert (a.usage.steps, b.usage.steps) == (1, 2)
    assert a.exceeded() is None
    level = budget_level(shared)
    assert level is not None
    assert (level.metric, level.state, level.ratio) == ("cost_usd", "ok", 0.75)
    a.record(STEP)  # 2.0 of 2.0
    assert a.exceeded() == b.exceeded() == "cost_usd budget (2) reached"
    level = budget_level(shared)
    assert level is not None
    assert level.state == "exceeded"


def test_budget_level_warning_at_80_percent() -> None:
    shared = SessionBudget(Budgets(max_total_steps=10))
    for _ in range(8):
        shared.record(Usage(steps=1))
    level = budget_level(shared)
    assert level is not None
    assert (level.metric, level.state) == ("total_steps", "warning")


def _runtime(shared: SessionBudget, index: int, model: FakeModel) -> AgentRuntime:
    session = make_session(agent_count=2, budgets=shared.budgets)
    agent = make_agent(index, session_id=session.id)
    store, bus = InMemoryEventStore(), InMemoryBus()
    return AgentRuntime(
        session=session,
        agent=agent,
        sandbox=FakeSandbox(),
        model=model,
        events=EventRecorder(store, bus),
        bus=bus,
        frames=InMemoryArtifactStore(),
        config=AgentRuntimeConfig(error_backoff_s=0.0),
        budget=AgentBudget(shared, agent.id),
    )


async def test_one_agent_using_the_budget_stops_the_others() -> None:
    shared = SessionBudget(Budgets(max_total_steps=3))
    busy = FakeModel(default=tool_response("run_command", cmd="echo hi"))
    first = await _runtime(shared, 0, busy).run_task(make_task())
    assert (first.status, first.reason, first.steps) == ("failed", "budget", 3)
    idle = FakeModel(default=tool_response("run_command", cmd="echo hi"))
    second = await _runtime(shared, 1, idle).run_task(make_task())
    assert (second.status, second.reason, second.steps) == ("failed", "budget", 0)
    assert idle.calls == []  # stopped before asking the model
