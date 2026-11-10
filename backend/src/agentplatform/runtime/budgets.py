"""Budget helpers for human-in-the-loop and safety (SPEC §12.8, §15.1; T18).

The session-wide accounting lives in ``runtime.scheduler.SessionBudget`` (one per
session, shared by every agent runtime) and the scheduler emits
``session.budget_warning`` at 80 % and ``session.budget_exceeded`` plus the budget
stop at 100 %. This module does not duplicate that; it builds on the public
``SessionBudget`` API (``record``, ``exceeded``, ``metrics``):

* ``effective_budgets`` applies the policy default cost limit to a session's budgets;
* ``AgentBudget`` is a per-agent ``BudgetMonitor`` view: every step is recorded in
  the shared ``SessionBudget`` (so the session stop applies to all agents at once)
  and in a per-agent tally (for metrics and the supervisor);
* ``budget_level`` reports the most-used budget as a ratio (UI, supervisor).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from agentplatform.config import PoliciesConfig
from agentplatform.contracts.events import BudgetMetric
from agentplatform.contracts.session import Budgets, Usage
from agentplatform.runtime.scheduler import SessionBudget

__all__ = [
    "WARNING_RATIO",
    "AgentBudget",
    "BudgetLevel",
    "budget_level",
    "effective_budgets",
]

WARNING_RATIO = 0.8
"""Same threshold as ``SchedulerConfig.budget_warning_ratio`` (SPEC §12.8)."""


def effective_budgets(budgets: Budgets, policies: PoliciesConfig) -> Budgets:
    """``budgets`` with ``policies.budgets.default_max_cost_usd`` filled in when the
    session sets no cost limit. An explicit session limit always wins."""
    default = policies.budgets.default_max_cost_usd
    if budgets.max_cost_usd is not None or default is None:
        return budgets
    return budgets.model_copy(update={"max_cost_usd": default})


class AgentBudget:
    """``BudgetMonitor`` for one agent over the session's shared ``SessionBudget``.

    ``record`` adds the step to the shared budget and to ``usage`` (this agent's
    share); ``exceeded`` is the session's verdict, so when one agent's step uses the
    last of the budget every agent stops at its next budget check.
    """

    def __init__(self, shared: SessionBudget, agent_id: str) -> None:
        self.shared = shared
        self.agent_id = agent_id
        self.usage = Usage()

    def record(self, usage: Usage) -> None:
        self.shared.record(usage)
        self.usage = self.usage.plus(usage)

    def exceeded(self) -> str | None:
        return self.shared.exceeded()


@dataclass(frozen=True, slots=True)
class BudgetLevel:
    metric: BudgetMetric
    used: float
    limit: float
    ratio: float
    state: Literal["ok", "warning", "exceeded"]


def budget_level(budget: SessionBudget, warning_ratio: float = WARNING_RATIO) -> BudgetLevel | None:
    """The budget closest to its limit (``None`` if no metric has a positive limit)."""
    best: BudgetLevel | None = None
    for metric, used, limit in budget.metrics():
        if limit <= 0:
            continue
        ratio = used / limit
        if best is None or ratio > best.ratio:
            state: Literal["ok", "warning", "exceeded"] = (
                "exceeded" if ratio >= 1 else "warning" if ratio >= warning_ratio else "ok"
            )
            best = BudgetLevel(metric, used, limit, ratio, state)
    return best
