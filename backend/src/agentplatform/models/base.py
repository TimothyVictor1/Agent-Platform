"""The provider-agnostic model interface (SPEC §8.1).

Implementations live next to this file (T02): ``openai_compatible.py``,
``anthropic_client.py`` and the wrappers in ``wrappers.py``. Everything in the
runtime (planner, supervisor, agent loop, summarizer) talks to models only through
``ModelClient``.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from agentplatform.contracts.common import HealthStatus
from agentplatform.contracts.model_io import ModelRequest, ModelResponse

__all__ = ["ModelClient"]


@runtime_checkable
class ModelClient(Protocol):
    """A chat model behind one provider/model/base_url.

    Request semantics every implementation must honour:

    * ``req.messages`` is ordered. ``system`` messages carry text only; images
      (``ImagePart``) appear only in ``user`` messages; a ``tool`` message carries the
      text result of the tool call named by ``tool_call_id`` and must directly follow
      the ``assistant`` message that contains that call (providers require this).
    * ``req.tools`` lists the tools the model may call. ``tool_choice="required"``
      means the model must call at least one tool. If the provider cannot enforce
      "required", fall back to "auto" (the runtime handles missing calls).
    * ``req.response_format="json"`` asks for a JSON object in ``text`` where the
      provider supports it; otherwise it is a hint only.
    * ``req.timeout_s`` bounds the whole call including retries done inside the
      implementation.

    Response semantics:

    * ``tool_calls`` contains structured tool calls in the order the model produced
      them; ``arguments`` are parsed JSON objects (invalid JSON from the provider is
      passed as ``{"_raw": "<text>"}`` so the runtime can report it to the model).
    * ``text`` is the assistant's text (may be ``None`` when only tools were called).
      Implementations do NOT run the text fallback parser; the runtime does that.
    * ``usage`` holds provider-reported token counts and the number of images sent.
      ``cost_usd`` is left at 0 by provider clients and filled by the accounting
      wrapper.
    * ``latency_ms`` is the wall time of the successful attempt.
    * ``raw`` is a JSON-able dump of the provider response (no secrets).

    Errors: raise ``agentplatform.errors.ModelTimeoutError`` on timeouts and
    ``ModelError`` for everything else (with a short safe message). Never raise
    provider SDK exceptions to callers. Retrying is done by a wrapper, not by
    provider clients.

    Implementations must be safe to call concurrently from many asyncio tasks.
    """

    @property
    def name(self) -> str:
        """Stable label for logs/metrics, e.g. ``"openai_compatible:qwen2.5-vl@z2.local"``."""
        ...

    async def complete(self, req: ModelRequest) -> ModelResponse:
        """Run one chat completion. See the class docstring for the contract."""
        ...

    async def health(self) -> HealthStatus:
        """Cheap liveness check (e.g. ``GET /models`` or a 1-token request).

        Must not raise: report failures in ``HealthStatus.error``. Callers apply their
        own timeout (the API uses 5 s).
        """
        ...

    async def aclose(self) -> None:
        """Release HTTP connections. Idempotent."""
        ...
