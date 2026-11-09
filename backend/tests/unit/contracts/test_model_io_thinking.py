"""Message.thinking_blocks carries provider thinking blocks unchanged (SPEC §8.1)."""

from __future__ import annotations

import json
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from agentplatform.contracts.model_io import Message, ModelRequest, TextPart, ToolCall

BLOCKS: list[dict[str, Any]] = [
    {
        "type": "thinking",
        "thinking": "The button is top right.\nUnicode: é ✓",
        "signature": "c2lnbmF0dXJlLW5vdC1yZWFs",  # gitleaks:allow
    },
    {"type": "redacted_thinking", "data": "b3BhcXVlLWJsb2I="},  # gitleaks:allow
    {"type": "thinking", "nested": {"list": [1, 2.5, None, True], "empty": {}}},
]


def assistant_with_blocks() -> Message:
    return Message(
        role="assistant",
        content=[TextPart(text="Clicking.")],
        tool_calls=[ToolCall(id="call_1", name="click", arguments={"x": 1, "y": 2})],
        thinking_blocks=BLOCKS,
    )


def test_defaults_to_none_and_old_payloads_still_validate() -> None:
    assert Message.text("assistant", "hi").thinking_blocks is None
    old = {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}
    assert Message.model_validate(old).thinking_blocks is None


def test_blocks_roundtrip_unchanged_and_in_order() -> None:
    message = assistant_with_blocks()
    again = Message.model_validate_json(message.model_dump_json())
    assert again == message
    assert again.thinking_blocks == BLOCKS
    assert json.dumps(again.thinking_blocks) == json.dumps(BLOCKS)


def test_blocks_survive_inside_a_request() -> None:
    request = ModelRequest(
        messages=[
            Message.text("user", "Do it."),
            assistant_with_blocks(),
            Message.tool_result("call_1", "ok"),
        ]
    )
    again = ModelRequest.model_validate_json(request.model_dump_json())
    assert again.messages[1].thinking_blocks == BLOCKS


def test_empty_list_is_kept() -> None:
    message = Message(role="assistant", thinking_blocks=[])
    assert Message.model_validate_json(message.model_dump_json()).thinking_blocks == []


@pytest.mark.parametrize("role", ["system", "user", "tool"])
def test_only_assistant_messages_may_carry_blocks(
    role: Literal["system", "user", "tool"],
) -> None:
    with pytest.raises(ValidationError, match="only allowed on assistant"):
        Message(role=role, content=[TextPart(text="x")], thinking_blocks=BLOCKS)
