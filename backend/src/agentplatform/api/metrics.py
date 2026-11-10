"""In-process counters for ``GET /api/v1/metrics`` (SPEC §19)."""

from __future__ import annotations

from agentplatform.contracts.api import MetricsResponse

__all__ = ["Metrics"]


class Metrics:
    """Monotonic counters keyed by dotted names, e.g. ``http.requests.2xx``.

    Mutated only from the event loop thread, so no lock is needed.
    """

    def __init__(self) -> None:
        self._counters: dict[str, float] = {}

    def inc(self, name: str, amount: float = 1.0) -> None:
        self._counters[name] = self._counters.get(name, 0.0) + amount

    def get(self, name: str) -> float:
        return self._counters.get(name, 0.0)

    def snapshot(self) -> MetricsResponse:
        return MetricsResponse(counters=dict(sorted(self._counters.items())))
