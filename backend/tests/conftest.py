"""Shared pytest configuration: skip live/docker tests unless explicitly enabled.

Everything under ``tests/live/`` is marked ``live`` automatically.
"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    live = os.environ.get("APP_LIVE") == "1"
    docker = os.environ.get("APP_DOCKER") == "1"
    for item in items:
        if "/tests/live/" in str(item.path):
            item.add_marker(pytest.mark.live)
        if "live" in item.keywords and not live:
            item.add_marker(pytest.mark.skip(reason="live test: set APP_LIVE=1"))
        if "docker" in item.keywords and not docker:
            item.add_marker(pytest.mark.skip(reason="docker test: set APP_DOCKER=1"))
