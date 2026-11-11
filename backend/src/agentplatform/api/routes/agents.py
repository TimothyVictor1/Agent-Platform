"""Agent takeover/release (SPEC §16 "Agents", §15.2)."""

from __future__ import annotations

from fastapi import APIRouter, status

from agentplatform.api.deps import Deps, Tx
from agentplatform.api.routes._common import load_session, require_session_control
from agentplatform.contracts.api import CommandAccepted
from agentplatform.db.repositories import AgentRepository
from agentplatform.errors import ConflictError

__all__ = ["router"]

router = APIRouter(tags=["agents"])


@router.post(
    "/agents/{agent_id}/takeover",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def takeover(agent_id: str, tx: Tx, deps: Deps) -> CommandAccepted:
    agent = await AgentRepository(tx).get(agent_id)
    session = await load_session(tx, agent.session_id)
    require_session_control(session, "take over an agent")
    if agent.status == "paused_by_user":
        raise ConflictError(f"agent {agent.name} is already taken over")
    if agent.status == "stopped":
        raise ConflictError(f"agent {agent.name} has stopped")
    return await deps.commands.issue("command.takeover", agent.session_id, agent_id=agent.id)


@router.post(
    "/agents/{agent_id}/release",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def release(agent_id: str, tx: Tx, deps: Deps) -> CommandAccepted:
    agent = await AgentRepository(tx).get(agent_id)
    session = await load_session(tx, agent.session_id)
    require_session_control(session, "release an agent")
    if agent.status != "paused_by_user":
        raise ConflictError(f"agent {agent.name} is not taken over")
    return await deps.commands.issue("command.release", agent.session_id, agent_id=agent.id)
