"""WebSocket ticket contracts (SPEC §16): POST /sessions/{id}/ws-ticket."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from agentplatform.contracts import PUBLIC_MODELS
from agentplatform.contracts.api import (
    WS_CLOSE_TICKET_REJECTED,
    WS_TICKET_TTL_S,
    WsTicket,
    WsTicketRequest,
)

TICKET = "Zm9vYmFyYmF6cXV4LXRlc3QtdGlja2V0LW5vdC1yZWFs_0123"  # gitleaks:allow


def test_ttl_and_close_code_constants() -> None:
    assert WS_TICKET_TTL_S == 60
    assert WS_CLOSE_TICKET_REJECTED == 4401


def test_ticket_roundtrip_and_default_ttl() -> None:
    expires = datetime(2026, 11, 9, 12, 0, 0, tzinfo=UTC) + timedelta(seconds=WS_TICKET_TTL_S)
    ticket = WsTicket(ticket=TICKET, expires_at=expires)
    assert ticket.ttl_s == 60
    again = WsTicket.model_validate_json(ticket.model_dump_json())
    assert again == ticket
    assert again.expires_at == expires


def test_request_body_is_empty_and_optional() -> None:
    assert WsTicketRequest.model_validate({}) == WsTicketRequest()
    assert WsTicketRequest.model_validate_json("{}") == WsTicketRequest()
    with pytest.raises(ValidationError):
        WsTicketRequest.model_validate({"token": "x"})


@pytest.mark.parametrize(
    "bad",
    [
        "short",
        "a" * 31,
        "a" * 129,
        "has spaces in it but is long enough to pass length",
        "not/url/safe+chars=padding-and-long-enough-0123456789",  # gitleaks:allow
    ],
)
def test_ticket_must_be_long_and_url_safe(bad: str) -> None:
    with pytest.raises(ValidationError):
        WsTicket(ticket=bad, expires_at=datetime.now(UTC))


def test_ticket_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        WsTicket.model_validate(
            {"ticket": TICKET, "expires_at": "2026-11-09T12:01:00Z", "api_token": "x"}
        )


def test_ticket_models_are_exported() -> None:
    assert WsTicket in PUBLIC_MODELS
    assert WsTicketRequest in PUBLIC_MODELS
