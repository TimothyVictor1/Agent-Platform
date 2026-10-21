"""Typed exceptions used across the platform.

Every error the platform raises on purpose derives from ``AppError``. The API maps
``AppError`` subclasses to RFC 7807 problem+json responses using ``status`` and
``code``; agent tools turn them into ``ERROR: <message>`` strings for the model.

Messages must be safe to show to a user or a model: never put secrets, API keys or
raw image bytes into an exception message.
"""

from __future__ import annotations

from typing import Any, ClassVar


class AppError(Exception):
    """Base class for all expected platform errors.

    Attributes:
        status: HTTP status the API uses for this error.
        code: stable machine-readable slug, used as the problem+json ``type`` suffix.
        detail: human/model readable message (also ``str(err)``).
        errors: optional structured details (e.g. per-field or per-task errors).
    """

    status: ClassVar[int] = 500
    code: ClassVar[str] = "internal_error"

    def __init__(self, detail: str, *, errors: list[dict[str, Any]] | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.errors = errors


# --- generic API-facing errors -------------------------------------------------------


class NotFoundError(AppError):
    status = 404
    code = "not_found"


class ConflictError(AppError):
    """The request is valid but conflicts with the current state (e.g. approving twice)."""

    status = 409
    code = "conflict"


class InvalidRequestError(AppError):
    """Input failed validation that Pydantic alone cannot express."""

    status = 422
    code = "invalid_request"


class PlanValidationError(InvalidRequestError):
    """A plan failed ``validate_plan``; ``errors`` holds serialized ``PlanError`` items."""

    code = "plan_invalid"


class AuthError(AppError):
    status = 401
    code = "unauthorized"


class PayloadTooLargeError(AppError):
    status = 413
    code = "payload_too_large"


class RateLimitedError(AppError):
    """A caller exceeded a platform rate limit (e.g. board posts per agent)."""

    status = 429
    code = "rate_limited"


class NotSupportedError(AppError):
    """The provider or mode does not support this operation (e.g. pause on a provider)."""

    status = 501
    code = "not_supported"


# --- configuration -------------------------------------------------------------------


class ConfigError(AppError):
    """Invalid or missing configuration (env, models.yaml, policies.yaml)."""

    code = "config_error"


# --- models --------------------------------------------------------------------------


class ModelError(AppError):
    """A model provider call failed after retries (or with a non-retryable error)."""

    status = 502
    code = "model_error"


class ModelTimeoutError(ModelError):
    status = 504
    code = "model_timeout"


# --- sandboxes -----------------------------------------------------------------------


class SandboxError(AppError):
    """A sandbox operation failed (control server error, container gone, SDK error)."""

    status = 502
    code = "sandbox_error"


class SandboxExpiredError(SandboxError):
    """The sandbox reached its provider time limit or no longer exists."""

    status = 410
    code = "sandbox_expired"


# --- agent actions -------------------------------------------------------------------


class ActionError(AppError):
    """An agent action is invalid. The message is returned to the model as ``ERROR: ...``."""

    status = 422
    code = "action_error"


class CoordinateError(ActionError):
    """Model coordinates could not be mapped to sandbox pixels."""

    code = "coordinate_error"


class ToolArgumentsError(ActionError):
    """Tool call arguments did not match the tool's JSON schema."""

    code = "tool_arguments"


# --- runtime -------------------------------------------------------------------------


class PlannerError(AppError):
    """The planner could not produce a valid plan (after one repair attempt)."""

    status = 502
    code = "planner_error"


class BudgetExceededError(AppError):
    status = 409
    code = "budget_exceeded"


class LeaseLostError(AppError):
    """A lease (task, session) expired or is owned by someone else."""

    status = 409
    code = "lease_lost"


class ArtifactTooLargeError(PayloadTooLargeError):
    code = "artifact_too_large"


class BoardRuleError(InvalidRequestError):
    """A board post broke a kind-specific rule (e.g. answer to a resolved question)."""

    code = "board_rule"
