"""RFC 7807 problem+json responses for every error the API returns (SPEC §16).

``AppError`` subclasses map to their ``status`` and ``code``. Request validation
errors become 422 with one ``ProblemError`` per field (``loc`` as FastAPI reports it,
e.g. ``["body", "tasks", 0, "key"]``). Anything unexpected becomes a generic 500 whose
body never contains the exception text (it may hold secrets or internals).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import JsonValue
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentplatform.contracts.api import Problem, ProblemError
from agentplatform.contracts.plan import PlanError
from agentplatform.errors import AppError, PlanValidationError

__all__ = [
    "PROBLEM_CONTENT_TYPE",
    "install_problem_handlers",
    "plan_validation_error",
    "problem_response",
    "problem_type",
]

log = structlog.get_logger(__name__)

PROBLEM_CONTENT_TYPE: Final = "application/problem+json"

_HTTP_TITLES: Final[dict[int, str]] = {
    400: "Bad request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Payload too large",
    415: "Unsupported media type",
    422: "Invalid request",
    429: "Too many requests",
    500: "Internal server error",
    501: "Not supported",
    502: "Bad gateway",
    503: "Service unavailable",
    504: "Gateway timeout",
}


def problem_type(code: str) -> str:
    """The ``type`` URI for a machine-readable error code."""
    return f"urn:agentplatform:problem:{code}"


def problem_response(
    status: int,
    *,
    code: str | None = None,
    title: str | None = None,
    detail: str | None = None,
    errors: Sequence[ProblemError] | None = None,
    instance: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = Problem(
        type=problem_type(code) if code else "about:blank",
        title=title or _HTTP_TITLES.get(status, "Error"),
        status=status,
        detail=detail,
        instance=instance,
        errors=list(errors) if errors is not None else None,
    )
    return JSONResponse(
        body.model_dump(mode="json"),
        status_code=status,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def plan_validation_error(errors: Sequence[PlanError]) -> PlanValidationError:
    """A ``PlanValidationError`` carrying ``PlanError`` dumps (rendered per task)."""
    n = len(errors)
    return PlanValidationError(
        f"The plan has {n} error{'s' if n != 1 else ''}.",
        errors=[e.model_dump(mode="json") for e in errors],
    )


def _json_details(raw: dict[str, Any], skip: set[str]) -> dict[str, JsonValue] | None:
    extra = {k: v for k, v in raw.items() if k not in skip and v is not None}
    return extra or None


def _app_error_items(err: AppError) -> list[ProblemError] | None:
    if err.errors is None:
        return None
    items: list[ProblemError] = []
    for raw in err.errors:
        message = str(raw.get("message") or raw.get("msg") or err.detail)
        code = raw.get("code")
        task_key = raw.get("task_key")
        field = raw.get("field")
        loc = raw.get("loc")
        if loc is None and field is not None:
            loc = [str(field)]
        items.append(
            ProblemError(
                message=message,
                code=str(code) if code is not None else None,
                task_key=str(task_key) if task_key is not None else None,
                loc=[p if isinstance(p, int) else str(p) for p in loc] if loc else None,
                details=_json_details(raw, {"message", "msg", "code", "task_key", "field", "loc"}),
            )
        )
    return items


async def _app_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    status = type(exc).status
    if status >= 500:
        # 5xx AppErrors are expected failures (provider down, ...); their message is
        # written to be user-safe, but keep the log line for operators.
        log.warning("request_failed", code=type(exc).code, status=status)
    return problem_response(
        status,
        code=type(exc).code,
        detail=exc.detail,
        errors=_app_error_items(exc),
        instance=request.url.path,
    )


def _validation_items(errors: Sequence[Any]) -> list[ProblemError]:
    items: list[ProblemError] = []
    for err in errors:
        loc = [p if isinstance(p, int) else str(p) for p in err.get("loc", ())]
        items.append(
            ProblemError(message=str(err.get("msg", "invalid")), code=err.get("type"), loc=loc)
        )
    return items


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return problem_response(
        422,
        code="invalid_request",
        detail="The request is invalid.",
        errors=_validation_items(exc.errors()),
        instance=request.url.path,
    )


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    detail = exc.detail if isinstance(exc.detail, str) else None
    return problem_response(
        exc.status_code,
        detail=detail,
        instance=request.url.path,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    # Never echo the exception: its text may contain secrets or internals.
    log.error("unhandled_error", error_type=type(exc).__name__, path=request.url.path)
    return problem_response(
        500,
        code="internal_error",
        detail="An unexpected error occurred.",
        instance=request.url.path,
    )


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
