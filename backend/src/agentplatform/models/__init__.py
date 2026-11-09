"""Provider-agnostic ModelClient interface and provider adapters (T02).

Public entry points: ``build_client`` (factory), ``SharedModelState`` (one per
process) and ``parse_text_tool_calls`` (the agent loop's ``fallback_parser``).
"""

from agentplatform.models.anthropic_client import AnthropicClient
from agentplatform.models.base import ModelClient
from agentplatform.models.errors import RetryableModelError
from agentplatform.models.factory import build_client
from agentplatform.models.fallback_parser import parse_text_tool_calls
from agentplatform.models.openai_compatible import OpenAICompatibleClient
from agentplatform.models.shared import SharedModelState
from agentplatform.models.wrappers import (
    AccountingClient,
    ConcurrencyLimitedClient,
    DebugLoggingClient,
    RetryingClient,
)

__all__ = [
    "AccountingClient",
    "AnthropicClient",
    "ConcurrencyLimitedClient",
    "DebugLoggingClient",
    "ModelClient",
    "OpenAICompatibleClient",
    "RetryableModelError",
    "RetryingClient",
    "SharedModelState",
    "build_client",
    "parse_text_tool_calls",
]
