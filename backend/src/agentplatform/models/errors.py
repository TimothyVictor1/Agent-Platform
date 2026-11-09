"""Model-layer error helpers shared by the provider clients and the retry wrapper.

Provider clients never let SDK exceptions escape (``models/base.py``). They map them
here to:

* ``ModelTimeoutError`` for timeouts (retryable while time is left),
* ``RetryableModelError`` for 408/409/429/5xx and connection failures,
* ``ModelError`` for everything else (bad request, auth, unparseable response).

``RetryingClient`` decides only from these types, never from SDK classes.
"""

from __future__ import annotations

import email.utils
import math
import re
import time
from collections.abc import Mapping

import anthropic
import openai

from agentplatform.errors import ModelError, ModelTimeoutError

__all__ = [
    "RETRYABLE_STATUSES",
    "RetryableModelError",
    "is_retryable",
    "map_sdk_error",
    "parse_retry_after",
    "redact_secrets",
]

RETRYABLE_STATUSES = frozenset({408, 409, 429})
"""Plus every 5xx status."""

_MAX_DETAIL = 200
_MAX_RETRY_AFTER_S = 600.0

REDACTED = "[redacted]"
_KEY_NAMES = r"(?:x-)?api[_-]?key|key|access[_-]?token|token|secret|password"
_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer/Basic <credentials>
    (
        re.compile(
            r"""(?i)\b(authorization["']?\s*[:=]\s*["']?)(?:(?:bearer|basic|token)\s+)?[^\s"',;}]+"""
        ),
        rf"\1{REDACTED}",
    ),
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"), rf"\1{REDACTED}"),
    # OpenAI / Anthropic / OpenRouter style keys (sk-..., sk-ant-..., sk-or-v1-...)
    (re.compile(r"\bsk-[A-Za-z0-9_*.-]{4,}"), f"sk-{REDACTED}"),
    # Google API keys (Gemini)
    (re.compile(r"\bAIza[0-9A-Za-z_-]{16,}"), REDACTED),
    # api_key=..., "api_key": "...", x-api-key: ..., ?key=..., token=..., password: ...
    (
        re.compile(rf"""(?i)\b({_KEY_NAMES})(["']?\s*[:=]\s*["']?)[^\s"'&,;}}]+"""),
        rf"\1\2{REDACTED}",
    ),
)


def redact_secrets(text: str) -> str:
    """``text`` with API keys, bearer tokens and ``api_key=...``-style values replaced
    by ``[redacted]``. Applied to every provider message that can reach an error,
    a ``HealthStatus`` or the debug log (providers sometimes echo the key back)."""
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RetryableModelError(ModelError):
    """A transient provider failure; worth retrying (after ``retry_after`` if set)."""

    def __init__(
        self, detail: str, *, status: int | None = None, retry_after: float | None = None
    ) -> None:
        super().__init__(detail)
        self.http_status = status
        self.retry_after = retry_after


def is_retryable(err: BaseException) -> bool:
    return isinstance(err, RetryableModelError | ModelTimeoutError)


def parse_retry_after(headers: Mapping[str, str], *, now: float | None = None) -> float | None:
    """Seconds to wait from ``retry-after-ms`` / ``retry-after`` (seconds or HTTP date).

    Returns ``None`` when absent or unparseable; clamps to ``[0, 600]``.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    value: float | None = None
    if ms := lowered.get("retry-after-ms"):
        try:
            value = float(ms) / 1000.0
        except ValueError:
            value = None
    if value is None and (raw := lowered.get("retry-after")):
        try:
            value = float(raw)
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(raw).timestamp()
            except (TypeError, ValueError, IndexError):
                return None
            value = when - (time.time() if now is None else now)
    if value is None or math.isnan(value):
        return None
    return min(max(value, 0.0), _MAX_RETRY_AFTER_S)


def _short(text: str) -> str:
    text = redact_secrets(" ".join(text.split()))
    return text if len(text) <= _MAX_DETAIL else text[: _MAX_DETAIL - 1] + "…"


def map_sdk_error(provider: str, err: Exception) -> ModelError:
    """Turn an ``openai``/``anthropic`` SDK exception into a platform error.

    Messages keep only the HTTP status and a truncated, secret-redacted provider
    message; request headers (which hold the API key) are never included.
    """
    if isinstance(err, ModelError):
        return err
    if isinstance(err, openai.APITimeoutError | anthropic.APITimeoutError | TimeoutError):
        return ModelTimeoutError(f"{provider}: request timed out")
    if isinstance(err, openai.APIConnectionError | anthropic.APIConnectionError):
        return RetryableModelError(f"{provider}: connection failed")
    if isinstance(err, openai.APIStatusError | anthropic.APIStatusError):
        status = err.status_code
        detail = f"{provider}: HTTP {status}: {_short(err.message)}"
        if status in RETRYABLE_STATUSES or status >= 500:
            return RetryableModelError(
                detail, status=status, retry_after=parse_retry_after(err.response.headers)
            )
        return ModelError(detail)
    return ModelError(f"{provider}: {type(err).__name__}: {_short(str(err))}")
