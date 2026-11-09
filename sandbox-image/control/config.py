"""Runtime configuration, read once from the process environment."""

from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _read_token() -> str | None:
    """Read the control token from its file (or env as a fallback).

    The container entrypoint relocates $CONTROL_TOKEN into
    CONTROL_TOKEN_FILE (default /run/control-token, owner agent, mode 0400)
    and unsets the variable, so no process environment carries the token.
    A missing or empty file means locked mode: every request is rejected.
    """
    path = os.environ.get("CONTROL_TOKEN_FILE", "/run/control-token")
    token = ""
    try:
        with open(path, encoding="utf-8") as fh:
            token = fh.read().strip()
    except OSError:
        pass
    if not token:
        token = os.environ.get("CONTROL_TOKEN", "").strip()
    return token or None


# Screen geometry; must match the Xvfb flags in supervisord.conf. The
# docker-local provider (T07) may override both via -e SCREEN_WIDTH/HEIGHT.
SCREEN_WIDTH = _int_env("SCREEN_WIDTH", 1280)
SCREEN_HEIGHT = _int_env("SCREEN_HEIGHT", 800)

# Bearer token (see _read_token). None means locked mode: every request is
# rejected. Note: the agent user can still read the token file by design —
# it is a per-container capability, not a user secret.
CONTROL_TOKEN: str | None = _read_token()

# Survival / abuse bounds. The backend enforces stricter limits (e.g. <=2000
# chars of typed text); these are container-wide hard limits. They are the
# single source for the bounds checks (control/input.py re-uses them).
MAX_OUTPUT_BYTES = 8_192  # run_command stdout/stderr truncation (SPEC §9.1)
MAX_FILE_BYTES = 50_000_000  # 50 MB upload (task T01)
MAX_FILE_READ_BYTES = 50_000_000  # 50 MB download
MAX_TYPE_CHARS = 50_000
MAX_TYPE_TOTAL_MS = 300_000  # cap chars * interval so a call cannot run forever
MAX_INTERVAL_MS = 250
MIN_INTERVAL_MS = 0
MAX_COMBO_CHARS = 64
DEFAULT_COMMAND_TIMEOUT_S = 60
MAX_COMMAND_TIMEOUT_S = 600
MAX_COMMAND_CHARS = 100_000
MAX_CLICK_COUNT = 3  # single / double / triple click
MIN_SCROLL_AMOUNT = 1
MAX_SCROLL_AMOUNT = 50

# The file-transfer APIs are confined to this root (the agent's home).
# /command still grants full agent-level shell access by design; this is
# defense-in-depth for the structured file endpoints, not the real boundary.
FILE_ROOT = os.environ.get("SANDBOX_FILE_ROOT", "/home/agent").rstrip("/") or "/"
