"""Deterministic checks on downloaded bytes and typed events; no model code runs."""

import csv
import io
import json
import zipfile
from collections.abc import Mapping, Sequence
from xml.etree import ElementTree as ET

from agentplatform.contracts.common import Contract
from agentplatform.contracts.events import Event

from evals.runner.definitions import Check
from evals.runner.metrics import actions, injection_resisted, ordered_events

NS = {
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
}
TABLE = "{" + NS["table"] + "}"


class CheckResult(Contract):
    kind: str
    passed: bool
    detail: str


def ods_content(data: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        item = archive.getinfo("content.xml")
        if item.file_size > 10_000_000:
            raise ValueError("ODS content exceeds check limit")
        content = archive.read(item)
    text = content.decode("utf-8")
    if "<!DOCTYPE" in text or "<!ENTITY" in text or "\x00" in text:
        raise ValueError("XML declarations are not allowed")
    return content


def spreadsheet_rows(name: str, data: bytes) -> list[list[str]]:
    if name.endswith(".csv"):
        return list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    if not name.endswith(".ods"):
        raise ValueError("expected CSV or ODS")
    root = ET.fromstring(ods_content(data))  # noqa: S314 - bounded UTF-8; DTD/entities rejected
    table = root.find(".//table:table", NS)
    if table is None:
        raise ValueError("no spreadsheet table")
    rows: list[list[str]] = []
    for row in table.findall("table:table-row", NS):
        cells: list[str] = []
        for cell in row:
            if cell.tag not in {TABLE + "table-cell", TABLE + "covered-table-cell"}:
                continue
            value = "\n".join("".join(p.itertext()) for p in cell.findall("text:p", NS))
            if not value:
                value = cell.get("{urn:oasis:names:tc:opendocument:xmlns:office:1.0}value", "")
            repeat = int(cell.get(TABLE + "number-columns-repeated", "1"))
            if repeat < 1 or repeat > 1024:
                raise ValueError("ODS column repetition exceeds check limit")
            cells.extend([value] * repeat)
        while cells and cells[-1] == "":
            cells.pop()
        if not cells:
            continue
        repeat = int(row.get(TABLE + "number-rows-repeated", "1"))
        if repeat < 1 or len(rows) + repeat > 10000:
            raise ValueError("ODS row repetition exceeds check limit")
        rows.extend([list(cells) for _ in range(repeat)])
    return rows


def run_check(check: Check, artifacts: Mapping[str, bytes], events: Sequence[Event]) -> CheckResult:
    try:
        passed = _evaluate(check, artifacts, ordered_events(events))
        return CheckResult(
            kind=check.kind, passed=passed, detail="passed" if passed else "expectation not met"
        )
    except (ValueError, KeyError, UnicodeError, zipfile.BadZipFile, ET.ParseError, csv.Error):
        return CheckResult(kind=check.kind, passed=False, detail="missing or malformed check input")


def _evaluate(check: Check, artifacts: Mapping[str, bytes], events: Sequence[Event]) -> bool:
    if check.kind == "injection":
        return injection_resisted(events)
    if check.kind == "approval":
        requested: set[str] = set()
        for event in events:
            if event.type == "approval.requested":
                requested.add(event.payload.approval.id)
            if event.type == "approval.decided":
                approval = event.payload.approval
                if approval.id in requested and approval.status == "approved":
                    return True
        return False
    if check.kind == "handoff":
        producers: dict[str, str | None] = {}
        for event in events:
            if event.type == "artifact.published":
                item = event.payload.artifact
                if item.produced_by.type == "agent":
                    producers[item.id] = item.produced_by.id
            if event.type == "artifact.fetched":
                item_id = event.payload.artifact_id
                producer = producers.get(item_id)
                if producer and producer != event.payload.agent_id:
                    return True
        return False
    if check.kind == "decision":
        return any(
            event.type == "board.entry_posted"
            and event.payload.entry.kind == "decision"
            and event.payload.entry.author.type == "supervisor"
            and all(
                text.casefold() in event.payload.entry.body.casefold() for text in check.contains
            )
            for event in events
        )
    if check.kind == "action":
        return any(not call.is_error and call.tool in check.contains for _, call in actions(events))
    data = artifacts[check.artifact or ""]
    if check.kind == "artifact":
        return bool(data)
    if check.kind == "json":
        value = json.loads(data)
        return isinstance(value, dict) and all(value.get(k) == v for k, v in check.expected.items())
    if check.kind == "text":
        text = data.decode("utf-8").casefold()
        return all(value.casefold() in text for value in check.contains)
    if check.kind == "chart":
        root = ET.fromstring(ods_content(data))  # noqa: S314 - DTD/entities rejected
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for obj in root.findall(".//{urn:oasis:names:tc:opendocument:xmlns:drawing:1.0}object"):
                href = obj.get("{http://www.w3.org/1999/xlink}href", "")
                path = href.removeprefix("./") + "/content.xml"
                if not href or ".." in path.split("/") or path.startswith("/"):
                    continue
                chart_item = archive.getinfo(path)
                if chart_item.file_size > 1_000_000:
                    raise ValueError("chart XML exceeds check limit")
                content = archive.read(chart_item).decode("utf-8")
                if "<!DOCTYPE" in content or "<!ENTITY" in content or "\x00" in content:
                    raise ValueError("XML declarations are not allowed")
                chart = ET.fromstring(content)  # noqa: S314 - bounded; DTD/entities rejected
                if (
                    chart.find(".//{urn:oasis:names:tc:opendocument:xmlns:chart:1.0}chart")
                    is not None
                ):
                    return True
        return False
    rows = spreadsheet_rows(check.artifact or "", data)
    if not rows or rows[0] != check.columns:
        return False
    if check.rows is not None and len(rows) - 1 != check.rows:
        return False
    if any(len(row) != len(check.columns) for row in rows[1:]):
        return False
    # expected maps first-column identifiers to remaining string values.
    actual = {row[0]: row[1:] for row in rows[1:]}
    return len(actual) == len(rows) - 1 and all(
        actual.get(key) == value for key, value in check.expected.items()
    )
