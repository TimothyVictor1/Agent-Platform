"""Helpers shared by the provider clients (T02 review)."""

from __future__ import annotations

from typing import Any

from agentplatform.models._common import parse_arguments


def test_deep_array_string_is_raw_not_recursion_error() -> None:
    text = "[" * 100_000 + "]" * 100_000
    assert parse_arguments(text) == {"_raw": text}  # was RecursionError


def test_deep_non_string_arguments_are_raw_not_recursion_error() -> None:
    deep: list[Any] = []
    for _ in range(100_000):
        deep = [deep]
    out = parse_arguments(deep)
    assert set(out) == {"_raw"}
    assert isinstance(out["_raw"], str)
