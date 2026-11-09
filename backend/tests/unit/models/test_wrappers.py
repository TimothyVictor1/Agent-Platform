"""Retry, concurrency, accounting and debug-logging wrappers (no network)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from agentplatform.config import ModelPrice
from agentplatform.contracts.common import HealthStatus
from agentplatform.contracts.model_io import (
    ImagePart,
    Message,
    ModelRequest,
    ModelResponse,
    ModelUsage,
    TextPart,
    ToolCall,
)
from agentplatform.errors import ModelError, ModelTimeoutError
from agentplatform.models.errors import RetryableModelError, parse_retry_after
from agentplatform.models.openai_compatible import OpenAICompatibleClient
from agentplatform.models.shared import SharedModelState
from agentplatform.models.wrappers import (
    MAX_LOG_DEPTH,
    AccountingClient,
    ConcurrencyLimitedClient,
    DebugLoggingClient,
    RetryingClient,
    compute_cost,
    redact_images,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "models"
JPEG = b"\xff\xd8\xff\xe0wrapper-jpeg"


class Gauge:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0


class Scripted:
    """A ModelClient that raises/returns queued items and records requests."""

    def __init__(
        self, *items: ModelResponse | Exception, delay: float = 0.0, gauge: Gauge | None = None
    ) -> None:
        self.items = list(items)
        self.requests: list[ModelRequest] = []
        self.delay = delay
        self.gauge = gauge or Gauge()
        self.closed = 0

    @property
    def name(self) -> str:
        return "scripted:test"

    async def complete(self, req: ModelRequest) -> ModelResponse:
        self.requests.append(req)
        self.gauge.active += 1
        self.gauge.peak = max(self.gauge.peak, self.gauge.active)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            item = self.items.pop(0) if self.items else ModelResponse(text="ok")
        finally:
            self.gauge.active -= 1
        if isinstance(item, Exception):
            raise item
        return item

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=1)

    async def aclose(self) -> None:
        self.closed += 1


class FakeTime:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def req(timeout_s: float = 120.0, *, image: bool = False) -> ModelRequest:
    content: list[Any] = [TextPart(text="hi")]
    if image:
        content.append(ImagePart(jpeg_bytes=JPEG, width=4, height=4))
    return ModelRequest(messages=[Message(role="user", content=content)], timeout_s=timeout_s)


def retrying(inner: Scripted | OpenAICompatibleClient, t: FakeTime, **kw: Any) -> RetryingClient:
    return RetryingClient(inner, sleep=t.sleep, clock=t.clock, rand=lambda: 1.0, **kw)


# --- RetryingClient --------------------------------------------------------------------


async def test_429_then_success_honours_retry_after_end_to_end() -> None:
    data = json.loads((FIXTURES / "openai_text.json").read_text())
    limited = json.loads((FIXTURES / "openai_rate_limited.json").read_text())
    calls: list[httpx2.Request] = []
    replies = [
        httpx2.Response(429, json=limited, headers={"retry-after": "2"}),
        httpx2.Response(200, json=data),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(request)
        return replies.pop(0)

    provider = OpenAICompatibleClient(
        base_url="http://llm.local/v1",
        model="m",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    t = FakeTime()
    client = retrying(provider, t, base_delay=0.5)
    resp = await client.complete(req())
    assert resp.text == "The page shows a login form."
    assert len(calls) == 2
    assert t.sleeps == [2.0 + 0.5]  # Retry-After plus at most base_delay of jitter
    await client.aclose()


async def test_exponential_backoff_and_gives_up_after_four_attempts() -> None:
    errors = [RetryableModelError("busy", status=503) for _ in range(5)]
    inner = Scripted(*errors)
    t = FakeTime()
    client = retrying(inner, t, base_delay=0.5)
    with pytest.raises(RetryableModelError):
        await client.complete(req())
    assert len(inner.requests) == 4
    assert t.sleeps == [0.5, 1.0, 2.0]  # rand=1.0 -> the full cap each time


async def test_backoff_is_capped_and_jittered() -> None:
    inner = Scripted(*[RetryableModelError("busy") for _ in range(3)])
    t = FakeTime()
    client = RetryingClient(
        inner, sleep=t.sleep, clock=t.clock, rand=lambda: 0.25, base_delay=10, max_delay=12
    )
    await client.complete(req(timeout_s=1000))
    assert t.sleeps == [2.5, 3.0, 3.0]


@pytest.mark.parametrize(
    "err",
    [ModelTimeoutError("t"), RetryableModelError("conn"), RetryableModelError("x", status=429)],
)
async def test_retryable_errors_are_retried(err: Exception) -> None:
    inner = Scripted(err, ModelResponse(text="done"))
    client = retrying(inner, FakeTime())
    assert (await client.complete(req())).text == "done"
    assert len(inner.requests) == 2


async def test_non_retryable_error_is_raised_immediately() -> None:
    inner = Scripted(ModelError("HTTP 400: bad"), ModelResponse(text="never"))
    t = FakeTime()
    with pytest.raises(ModelError, match="400"):
        await retrying(inner, t).complete(req())
    assert len(inner.requests) == 1
    assert t.sleeps == []


async def test_deadline_bounds_retries_and_attempt_timeouts() -> None:
    inner = Scripted(RetryableModelError("busy", retry_after=30), ModelResponse(text="x"))
    t = FakeTime()
    with pytest.raises(RetryableModelError):
        await retrying(inner, t).complete(req(timeout_s=10))
    assert t.sleeps == []  # a 30 s Retry-After cannot fit into a 10 s budget

    inner2 = Scripted(RetryableModelError("busy", retry_after=3), ModelResponse(text="x"))
    t2 = FakeTime()
    await retrying(inner2, t2, base_delay=0).complete(req(timeout_s=10))
    assert [r.timeout_s for r in inner2.requests] == [10, 7]


async def test_health_and_aclose_delegate() -> None:
    inner = Scripted()
    client = RetryingClient(inner)
    assert (await client.health()).ok
    await client.aclose()
    assert inner.closed == 1
    assert client.name == "scripted:test"


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"retry-after": "2"}, 2.0),
        ({"Retry-After": "1.5"}, 1.5),
        ({"retry-after-ms": "250", "retry-after": "9"}, 0.25),
        ({"retry-after": "-4"}, 0.0),
        ({"retry-after": "99999"}, 600.0),
        ({"retry-after": "Wed, 21 Oct 2015 07:28:10 GMT"}, 10.0),
        ({"retry-after": "soon"}, None),
        ({}, None),
    ],
)
def test_parse_retry_after(headers: dict[str, str], expected: float | None) -> None:
    now = 1445412480.0  # 07:28:00 GMT on that date
    assert parse_retry_after(headers, now=now) == expected


# --- ConcurrencyLimitedClient ----------------------------------------------------------


async def test_shared_semaphore_limits_all_clients_of_one_server() -> None:
    shared = SharedModelState()
    gauge = Gauge()
    inner_a, inner_b = Scripted(delay=0.02, gauge=gauge), Scripted(delay=0.02, gauge=gauge)
    a = ConcurrencyLimitedClient(inner_a, shared.semaphore("http://gpu/v1", 2))
    b = ConcurrencyLimitedClient(inner_b, shared.semaphore("http://gpu/v1", 5))  # first wins

    await asyncio.gather(*(c.complete(req()) for c in [a, b] * 4))
    assert gauge.peak == 2
    assert len(inner_a.requests) + len(inner_b.requests) == 8


async def test_waiting_for_a_slot_counts_against_the_timeout() -> None:
    sem = asyncio.Semaphore(1)
    slow = ConcurrencyLimitedClient(Scripted(delay=0.3), sem)
    fast = ConcurrencyLimitedClient(Scripted(), sem)
    task = asyncio.create_task(slow.complete(req()))
    await asyncio.sleep(0.01)
    with pytest.raises(ModelTimeoutError, match="no free model slot"):
        await fast.complete(req(timeout_s=0.05))
    await task
    assert not sem.locked()  # released after success and after timeout


async def test_slot_is_released_when_the_call_fails() -> None:
    sem = asyncio.Semaphore(1)
    client = ConcurrencyLimitedClient(Scripted(ModelError("boom")), sem)
    with pytest.raises(ModelError):
        await client.complete(req())
    assert not sem.locked()


# --- AccountingClient ------------------------------------------------------------------


def test_redact_images_handles_json_mode_dumps() -> None:
    dumped = req(image=True).model_dump(mode="json")
    digest = redact_images(dumped)["messages"][0]["content"][1]["jpeg_bytes"]
    assert digest == {"sha256": hashlib.sha256(JPEG).hexdigest(), "bytes": len(JPEG)}


def test_cost_math() -> None:
    price = ModelPrice(input=3.0, output=15.0, image=0.001)
    assert compute_cost(1_000_000, 0, 0, price) == pytest.approx(3.0)
    assert compute_cost(0, 1_000_000, 0, price) == pytest.approx(15.0)
    assert compute_cost(2000, 500, 2, price) == pytest.approx(0.006 + 0.0075 + 0.002)
    assert compute_cost(10**6, 10**6, 10, ModelPrice()) == 0.0  # local models are free


async def test_accounting_fills_cost_and_counts_images() -> None:
    inner = Scripted(
        ModelResponse(text="x", usage=ModelUsage(input_tokens=2000, output_tokens=500))
    )
    client = AccountingClient(inner, ModelPrice(input=3.0, output=15.0, image=0.001))
    resp = await client.complete(req(image=True))
    assert resp.usage.images == 1
    assert resp.usage.cost_usd == pytest.approx(0.006 + 0.0075 + 0.001)
    assert (resp.usage.input_tokens, resp.usage.output_tokens) == (2000, 500)


async def test_accounting_keeps_provider_image_count() -> None:
    inner = Scripted(ModelResponse(usage=ModelUsage(images=3)))
    resp = await AccountingClient(inner, ModelPrice(image=0.5)).complete(req())
    assert resp.usage.images == 3
    assert resp.usage.cost_usd == pytest.approx(1.5)


# --- DebugLoggingClient ----------------------------------------------------------------


def _records(directory: Path) -> list[dict[str, Any]]:
    files = list(directory.glob("*.jsonl"))
    assert len(files) == 1
    assert files[0].name.startswith("scripted_test-")
    return [json.loads(line) for line in files[0].read_text().splitlines()]


async def test_debug_log_hashes_images_and_records_errors(tmp_path: Path) -> None:
    inner = Scripted(ModelResponse(text="seen"), ModelError("HTTP 500: down"))
    client = DebugLoggingClient(inner, tmp_path / "model-debug")

    await client.complete(req(image=True))
    with pytest.raises(ModelError):
        await client.complete(req())

    ok, failed = _records(tmp_path / "model-debug")
    image = ok["request"]["messages"][0]["content"][1]
    assert image["jpeg_bytes"] == {"sha256": hashlib.sha256(JPEG).hexdigest(), "bytes": len(JPEG)}
    assert ok["response"]["text"] == "seen"
    assert failed["error"] == {"type": "ModelError", "message": "HTTP 500: down"}
    raw = "".join(p.read_text() for p in (tmp_path / "model-debug").glob("*.jsonl"))
    assert "/9j/" not in raw  # no base64 JPEG anywhere in the file


async def test_debug_log_never_contains_the_api_key(tmp_path: Path) -> None:
    data = json.loads((FIXTURES / "openai_tool_call.json").read_text())
    provider = OpenAICompatibleClient(
        base_url="http://llm.local/v1",
        model="m",
        api_key="sk-secret-value-123",
        http_client=httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda r: httpx2.Response(200, json=data))
        ),
    )
    client = DebugLoggingClient(provider, tmp_path)
    await client.complete(req(image=True))
    text = "".join(p.read_text() for p in sorted(tmp_path.iterdir()))  # noqa: ASYNC240
    assert "sk-secret-value-123" not in text
    assert '"click"' in text
    await client.aclose()


async def test_debug_log_failure_does_not_break_the_call(tmp_path: Path) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file in the way")
    client = DebugLoggingClient(Scripted(ModelResponse(text="fine")), blocker)
    assert (await client.complete(req())).text == "fine"


# --- review fixes (T02 review) ----------------------------------------------------------


def _deep(levels: int) -> dict[str, Any]:
    value: dict[str, Any] = {"leaf": True}
    for _ in range(levels):
        value = {"a": value}
    return value


def _depth(value: Any) -> int:
    n = 0
    while isinstance(value, dict) and "a" in value:
        value, n = value["a"], n + 1
    return n


def test_redact_images_caps_depth() -> None:
    out = redact_images(_deep(5000))  # was RecursionError
    assert _depth(out) == MAX_LOG_DEPTH  # the value at depth 64 is the marker
    inner = out
    for _ in range(MAX_LOG_DEPTH):
        inner = inner["a"]
    assert inner == "<max-depth>"
    assert redact_images([[["x"]]]) == [[["x"]]]  # shallow values are unchanged


async def test_debug_log_survives_a_deeply_nested_raw_response(tmp_path: Path) -> None:
    resp = ModelResponse(text="deep", raw=_deep(5000))
    client = DebugLoggingClient(Scripted(resp), tmp_path)
    assert (await client.complete(req())).text == "deep"  # was ValueError from model_dump
    (record,) = _records(tmp_path)
    assert record["response"]["text"] == "deep"
    assert "<max-depth>" in json.dumps(record)


async def test_debug_log_survives_deeply_nested_request_arguments(tmp_path: Path) -> None:
    call = ToolCall(id="c1", name="click", arguments=_deep(5000))
    deep_req = ModelRequest(
        messages=[
            Message.text("user", "hi"),
            Message(role="assistant", content=[], tool_calls=[call]),
            Message.tool_result("c1", "ok"),
        ]
    )
    client = DebugLoggingClient(Scripted(ModelResponse(text="fine")), tmp_path)
    assert (await client.complete(deep_req)).text == "fine"  # was RecursionError
    (record,) = _records(tmp_path)
    assert record["response"]["text"] == "fine"
    assert "<max-depth>" in json.dumps(record["request"])


async def test_debug_log_marks_an_unloggable_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: ModelResponse, **kw: Any) -> dict[str, Any]:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(ModelResponse, "model_dump", boom)
    client = DebugLoggingClient(Scripted(ModelResponse(text="fine")), tmp_path)
    assert (await client.complete(req())).text == "fine"
    (record,) = _records(tmp_path)
    assert record["response"] == {"unloggable": "RecursionError"}
