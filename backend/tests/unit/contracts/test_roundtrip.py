"""Contracts survive model -> JSON -> model unchanged, and reject unknown fields."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from agentplatform.contracts import (
    COMMAND_ADAPTER,
    EVENT_ADAPTER,
    PUBLIC_MODELS,
    Actor,
    Approval,
    Artifact,
    BoardEntryDraft,
    Frame,
    Plan,
    TaskResult,
    new_id,
    utcnow,
)
from agentplatform.contracts.agent import SandboxRef, agent_identity
from agentplatform.contracts.api import (
    CreateSessionRequest,
    SessionDetail,
    WsEventMessage,
    WsServerMessage,
)
from agentplatform.contracts.commands import StopSessionCommand, UpdatePlanCommand
from agentplatform.contracts.common import Contract
from agentplatform.contracts.events import (
    EPHEMERAL_ADAPTER,
    EVENT_TYPES,
    PAYLOAD_TYPES,
    BoardEntryPosted,
    EventDraft,
    Heartbeat,
    SessionCreated,
    TaskAssigned,
    build_event,
)
from agentplatform.contracts.model_io import ImagePart, Message, ModelRequest, TextPart
from agentplatform.testing import (
    make_agent,
    make_board_entry,
    make_plan,
    make_session,
    make_task,
)


def roundtrip[M: BaseModel](model: M) -> M:
    restored = type(model).model_validate_json(model.model_dump_json())
    assert restored == model
    return restored


def test_spec_event_types_are_all_defined() -> None:
    spec_types = {
        "session.created", "session.planning_started", "session.plan_proposed",
        "session.plan_updated", "session.approved", "session.paused", "session.resumed",
        "session.completed", "session.failed", "session.stopped", "session.budget_warning",
        "session.budget_exceeded",
        "task.created", "task.updated", "task.ready", "task.assigned",
        "task.assignment_acked", "task.started", "task.result_submitted",
        "task.review_started", "task.accepted", "task.rework_requested", "task.failed",
        "task.cancelled",
        "agent.created", "agent.sandbox_booting", "agent.sandbox_ready", "agent.step",
        "agent.status_changed", "agent.stuck_detected", "agent.error", "agent.stopped",
        "board.entry_posted", "board.entry_status_changed",
        "artifact.published", "artifact.fetched",
        "user.steer", "user.takeover_started", "user.takeover_ended",
        "approval.requested", "approval.decided",
        "supervisor.triggered", "supervisor.action",
    }  # fmt: skip
    # agent.heartbeat is bus-only (Heartbeat); session.planning_failed is an addition.
    assert set(EVENT_TYPES) == spec_types | {"session.planning_failed"}
    assert set(PAYLOAD_TYPES) == set(EVENT_TYPES)


def test_domain_models_roundtrip() -> None:
    session = make_session()
    roundtrip(session)
    task = make_task(status="done")
    task = task.model_copy(
        update={
            "result": TaskResult(
                summary="done",
                outputs={"rows": [{"app": "Notion", "price": 8.0}], "note": None},
                artifact_ids=["a1"],
                confidence="high",
            )
        }
    )
    roundtrip(task)
    roundtrip(make_agent(2))
    roundtrip(make_board_entry("question", to=["a-2"], tags=["pricing"]))
    roundtrip(Plan(session_id="s", version=1, proposal=make_plan(), created_at=utcnow()))
    roundtrip(
        Artifact(
            id=new_id(),
            session_id="s",
            name="comparison.ods",
            mime="application/vnd.oasis.opendocument.spreadsheet",
            size_bytes=10,
            sha256="0" * 64,
            produced_by=Actor.agent("a1", "Ada"),
            storage_key="sha256/00/" + "0" * 64,
            created_at=utcnow(),
        )
    )
    roundtrip(
        Approval(
            id=new_id(),
            session_id="s",
            agent_id="a1",
            action_summary="Submit the contact form",
            risk="medium",
            created_at=utcnow(),
        )
    )
    roundtrip(
        Frame(
            id=new_id(),
            session_id="s",
            agent_id="a1",
            step=3,
            ts=utcnow(),
            storage_key_small="frames/s/a1/3.jpg",
            phash="ffff0000ffff0000",
        )
    )
    roundtrip(SandboxRef(provider="e2b", sandbox_id="x", created_at=utcnow()))


@pytest.mark.parametrize(
    ("model", "data"),
    [
        (Actor, {"type": "user", "name": "You", "extra": 1}),
        (BoardEntryDraft, {"kind": "fact", "title": "t", "nope": True}),
        (CreateSessionRequest, {"prompt": "x", "model_profile": "p", "agentcount": 2}),
    ],
)
def test_extra_fields_are_rejected(model: type[BaseModel], data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(data)


def test_every_public_model_forbids_extra() -> None:
    for model in PUBLIC_MODELS:
        assert issubclass(model, Contract), model
        assert model.model_config.get("extra") == "forbid", model


def test_datetimes_serialize_as_utc_z() -> None:
    plus_two = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    entry = make_board_entry().model_copy(update={"created_at": plus_two})
    dumped = json.loads(entry.model_dump_json())
    assert dumped["created_at"] == "2026-01-01T10:00:00Z"
    restored = type(entry).model_validate(dumped)
    assert restored.created_at == plus_two


def test_naive_datetimes_are_rejected() -> None:
    data = make_board_entry().model_dump(mode="json")
    data["created_at"] = "2026-01-01T10:00:00"
    with pytest.raises(ValidationError):
        type(make_board_entry()).model_validate(data)


def test_event_build_and_union_roundtrip() -> None:
    session = make_session()
    draft = EventDraft(payload=SessionCreated(session=session), actor=Actor.user("local"))
    event = build_event(draft, session_id=session.id, seq=1)
    assert event.type == "session.created"
    assert isinstance(event.payload, SessionCreated)
    restored = EVENT_ADAPTER.validate_json(EVENT_ADAPTER.dump_json(event))
    assert restored == event

    now = utcnow()
    assigned = build_event(
        EventDraft(
            payload=TaskAssigned(
                task_id="t1",
                task_key="T1",
                agent_id="a1",
                attempt=1,
                ack_deadline=now,
                lease_expires_at=now,
            ),
            actor=Actor.system(),
            correlation_id="t1",
        ),
        session_id=session.id,
        seq=2,
    )
    assert EVENT_ADAPTER.validate_python(assigned.model_dump(mode="json")) == assigned


def test_build_event_copies_payload() -> None:
    entry = make_board_entry()
    payload = BoardEntryPosted(entry=entry)
    event = build_event(EventDraft(payload=payload, actor=entry.author), session_id="s", seq=5)
    assert isinstance(event.payload, BoardEntryPosted)
    assert event.payload == payload
    assert event.payload is not payload


def test_event_type_and_payload_must_match() -> None:
    event = build_event(
        EventDraft(payload=SessionCreated(session=make_session()), actor=Actor.system()),
        session_id="s",
        seq=1,
    )
    data = event.model_dump(mode="json")
    data["type"] = "task.ready"
    with pytest.raises(ValidationError):
        EVENT_ADAPTER.validate_python(data)


def test_ws_message_and_ephemeral_roundtrip() -> None:
    event = build_event(
        EventDraft(payload=SessionCreated(session=make_session()), actor=Actor.system()),
        session_id="s",
        seq=1,
    )
    msg = WsEventMessage(event=event)
    adapter: TypeAdapter[WsServerMessage] = TypeAdapter(WsServerMessage)
    assert adapter.validate_json(msg.model_dump_json()) == msg

    hb = Heartbeat(session_id="s", agent_id="a", step=4, ts=utcnow())
    assert EPHEMERAL_ADAPTER.validate_json(hb.model_dump_json()) == hb


def test_commands_roundtrip() -> None:
    now, user = utcnow(), Actor.user()
    for command in (
        StopSessionCommand(id=new_id(), session_id="s", issued_at=now, issued_by=user),
        UpdatePlanCommand(
            id=new_id(),
            session_id="s",
            issued_at=now,
            issued_by=user,
            tasks=make_plan().tasks,
            base_version=1,
        ),
    ):
        restored = COMMAND_ADAPTER.validate_json(command.model_dump_json())
        assert restored == command


def test_image_part_bytes_are_base64_in_json() -> None:
    req = ModelRequest(
        messages=[
            Message(
                role="user",
                content=[
                    TextPart(text="look"),
                    ImagePart(jpeg_bytes=b"\xff\xd8abc", width=2, height=1),
                ],
            )
        ]
    )
    dumped = json.loads(req.model_dump_json())
    encoded = dumped["messages"][0]["content"][1]["jpeg_bytes"]
    assert base64.urlsafe_b64decode(encoded) == b"\xff\xd8abc"
    assert ModelRequest.model_validate_json(req.model_dump_json()) == req


def test_session_detail_with_nested_models() -> None:
    session = make_session()
    detail = SessionDetail(
        session=session,
        tasks=[make_task(session_id=session.id)],
        agents=[make_agent(0, session_id=session.id)],
        last_seq=7,
    )
    roundtrip(detail)


def test_new_id_is_uuid7_and_sortable() -> None:
    ids = [new_id() for _ in range(2000)]
    assert ids == sorted(ids)
    assert len(set(ids)) == len(ids)
    assert all(i[14] == "7" for i in ids)  # version nibble
    assert all(i[19] in "89ab" for i in ids)  # RFC 4122 variant


def test_agent_identity_is_deterministic() -> None:
    assert agent_identity(0) == ("Ada", "#4F8EF7")
    assert agent_identity(1)[0] == "Brook"
    with pytest.raises(ValueError, match="agent index"):
        agent_identity(6)


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)
    assert now.microsecond % 1000 == 0
    assert abs((datetime.now(UTC) - now).total_seconds()) < 5
