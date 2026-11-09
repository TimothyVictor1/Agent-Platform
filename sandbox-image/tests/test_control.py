"""Control-server acceptance tests (task T01, SPEC §9.1/§9.2).

Covers the required scenarios: health, auth rejected without a token,
screenshot is a valid PNG of the full screen, type text into mousepad then
read it back via /command, file write/read roundtrip — plus command
execution, truncation, timeouts, input validation and noVNC page serving.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import shlex
import struct
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from conftest import requires_docker  # pytest prepends tests/ to sys.path

pytestmark = [requires_docker]

TYPED_TEXT = "The quick brown agent types via xdotool."


def _multipart_raw(path: str, data: bytes) -> tuple[bytes, str]:
    from conftest import _multipart_body

    return _multipart_body(path, "blob.bin", data)


# --- helpers -------------------------------------------------------------------
def png_dimensions(png: bytes) -> tuple[int, int]:
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    assert png[12:16] == b"IHDR", "first chunk is not IHDR"
    width, height = struct.unpack(">II", png[16:24])
    return width, height


def close_mousepad(client) -> None:
    client.command("pkill -f mousepad >/dev/null 2>&1 || true")


def launch_mousepad(client, path: str) -> None:
    client.command(f"rm -f {path}; touch {path}")
    quoted = shlex.quote(path)
    client.command(f"nohup mousepad {quoted} >/dev/null 2>&1 & disown; sleep 0.2")


def wait_for_window(client, needle: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        out = str(client.command("wmctrl -l || true")["stdout"])
        if needle.lower() in out.lower():
            client.command(f"wmctrl -a {shlex.quote(needle)}")
            time.sleep(0.4)
            return
        time.sleep(0.4)
    pytest.fail(f"window containing {needle!r} never appeared")


def window_center(client) -> tuple[int, int]:
    out = str(
        client.command("xdotool search --onlyvisible --name 'Mousepad' || true")["stdout"]
    )
    win = out.strip().splitlines()[-1].strip()
    assert win.isdigit(), f"no mousepad window id: {out!r}"
    geo = str(client.command(f"xdotool getwindowgeometry --shell {win}")["stdout"])
    values: dict[str, int] = {}
    for line in geo.splitlines():
        if "=" in line:
            key, _, raw = line.partition("=")
            values[key.strip()] = int(raw.strip())
    return values["X"] + values["WIDTH"] // 2, values["Y"] + values["HEIGHT"] // 2


def poll_file(client, path: str, want: str, timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    data = b""
    while time.monotonic() < deadline:
        status, data = client.read_file(path)
        if status == 200 and data.decode("utf-8", "replace") == want:
            return
        time.sleep(0.4)
    got = data.decode("utf-8", "replace")
    pytest.fail(f"file never matched: want={want!r} got={got!r}")


# --- status / auth --------------------------------------------------------------
def test_health_ok(sandbox: SimpleNamespace) -> None:
    status, payload = sandbox.client.call("GET", "/health")
    assert status == 200
    assert b"ok" in payload


def test_auth_rejected_without_or_wrong_token(sandbox: SimpleNamespace) -> None:
    status, _ = sandbox.client.call("GET", "/health", token=None)
    assert status == 401
    status, _ = sandbox.client.call("GET", "/screen", token="deadbeef" * 4)
    assert status == 401
    # No Authorization header at all, not just a wrong one.
    req = urllib.request.Request(sandbox.client.base_url + "/health")
    try:
        urllib.request.urlopen(req, timeout=10)
        raise AssertionError("request without Authorization must fail")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401


def test_screen_geometry(sandbox: SimpleNamespace) -> None:
    status, payload = sandbox.client.call("GET", "/screen")
    assert status == 200
    data = json.loads(payload)
    assert data == {"width": 1280, "height": 800}


def test_screenshot_valid_png_1280x800(sandbox: SimpleNamespace) -> None:
    png = sandbox.client.screenshot()
    assert len(png) > 1_000
    assert png_dimensions(png) == (1280, 800)


# --- commands ---------------------------------------------------------------------
def test_command_echo_and_exit_code(sandbox: SimpleNamespace) -> None:
    out = sandbox.client.command("printf hello")
    assert out["stdout"] == "hello" and out["exit_code"] == 0
    out = sandbox.client.command("exit 3")
    assert out["exit_code"] == 3


def test_command_output_truncation(sandbox: SimpleNamespace) -> None:
    out = sandbox.client.command("python3 -c \"print('a' * 30000)\"")
    assert len(out["stdout"]) == 8192
    out = sandbox.client.command(
        "python3 -c \"import sys; print('e' * 30000, file=sys.stderr)\""
    )
    assert len(out["stderr"]) == 8192


def test_command_timeout_kills_group(sandbox: SimpleNamespace) -> None:
    started = time.monotonic()
    out = sandbox.client.command("sleep 30 & sleep 30", timeout_s=1)
    elapsed = time.monotonic() - started
    assert out["timed_out"] is True
    assert out["exit_code"] != 0
    assert elapsed < 10, "timeout must not wait for the full sleep"
    # Background children in the same group must be gone.
    left = str(
        sandbox.client.command("pgrep -f 'sleep 30' >/dev/null && echo alive || true")["stdout"]
    ).strip()
    assert left != "alive", "stray processes survived the timeout"


# --- files -----------------------------------------------------------------------
def test_file_write_read_roundtrip(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    payload = secrets.token_bytes(1_048_576)
    digest = hashlib.sha256(payload).hexdigest()
    client.write_file("/home/agent/Shared/selftest.bin", payload)
    status, back = client.read_file("/home/agent/Shared/selftest.bin")
    assert status == 200
    assert hashlib.sha256(back).hexdigest() == digest
    listing = client.json(*client.call("GET", "/files/list?path=%2Fhome%2Fagent%2FShared"))
    names = {entry["name"] for entry in listing["entries"]}
    assert "selftest.bin" in names


def test_files_read_missing_is_404(sandbox: SimpleNamespace) -> None:
    status, _ = sandbox.client.read_file("/home/agent/Shared/definitely-missing.bin")
    assert status == 404


def test_files_reject_relative_paths(sandbox: SimpleNamespace) -> None:
    status, _ = sandbox.client.read_file("relative/path.txt")
    assert status == 422
    status, _ = sandbox.client.call(
        "POST", "/files/write", raw=_multipart_raw("relative/path.txt", b"nope")
    )
    assert status == 422


def test_files_write_rejects_oversize(sandbox: SimpleNamespace) -> None:
    status, _ = sandbox.client.call(
        "POST",
        "/files/write",
        raw=_multipart_raw("/home/agent/Shared/big.bin", b"x" * (51 * 1024 * 1024)),
    )
    assert status == 413


# --- input ------------------------------------------------------------------------
def test_scroll_does_not_error(sandbox: SimpleNamespace) -> None:
    sandbox.client.scroll(640, 400, "down", 3)
    sandbox.client.scroll(640, 400, "up", 1)


def test_type_text_into_mousepad_and_read_back(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    path = "/home/agent/Shared/selftest-typed.txt"
    close_mousepad(client)
    launch_mousepad(client, path)
    wait_for_window(client, "Mousepad")
    x, y = window_center(client)
    client.click(x, y)  # place the caret inside the buffer
    client.type_text(TYPED_TEXT, interval_ms=5)
    client.key("ctrl+s")
    poll_file(client, path, TYPED_TEXT)
    close_mousepad(client)


def test_key_combo_select_all_and_replace(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    path = "/home/agent/Shared/selftest-combo.txt"
    close_mousepad(client)
    launch_mousepad(client, path)
    wait_for_window(client, "Mousepad")
    x, y = window_center(client)
    client.click(x, y)
    client.type_text("0123456789", interval_ms=5)
    client.key("ctrl+a")
    client.type_text("REPLACED", interval_ms=5)
    client.key("ctrl+s")
    poll_file(client, path, "REPLACED")
    close_mousepad(client)


def test_input_validation_errors(sandbox: SimpleNamespace) -> None:
    status, _ = sandbox.client.call("POST", "/type", body={"text": "x" * 60_000})
    assert status == 422
    status, _ = sandbox.client.call("POST", "/type", body={"text": ""})
    assert status == 422
    status, _ = sandbox.client.call("POST", "/click", body={"x": 9999, "y": 10})
    assert status == 422
    status, _ = sandbox.client.call("POST", "/key", body={"combo": "ctrl+FOO_BAR"})
    assert status == 422


# --- noVNC -----------------------------------------------------------------------
def test_no_vnc_pages_served(sandbox: SimpleNamespace) -> None:
    for suffix in (
        "",
        "?autoconnect=1&resize=scale",
        "?view_only=1&autoconnect=1&resize=scale",
    ):
        with urllib.request.urlopen(
            sandbox.novnc + "/vnc.html" + suffix, timeout=10
        ) as resp:
            assert resp.status == 200
            body = resp.read().decode("utf-8", "replace")
        assert "noVNC" in body
