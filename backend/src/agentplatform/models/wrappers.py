"""Cross-cutting wrappers around any ``ModelClient`` (SPEC §8.1).

``build_client`` (factory.py) stacks them, outermost first::

    DebugLoggingClient -> AccountingClient -> RetryingClient
        -> ConcurrencyLimitedClient -> provider client

The semaphore sits inside the retry loop, so a request sleeping out a back-off does
not hold a slot that another agent could use. Every wrapper is a ``ModelClient`` and
is safe for concurrent use; ``health()``/``aclose()`` delegate to the inner client.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel

from agentplatform.config import ModelPrice
from agentplatform.contracts.common import HealthStatus
from agentplatform.contracts.model_io import ModelRequest, ModelResponse
from agentplatform.errors import ModelTimeoutError
from agentplatform.models.base import ModelClient
from agentplatform.models.errors import RetryableModelError, is_retryable, redact_secrets

__all__ = [
    "DEFAULT_DEBUG_DIR",
    "MAX_LOG_DEPTH",
    "AccountingClient",
    "ConcurrencyLimitedClient",
    "DebugLoggingClient",
    "RetryingClient",
    "compute_cost",
    "redact_images",
]

log = structlog.get_logger(__name__)

DEFAULT_DEBUG_DIR = Path("data/model-debug")
MAX_LOG_DEPTH = 64
"""Debug-log values nested deeper than this are replaced by ``"<max-depth>"``."""


class _Wrapper:
    def __init__(self, inner: ModelClient) -> None:
        self.inner = inner

    @property
    def name(self) -> str:
        return self.inner.name

    async def health(self) -> HealthStatus:
        return await self.inner.health()

    async def aclose(self) -> None:
        await self.inner.aclose()


# --- retries ---------------------------------------------------------------------------


class RetryingClient(_Wrapper):
    """Retries transient failures with exponential back-off and full jitter.

    * Retries ``RetryableModelError`` (408/409/429/5xx, connection errors) and
      ``ModelTimeoutError``; everything else is raised at once.
    * At most ``max_attempts`` attempts (default 4). Delay before attempt *n+1* is
      ``uniform(0, min(max_delay, base_delay * 2**(n-1)))``, or the provider's
      ``Retry-After`` when given (plus up to ``base_delay`` of jitter).
    * ``req.timeout_s`` bounds the whole call: each attempt gets the remaining time,
      and a back-off that would overrun the deadline is not taken (the last error is
      raised instead).
    """

    def __init__(
        self,
        inner: ModelClient,
        *,
        max_attempts: int = 4,
        base_delay: float = 0.5,
        max_delay: float = 20.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(inner)
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._max_attempts = max_attempts
        self._base = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        self._rand = rand
        self._clock = clock

    def _delay(self, attempt: int, err: BaseException) -> float:
        retry_after = err.retry_after if isinstance(err, RetryableModelError) else None
        if retry_after is not None:
            return retry_after + self._rand() * self._base
        return self._rand() * min(self._max_delay, self._base * 2.0 ** (attempt - 1))

    async def complete(self, req: ModelRequest) -> ModelResponse:
        deadline = self._clock() + req.timeout_s
        attempt = 0
        while True:
            attempt += 1
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise ModelTimeoutError(f"{self.name}: timed out after {attempt - 1} attempt(s)")
            try:
                return await self.inner.complete(req.model_copy(update={"timeout_s": remaining}))
            except Exception as err:
                if not is_retryable(err) or attempt >= self._max_attempts:
                    raise
                delay = self._delay(attempt, err)
                if self._clock() + delay >= deadline:
                    raise
                log.warning(
                    "model.retry",
                    client=self.name,
                    attempt=attempt,
                    delay_s=round(delay, 3),
                    error=type(err).__name__,
                    status=getattr(err, "http_status", None),
                )
                await self._sleep(delay)


# --- concurrency -----------------------------------------------------------------------


class ConcurrencyLimitedClient(_Wrapper):
    """At most N concurrent ``complete`` calls through ``semaphore``.

    The semaphore comes from ``SharedModelState.semaphore(key, limit)`` so all
    clients for one server share it. Waiting for a slot counts against
    ``req.timeout_s``; the inner call gets the time that is left.
    """

    def __init__(
        self,
        inner: ModelClient,
        semaphore: asyncio.Semaphore,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(inner)
        self._sem = semaphore
        self._clock = clock

    async def complete(self, req: ModelRequest) -> ModelResponse:
        started = self._clock()
        try:
            async with asyncio.timeout(req.timeout_s):
                await self._sem.acquire()
        except TimeoutError:
            raise ModelTimeoutError(f"{self.name}: no free model slot in time") from None
        try:
            remaining = max(0.001, req.timeout_s - (self._clock() - started))
            return await self.inner.complete(req.model_copy(update={"timeout_s": remaining}))
        finally:
            self._sem.release()


# --- accounting ------------------------------------------------------------------------


def compute_cost(input_tokens: int, output_tokens: int, images: int, price: ModelPrice) -> float:
    """USD: tokens priced per 1M, images per image (``ModelPrice`` units)."""
    return (
        input_tokens * price.input / 1_000_000
        + output_tokens * price.output / 1_000_000
        + images * price.image
    )


class AccountingClient(_Wrapper):
    """Fills ``usage.images`` (if the provider left it 0) and ``usage.cost_usd``."""

    def __init__(self, inner: ModelClient, price: ModelPrice) -> None:
        super().__init__(inner)
        self._price = price

    async def complete(self, req: ModelRequest) -> ModelResponse:
        resp = await self.inner.complete(req)
        usage = resp.usage
        images = usage.images or sum(len(m.images()) for m in req.messages)
        cost = compute_cost(usage.input_tokens, usage.output_tokens, images, self._price)
        return resp.model_copy(
            update={"usage": usage.model_copy(update={"images": images, "cost_usd": cost})}
        )


# --- debug logging ---------------------------------------------------------------------


def _image_digest(value: str | bytes) -> dict[str, Any]:
    if isinstance(value, bytes):
        raw = value
    else:  # JSON-mode dumps use URL-safe base64 (ser_json_bytes="base64")
        try:
            raw = base64.urlsafe_b64decode(value)
        except ValueError:
            raw = value.encode("utf-8", "replace")
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def redact_images(value: Any, depth: int = 0) -> Any:
    """Replace every ``jpeg_bytes`` (raw bytes, or base64 from a JSON-mode dump) by the
    sha256 and size of the image bytes. Values nested ``MAX_LOG_DEPTH`` deep become
    ``"<max-depth>"``, so hostile payloads cannot exhaust the stack."""
    if depth >= MAX_LOG_DEPTH:
        return "<max-depth>"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if k == "jpeg_bytes" and isinstance(v, str | bytes):
                out[k] = _image_digest(v)
            else:
                out[k] = redact_images(v, depth + 1)
        return out
    if isinstance(value, list):
        return [redact_images(v, depth + 1) for v in value]
    return value


def _snapshot(model: BaseModel) -> Any:
    """``model`` for the debug log (images hashed, depth capped); never raises."""
    try:
        return redact_images(model.model_dump())
    except (RecursionError, ValueError, TypeError) as err:
        return {"unloggable": type(err).__name__}


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name)[:120]


class DebugLoggingClient(_Wrapper):
    """Appends one JSONL record per call to ``<directory>/<client>-<date>.jsonl``.

    Records hold the request (images replaced by sha256 hashes), the response (or the
    error type and its message, secrets redacted) and timings. API keys never reach this layer:
    they live only inside the provider SDK client. Building and writing a record never
    fails the model call: payloads that cannot be dumped (too deep, unserializable)
    are logged as ``{"unloggable": <error type>}``, nesting is capped at
    ``MAX_LOG_DEPTH``, and writes run in a worker thread, one ``write()`` per record in
    append mode.
    """

    def __init__(self, inner: ModelClient, directory: Path = DEFAULT_DEBUG_DIR) -> None:
        super().__init__(inner)
        self._dir = directory

    def _write(self, record: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._dir / f"{_safe_name(self.name)}-{day}.jsonl"
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)

    async def _log(self, record: dict[str, Any]) -> None:
        try:
            await asyncio.to_thread(self._write, record)
        except (OSError, TypeError, ValueError, RecursionError) as err:
            log.warning("model.debug_log_failed", client=self.name, error=type(err).__name__)

    async def complete(self, req: ModelRequest) -> ModelResponse:
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "client": self.name,
            "request": _snapshot(req),
        }
        started = time.perf_counter()
        try:
            resp = await self.inner.complete(req)
        except Exception as err:
            message = redact_secrets(str(err))[:500]
            record["error"] = {"type": type(err).__name__, "message": message}
            record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
            await self._log(record)
            raise
        record["response"] = _snapshot(resp)
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000)
        await self._log(record)
        return resp
