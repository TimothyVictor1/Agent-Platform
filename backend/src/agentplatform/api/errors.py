"""API-only error types (the shared ones live in ``agentplatform.errors``)."""

from __future__ import annotations

from agentplatform.errors import AppError

__all__ = ["ServiceUnavailableError"]


class ServiceUnavailableError(AppError):
    """A dependency (bus, store) is unreachable; the client may retry later."""

    status = 503
    code = "service_unavailable"
