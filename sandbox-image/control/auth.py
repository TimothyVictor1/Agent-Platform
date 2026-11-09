"""Bearer authentication for every control-server endpoint.

Fails closed: with no CONTROL_TOKEN configured, all requests are rejected.
Comparison is constant-time so response timing cannot reveal the token.
"""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException

from .config import CONTROL_TOKEN


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_bearer(
    authorization: str | None = Header(default=None, include_in_schema=False),
) -> None:
    if CONTROL_TOKEN is None:
        raise _unauthorized("control server is locked: CONTROL_TOKEN is not configured")
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized("missing bearer token")
    supplied = authorization[len("Bearer ") :].strip()
    if not supplied or not hmac.compare_digest(
        supplied.encode("utf-8"), CONTROL_TOKEN.encode("utf-8")
    ):
        raise _unauthorized("invalid bearer token")
