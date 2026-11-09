import io
import zipfile
from datetime import UTC, datetime, timedelta
from hashlib import sha256

from agentplatform.contracts.artifact import Artifact
from agentplatform.contracts.common import Actor
from agentplatform.contracts.events import Event, EventDraft, EventPayload, build_event
from agentplatform.contracts.session import Session

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def event(seq: int, payload: EventPayload, actor: Actor | None = None) -> Event:
    return build_event(
        EventDraft(payload=payload, actor=actor or Actor.system()),
        session_id="s1",
        event_id=f"event-{seq}",
        seq=seq,
        ts=NOW + timedelta(seconds=seq - 1),
    )


def session(status: str = "completed") -> Session:
    return Session.model_validate(
        {
            "id": "s1",
            "user_id": "u1",
            "title": "Eval",
            "prompt": "Test",
            "status": status,
            "agent_count": 2,
            "model_profile": "local-free",
            "sandbox_provider": "docker_local",
            "plan_version": 1,
            "created_at": NOW,
        }
    )


def artifact(name: str, data: bytes, version: int = 1) -> Artifact:
    return Artifact(
        id=f"artifact-{version}",
        session_id="s1",
        name=name,
        mime="application/octet-stream",
        size_bytes=len(data),
        sha256=sha256(data).hexdigest(),
        produced_by=Actor.agent("a", "Ada"),
        storage_key="fixture",
        version=version,
        created_at=NOW,
    )


def ods(content: str, chart: str | None = None) -> bytes:
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w") as archive:
        archive.writestr("content.xml", content)
        if chart:
            archive.writestr("Object 1/content.xml", chart)
    return result.getvalue()


def sample_ods(with_chart: bool = False) -> bytes:
    obj = '<draw:object xlink:href="./Object 1"/>' if with_chart else ""
    return ods(
        "<office:document-content "
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
        'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
        'xmlns:xlink="http://www.w3.org/1999/xlink">'
        "<office:body><office:spreadsheet><table:table>"
        "<table:table-row><table:table-cell><text:p>quarter</text:p></table:table-cell>"
        "<table:table-cell><text:p>revenue</text:p></table:table-cell></table:table-row>"
        "<table:table-row><table:table-cell><text:p>Q1</text:p></table:table-cell>"
        '<table:table-cell office:value="10"/></table:table-row>'
        + obj
        + "</table:table></office:spreadsheet></office:body></office:document-content>",
        '<root xmlns:chart="urn:oasis:names:tc:opendocument:xmlns:chart:1.0"><chart:chart/></root>'
        if with_chart
        else None,
    )
