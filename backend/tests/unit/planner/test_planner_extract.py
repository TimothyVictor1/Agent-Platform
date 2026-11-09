"""extract_json_object: fences, prose, strings/escapes, bad candidates, limits."""

from __future__ import annotations

import json

import pytest

from agentplatform.runtime.planner import (
    MAX_EXTRACT_ATTEMPTS,
    MAX_EXTRACT_CHARS,
    extract_json_object,
)


def test_plain_object() -> None:
    assert extract_json_object('{"tasks": [], "rationale": "x"}') == {
        "tasks": [],
        "rationale": "x",
    }


def test_prose_around_object() -> None:
    text = 'Sure! Here is the plan:\n{"a": {"b": [1, 2]}}\nLet me know if you need more.'
    assert extract_json_object(text) == {"a": {"b": [1, 2]}}


def test_fenced_json_is_preferred_over_prose_braces() -> None:
    text = 'I used {curly} notes.\n```json\n{"a": 1}\n```\nAnd {"b": 2} later.'
    assert extract_json_object(text) == {"a": 1}


def test_fence_without_language_and_unclosed_fence() -> None:
    assert extract_json_object('```\n{"a": 1}\n```') == {"a": 1}
    assert extract_json_object('```json\n{"a": 1}\n') == {"a": 1}


def test_invalid_fence_falls_back_to_whole_text() -> None:
    text = '```json\n{not json}\n```\nCorrected: {"a": 1}'
    assert extract_json_object(text) == {"a": 1}


def test_braces_inside_strings_are_ignored() -> None:
    obj = {"goal": "type } then { and }}", "n": 1}
    assert extract_json_object("prefix " + json.dumps(obj) + " suffix }") == obj


def test_escaped_quotes_and_backslashes() -> None:
    obj = {"cmd": 'say "hi" {', "path": "C:\\dir\\", "tail": "}"}
    text = json.dumps(obj)
    assert '\\"' in text
    assert "\\\\" in text
    assert extract_json_object(text + " trailing") == obj


def test_first_balanced_but_invalid_candidate_is_skipped() -> None:
    assert extract_json_object('{not: json} then {"ok": true}') == {"ok": True}


def test_unclosed_first_brace_then_valid_object() -> None:
    assert extract_json_object('{ oops {"a": 1}') == {"a": 1}


def test_array_is_not_an_object_but_an_object_inside_it_is_found() -> None:
    with pytest.raises(ValueError, match="valid JSON object"):
        extract_json_object('["T1", "T2"]')
    assert extract_json_object('[{"a": 1}, {"b": 2}]') == {"a": 1}


def test_no_object() -> None:
    with pytest.raises(ValueError, match="valid JSON object"):
        extract_json_object("I could not make a plan.")


def test_too_long_is_rejected_before_scanning() -> None:
    with pytest.raises(ValueError, match="too long"):
        extract_json_object("{" * (MAX_EXTRACT_CHARS + 1))


def test_attempts_are_bounded() -> None:
    text = "{x} " * (MAX_EXTRACT_ATTEMPTS + 5) + '{"a": 1}'
    with pytest.raises(ValueError, match="valid JSON object"):
        extract_json_object(text)
