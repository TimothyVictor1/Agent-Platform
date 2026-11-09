"""Public API adapter only: no runtime, database, model or sandbox imports."""

import asyncio
from collections.abc import Callable
from hashlib import sha256
from time import monotonic
from urllib.parse import quote

import httpx
from agentplatform.contracts.api import (
    ApprovalDecisionRequest,
    ApprovalList,
    ApprovePlanRequest,
    ArtifactList,
    CommandAccepted,
    CreateSessionRequest,
    EventPage,
    SessionDetail,
)
from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.events import Event
from agentplatform.contracts.session import Session
from agentplatform.errors import AppError, InvalidRequestError
from pydantic import BaseModel

from evals.runner.metrics import ordered_events


def segment(value: str) -> str:
    return quote(value, safe="")


class API:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client

    async def request(self, method: str, path: str, body: BaseModel | None = None) -> bytes:
        try:
            response = await self.client.request(
                method, path, json=body.model_dump(mode="json") if body else None
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # Do not echo URLs, response bodies or headers (may contain secrets).
            raise AppError("evaluation API request failed") from exc
        return response.content

    async def create(self, request: CreateSessionRequest) -> Session:
        return Session.model_validate_json(await self.request("POST", "sessions", request))

    async def detail(self, session_id: str) -> SessionDetail:
        return SessionDetail.model_validate_json(
            await self.request("GET", f"sessions/{segment(session_id)}")
        )

    async def stop(self, session_id: str) -> None:
        CommandAccepted.model_validate_json(
            await self.request("POST", f"sessions/{segment(session_id)}/stop")
        )

    async def wait(
        self,
        session_id: str,
        timeout_seconds: float,
        poll: float,
        approve_fixture_forms: bool = False,
        clock: Callable[[], float] = monotonic,
    ) -> Session:
        deadline = clock() + timeout_seconds
        approved_versions: set[int] = set()
        decided: set[str] = set()
        try:
            while clock() < deadline:
                detail = await self.detail(session_id)
                session = detail.session
                if session.status in {"completed", "failed", "stopped"}:
                    return session
                if session.status == "awaiting_approval":
                    version = session.plan_version
                    if version not in approved_versions:
                        CommandAccepted.model_validate_json(
                            await self.request(
                                "POST",
                                f"sessions/{segment(session_id)}/approve",
                                ApprovePlanRequest(plan_version=version),
                            )
                        )
                        approved_versions.add(version)
                if approve_fixture_forms:
                    pending = ApprovalList.model_validate_json(
                        await self.request(
                            "GET", f"sessions/{segment(session_id)}/approvals?status=pending"
                        )
                    )
                    for approval in pending.items:
                        if approval.status != "pending" or approval.id in decided:
                            continue
                        # Only fixture form tasks opt in; no other eval auto-approves actions.
                        CommandAccepted.model_validate_json(
                            await self.request(
                                "POST",
                                f"approvals/{segment(approval.id)}/decide",
                                ApprovalDecisionRequest(
                                    decision="approve", note="Local fixture evaluation approval"
                                ),
                            )
                        )
                        decided.add(approval.id)
                await asyncio.sleep(min(poll, max(0, deadline - clock())))
        except asyncio.CancelledError:
            await asyncio.shield(self.stop(session_id))
            raise
        await self.stop(session_id)
        raise AppError("evaluation timed out; stop requested")

    async def events(self, session_id: str) -> list[Event]:
        events: list[Event] = []
        cursor = 0
        while True:
            page = EventPage.model_validate_json(
                await self.request(
                    "GET", f"sessions/{segment(session_id)}/events?after_seq={cursor}&limit=1000"
                )
            )
            if any(event.session_id != session_id or event.seq <= cursor for event in page.items):
                raise InvalidRequestError("invalid evaluation event page")
            events.extend(page.items)
            next_cursor = page.next_after_seq
            if next_cursor is None:
                return ordered_events(events)
            if (
                next_cursor <= cursor
                or not page.items
                or next_cursor != max(event.seq for event in page.items)
            ):
                raise InvalidRequestError("evaluation event cursor did not advance")
            cursor = next_cursor

    async def artifacts(self, session_id: str) -> dict[str, bytes]:
        listing = ArtifactList.model_validate_json(
            await self.request("GET", f"sessions/{segment(session_id)}/artifacts")
        )
        # Use names only as mapping keys, never as host filesystem paths.
        latest: dict[str, Artifact] = {}
        for item in listing.items:
            if item.session_id != session_id:
                raise InvalidRequestError("artifact belongs to another session")
            previous = latest.get(item.name)
            if previous is None or (item.version, item.created_at, item.id) > (
                previous.version,
                previous.created_at,
                previous.id,
            ):
                latest[item.name] = item
        data: dict[str, bytes] = {}
        for name, item in latest.items():
            if item.size_bytes > 50_000_000:
                raise InvalidRequestError("evaluation artifact exceeds size limit")
            content = await self.request("GET", f"artifacts/{segment(item.id)}/download")
            if len(content) != item.size_bytes:
                raise InvalidRequestError("artifact size mismatch")
            if sha256(content).hexdigest() != item.sha256:
                raise InvalidRequestError("artifact hash mismatch")
            data[name] = content
        return data
