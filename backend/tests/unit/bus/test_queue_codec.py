"""``decode_queue_item`` rejects every non-envelope with ``ValueError`` (T04 review #9)."""

from __future__ import annotations

import pytest

from agentplatform.bus.base import decode_queue_item, encode_queue_item
from agentplatform.contracts.commands import PlanSessionCommand
from agentplatform.contracts.common import Actor, new_id, utcnow


def test_round_trip() -> None:
    cmd = PlanSessionCommand(
        id=new_id(), session_id=new_id(), issued_at=utcnow(), issued_by=Actor.user("local")
    )
    assert decode_queue_item(encode_queue_item(cmd)) == cmd


@pytest.mark.parametrize(
    "raw",
    [
        "null",
        "[]",
        "1",
        '"text"',
        "true",
        "{}",
        '{"kind": "command"}',
        '{"data": {}}',
        '{"kind": "command", "data": {}, "extra": 1}',
        '{"kind": "command", "data": null}',
        '{"kind": "event", "data": []}',
        '{"kind": ["x"], "data": {}}',
        '{"kind": "nope", "data": {}}',
        "{not json",
    ],
)
def test_malformed_items_raise_value_error(raw: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - any ValueError is the contract
        decode_queue_item(raw)
