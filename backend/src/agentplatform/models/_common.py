"""Small helpers shared by the provider clients (internal)."""

from __future__ import annotations

import base64
import json
import time
from typing import Any
from urllib.parse import urlsplit

from agentplatform.contracts.common import HealthStatus
from agentplatform.contracts.model_io import ImagePart, Message, ModelRequest, ToolCall
from agentplatform.errors import ModelError
from agentplatform.models.errors import redact_secrets

HEALTH_ERROR_CHARS = 200


def count_images(req: ModelRequest) -> int:
    return sum(len(m.images()) for m in req.messages)


def b64(image: ImagePart) -> str:
    return base64.b64encode(image.jpeg_bytes).decode("ascii")


def check_images_only_in_user(msg: Message) -> None:
    if msg.role != "user" and msg.images():
        raise ModelError(f"images are only allowed in user messages, not {msg.role!r}")


def dump_arguments(call: ToolCall) -> str:
    """``call.arguments`` as strict JSON (no NaN/Infinity).

    Raises ``ModelError`` when they are not JSON data (bytes, sets, non-finite
    numbers, too deep), so a bad call in the history fails as a typed error before
    any request is sent.
    """
    try:
        return json.dumps(call.arguments, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        raise ModelError(f"tool call {call.id!r} ({call.name!r}) has non-JSON arguments") from None


def parse_arguments(raw: Any) -> dict[str, Any]:
    """Tool-call arguments as a dict; invalid (or too deeply nested) JSON becomes
    ``{"_raw": text}``."""
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}
    if isinstance(raw, str):
        text = raw
    else:
        try:
            text = json.dumps(raw)
        except (TypeError, ValueError, RecursionError):
            return {"_raw": f"<unserializable {type(raw).__name__}>"}
    try:
        parsed = json.loads(text)
    except (ValueError, RecursionError):
        return {"_raw": text}
    if isinstance(parsed, dict):
        return {str(k): v for k, v in parsed.items()}
    return {"_raw": text}


def host_label(base_url: str | None) -> str:
    if not base_url:
        return "default"
    parts = urlsplit(base_url)
    return parts.netloc or base_url


def elapsed_ms(started: float) -> int:
    return max(0, round((time.perf_counter() - started) * 1000))


def health_failure(started: float, err: BaseException) -> HealthStatus:
    detail = redact_secrets(str(getattr(err, "detail", "") or err)) or type(err).__name__
    return HealthStatus(ok=False, latency_ms=elapsed_ms(started), error=detail[:HEALTH_ERROR_CHARS])
