"""The desktop image's Firefox policies keep first-run screens away from the agent.

A first-run "Welcome to Firefox" / terms-of-use dialog covered example.com in the
first live run and the agent clicked into the privacy notice instead (T13).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentplatform.config import find_repo_root

POLICIES = find_repo_root(Path(__file__).parent) / "sandbox-image" / "firefox-policies.json"


def _policies() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(POLICIES.read_text())
    policies: dict[str, Any] = data["policies"]
    return policies


def test_first_run_and_welcome_pages_are_disabled() -> None:
    p = _policies()
    assert p["OverrideFirstRunPage"] == ""
    assert p["OverridePostUpdatePage"] == ""
    assert p["SkipTermsOfUse"] is True
    prefs = p["Preferences"]
    assert prefs["browser.aboutwelcome.enabled"]["Value"] is False
    assert prefs["browser.startup.homepage_override.mstone"]["Value"] == "ignore"
    assert prefs["datareporting.policy.dataSubmissionPolicyBypassNotification"]["Value"] is True


def test_messaging_default_browser_and_telemetry_prompts_are_off() -> None:
    p = _policies()
    messaging = p["UserMessaging"]
    assert messaging["SkipOnboarding"] is True
    assert messaging["Locked"] is True
    for key in (
        "WhatsNew",
        "ExtensionRecommendations",
        "FeatureRecommendations",
        "UrlbarInterventions",
        "MoreFromMozilla",
        "FirefoxLabs",
    ):
        assert messaging[key] is False, key
    assert p["DontCheckDefaultBrowser"] is True
    assert p["DisableTelemetry"] is True
    assert p["Preferences"]["browser.shell.checkDefaultBrowser"]["Value"] is False
