"""Provider-agnostic model request/response types (SPEC §8.1).

These are runtime-internal (not exported to the frontend). Image bytes serialize to
base64 in JSON so requests can be logged/recorded; debug logging replaces them with
hashes before writing.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import ConfigDict, Field, model_validator

from agentplatform.contracts.common import Contract

__all__ = [
    "ImagePart",
    "Message",
    "ModelRequest",
    "ModelResponse",
    "ModelUsage",
    "Part",
    "TextPart",
    "ToolCall",
    "ToolSpec",
]


class TextPart(Contract):
    type: Literal["text"] = "text"
    text: str


class ImagePart(Contract):
    """A JPEG image. Only allowed in ``user`` messages (providers rely on this)."""

    model_config = ConfigDict(extra="forbid", ser_json_bytes="base64", val_json_bytes="base64")

    type: Literal["image"] = "image"
    jpeg_bytes: bytes
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    detail: Literal["low", "high", "auto"] = "auto"


Part = Annotated[TextPart | ImagePart, Field(discriminator="type")]


class ToolCall(Contract):
    """A tool call requested by the model. ``id`` is provider-assigned (or synthesized
    by the fallback parser); tool results refer to it via ``Message.tool_call_id``."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolSpec(Contract):
    """A tool offered to the model. ``parameters`` is a JSON Schema object."""

    name: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")
    description: str
    parameters: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False}
    )


class Message(Contract):
    """One chat message.

    * ``system``: text parts only.
    * ``user``: text and image parts.
    * ``assistant``: optional text parts, ``tool_calls`` and ``thinking_blocks``.
    * ``tool``: the result of one tool call (text parts only), ``tool_call_id`` set.
    """

    role: Literal["system", "user", "assistant", "tool"]
    content: list[Part] = Field(default_factory=list)
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    thinking_blocks: list[dict[str, Any]] | None = None
    """Provider thinking blocks of an assistant turn (e.g. Anthropic ``thinking`` /
    ``redacted_thinking`` with their signatures), exactly as the provider returned
    them. Opaque: clients echo them back unchanged and in order when this message is
    sent again, and never edit, merge or display them (SPEC §8.1). Assistant only."""

    @model_validator(mode="after")
    def _thinking_only_on_assistant(self) -> Message:
        if self.thinking_blocks is not None and self.role != "assistant":
            raise ValueError("thinking_blocks are only allowed on assistant messages")
        return self

    @classmethod
    def text(cls, role: Literal["system", "user", "assistant"], text: str) -> Message:
        return cls(role=role, content=[TextPart(text=text)])

    @classmethod
    def tool_result(cls, tool_call_id: str, text: str) -> Message:
        return cls(role="tool", content=[TextPart(text=text)], tool_call_id=tool_call_id)

    def text_content(self) -> str:
        """All text parts joined with newlines."""
        return "\n".join(p.text for p in self.content if isinstance(p, TextPart))

    def images(self) -> list[ImagePart]:
        return [p for p in self.content if isinstance(p, ImagePart)]


class ModelRequest(Contract):
    messages: list[Message]
    tools: list[ToolSpec] | None = None
    tool_choice: Literal["auto", "required", "none"] = "auto"
    max_output_tokens: int = Field(default=1024, ge=1)
    temperature: float = Field(default=0.0, ge=0, le=2)
    response_format: Literal["json"] | None = None
    timeout_s: float = Field(default=120.0, gt=0)


class ModelUsage(Contract):
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    images: int = Field(default=0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)
    """Filled by the accounting wrapper from the price table; 0 for local models."""


class ModelResponse(Contract):
    text: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: ModelUsage = Field(default_factory=ModelUsage)
    raw: dict[str, Any] = Field(default_factory=dict)
    """Provider response as JSON-able dict, for debugging. Never contains API keys."""
    latency_ms: int = Field(default=0, ge=0)
