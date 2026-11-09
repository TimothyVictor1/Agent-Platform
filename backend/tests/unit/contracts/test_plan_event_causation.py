"""causation_id on session.plan_proposed / session.plan_updated (SPEC §7.1)."""

from __future__ import annotations

from typing import Any

import pytest

from agentplatform.contracts import EVENT_ADAPTER, Actor, new_id
from agentplatform.contracts.api import CommandAccepted, WsEventMessage
from agentplatform.contracts.events import (
    EventDraft,
    EventPayload,
    SessionPlanProposed,
    SessionPlanUpdated,
    build_event,
)
from agentplatform.testing import make_plan

USER = Actor(type="user", id="u1", name="You")
SYSTEM = Actor(type="system", name="Planner")


def plan_payloads() -> list[tuple[EventPayload, Actor]]:
    proposal = make_plan()
    return [
        (SessionPlanProposed(plan_version=2, proposal=proposal), SYSTEM),
        (SessionPlanUpdated(plan_version=3, proposal=proposal, source="user"), USER),
    ]


@pytest.mark.parametrize(("payload", "actor"), plan_payloads())
def test_causation_id_carries_the_command_id(payload: EventPayload, actor: Actor) -> None:
    accepted = CommandAccepted(command_id=new_id())
    event = build_event(
        EventDraft(payload=payload, actor=actor, causation_id=accepted.command_id),
        session_id="s1",
        seq=7,
    )
    assert event.type == payload.event_type
    assert event.causation_id == accepted.command_id

    restored = EVENT_ADAPTER.validate_json(EVENT_ADAPTER.dump_json(event))
    assert restored == event
    assert restored.causation_id == accepted.command_id

    message = WsEventMessage(event=event)
    assert WsEventMessage.model_validate_json(message.model_dump_json()) == message


@pytest.mark.parametrize(("payload", "actor"), plan_payloads())
def test_causation_id_is_optional_for_old_events(payload: EventPayload, actor: Actor) -> None:
    event = build_event(EventDraft(payload=payload, actor=actor), session_id="s1", seq=1)
    assert event.causation_id is None

    data: dict[str, Any] = EVENT_ADAPTER.dump_python(event, mode="json")
    del data["causation_id"]  # an event stored before the field was used
    restored = EVENT_ADAPTER.validate_python(data)
    assert restored.causation_id is None
    assert restored == event


def test_command_id_fits_the_events_column() -> None:
    # events.causation_id is String(36) (SPEC §17); command ids are UUID strings.
    assert len(CommandAccepted(command_id=new_id()).command_id) <= 36
