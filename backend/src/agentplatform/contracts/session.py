"""Session, budgets and usage (SPEC §6.1)."""

from __future__ import annotations

from pydantic import Field

from agentplatform.contracts.common import (
    Contract,
    SandboxProviderName,
    SessionMode,
    SessionStatus,
    UtcDatetime,
)

__all__ = ["Budgets", "Session", "Usage"]


class Budgets(Contract):
    """Hard limits for a session. ``None`` means unlimited for the optional ones."""

    max_steps_per_task: int = Field(default=150, ge=1, le=10_000)
    max_total_steps: int = Field(default=1500, ge=1, le=100_000)
    max_wall_minutes: int = Field(default=60, ge=1, le=24 * 60)
    max_cost_usd: float | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=1)


class Usage(Contract):
    """Accumulated usage of a session (or a single step, in ``agent.step``)."""

    steps: int = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    images: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)

    def plus(self, other: Usage) -> Usage:
        """Return the element-wise sum of two usages."""
        return Usage(
            steps=self.steps + other.steps,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            images=self.images + other.images,
            cost_usd=self.cost_usd + other.cost_usd,
        )


class Session(Contract):
    id: str
    user_id: str
    title: str = Field(max_length=200)
    prompt: str = Field(min_length=1, max_length=8000)
    status: SessionStatus
    agent_count: int = Field(ge=1, le=6)
    model_profile: str = Field(min_length=1)
    sandbox_provider: SandboxProviderName
    mode: SessionMode = "coordinated"
    plan_version: int = Field(default=0, ge=0)
    """Version of the current plan. 0 until the planner proposes the first plan."""
    budgets: Budgets = Field(default_factory=Budgets)
    usage: Usage = Field(default_factory=Usage)
    created_at: UtcDatetime
    started_at: UtcDatetime | None = None
    ended_at: UtcDatetime | None = None
