"""Shared lookups and state checks for the routers."""

from __future__ import annotations

from collections.abc import Collection

from sqlalchemy.ext.asyncio import AsyncSession

from agentplatform.contracts.common import SessionStatus
from agentplatform.contracts.session import Session
from agentplatform.db.repositories import PlanRepository, SessionRepository
from agentplatform.errors import ConflictError

__all__ = [
    "ACTIVE",
    "EDITABLE_PLAN",
    "TERMINAL",
    "current_plan_version",
    "load_session",
    "require_status",
]

TERMINAL: frozenset[SessionStatus] = frozenset({"completed", "failed", "stopped"})
ACTIVE: frozenset[SessionStatus] = frozenset({"running", "paused"})
EDITABLE_PLAN: frozenset[SessionStatus] = frozenset({"planning", "awaiting_approval"})


async def load_session(tx: AsyncSession, session_id: str) -> Session:
    """The session, or ``NotFoundError`` (404)."""
    return await SessionRepository(tx).get(session_id)


def require_status(session: Session, allowed: Collection[SessionStatus], action: str) -> None:
    """``ConflictError`` (409) unless the session is in one of ``allowed``."""
    if session.status not in allowed:
        raise ConflictError(f"cannot {action} while the session is {session.status}")


async def current_plan_version(tx: AsyncSession, session: Session) -> int:
    """Version of the current plan: the newest stored plan, 0 when there is none."""
    latest = await PlanRepository(tx).find(session.id)
    return max(session.plan_version, latest.version if latest is not None else 0)
