"""Approvals (SPEC §16 "Approvals", §15.2)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, status

from agentplatform.api.deps import Deps, Tx
from agentplatform.api.routes._common import load_session
from agentplatform.contracts.api import ApprovalDecisionRequest, ApprovalList, CommandAccepted
from agentplatform.contracts.common import ApprovalStatus
from agentplatform.db.repositories import ApprovalRepository
from agentplatform.errors import ConflictError

__all__ = ["router"]

router = APIRouter(tags=["approvals"])


@router.get("/sessions/{session_id}/approvals", response_model=ApprovalList)
async def list_approvals(
    session_id: str,
    tx: Tx,
    status_: Annotated[ApprovalStatus | None, Query(alias="status")] = None,
) -> ApprovalList:
    await load_session(tx, session_id)
    statuses = None if status_ is None else [status_]
    return ApprovalList(items=await ApprovalRepository(tx).list(session_id, statuses=statuses))


@router.post(
    "/approvals/{approval_id}/decide",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=CommandAccepted,
)
async def decide(
    approval_id: str, body: ApprovalDecisionRequest, tx: Tx, deps: Deps
) -> CommandAccepted:
    approval = await ApprovalRepository(tx).get(approval_id)
    if approval.status != "pending":
        raise ConflictError(f"approval {approval_id} is already {approval.status}")
    return await deps.commands.issue(
        "command.decide_approval",
        approval.session_id,
        approval_id=approval.id,
        decision=body.decision,
        note=body.note,
    )
