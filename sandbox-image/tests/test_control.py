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
    # -x: exact process name, so the bash -lc wrapper running this pkill
    # does not match its own cmdline ("pkill -f mousepad" kills itself).
    client.command("pkill -x mousepad >/dev/null 2>&1 || true")
    # Mousepad is single-instance over D-Bus: if a new mousepad starts while
    # the previous one is still shutting down, it forwards the file to the
    # dying instance and exits -> no window ever appears. Wait for exit.
    for _ in range(30):
        out = str(
            client.command("pgrep -x mousepad >/dev/null && echo running || true")["stdout"]
        ).strip()
        if out != "running":
            return
        time.sleep(0.3)


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
        # RFC 6750: challenge the client so it knows how to authenticate.
        assert exc.headers.get("WWW-Authenticate") == "Bearer"


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
    # Background children in the same group must be gone. (-fx: exact full
    # cmdline match, so the bash -lc wrapper running this pgrep does not
    # match its own argument text.)
    left = str(
        sandbox.client.command("pgrep -fx 'sleep 30' >/dev/null && echo alive || true")["stdout"]
    ).strip()
    assert left != "alive", "stray processes survived the timeout"


def test_backgrounded_command_returns_promptly(sandbox: SimpleNamespace) -> None:
    """A backgrounded child holding the pipes open must not wedge /command.

    ``bash -lc 'sleep 300 &'`` exits at once, but the orphaned sleep keeps
    stdout/stderr open for its whole lifetime: the old communicate()-until-
    EOF flow hung for the full 300 s.
    """
    client = sandbox.client
    started = time.monotonic()
    out = client.command("sleep 300 &", timeout_s=2)
    elapsed = time.monotonic() - started
    assert elapsed < 5, f"backgrounded child kept the call open for {elapsed:.1f}s"
    assert out["timed_out"] is False
    assert out["exit_code"] == 0
    # Hygiene: do not leave the orphan around for the rest of the session.
    client.command("pkill -fx 'sleep 300' >/dev/null 2>&1 || true")


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


def test_files_list_trailing_slash_and_on_file(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    client.write_file("/home/agent/Shared/selftest-list.txt", b"list me")
    # A trailing slash names the same directory and must be accepted.
    listing = client.json(
        *client.call("GET", "/files/list?path=%2Fhome%2Fagent%2FShared%2F")
    )
    names = {entry["name"] for entry in listing["entries"]}
    assert "selftest-list.txt" in names
    # But listing a FILE is a 404 (it is not a directory).
    status, _ = client.call(
        "GET", "/files/list?path=%2Fhome%2Fagent%2FShared%2Fselftest-list.txt"
    )
    assert status == 404


def test_files_reject_relative_paths(sandbox: SimpleNamespace) -> None:
    status, _ = sandbox.client.read_file("relative/path.txt")
    assert status == 422
    status, _ = sandbox.client.call(
        "POST", "/files/write", raw=_multipart_raw("relative/path.txt", b"nope")
    )
    assert status == 422


def _post_oversize(sandbox: SimpleNamespace, path: str, *, chunked: bool) -> int:
    """POST a 51 MB upload and return the status, tolerating an early close.

    The server answers 413 before reading the body (Content-Length) or
    part-way through it (chunked) and then drops the connection, so the
    client may hit a broken pipe while sending; the response is still
    readable afterwards.
    """
    import http.client
    import urllib.parse

    url = urllib.parse.urlsplit(sandbox.client.base_url)
    body, ctype = _multipart_raw(path, b"x" * (51 * 1024 * 1024))
    conn = http.client.HTTPConnection(url.hostname or "127.0.0.1", url.port, timeout=60)
    try:
        conn.putrequest("POST", "/files/write")
        conn.putheader("Authorization", f"Bearer {sandbox.client.token}")
        conn.putheader("Content-Type", ctype)
        if chunked:
            conn.putheader("Transfer-Encoding", "chunked")
        else:
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders()
        try:
            for i in range(0, len(body), 1 << 20):
                part = body[i : i + (1 << 20)]
                if chunked:
                    part = f"{len(part):x}\r\n".encode() + part + b"\r\n"
                conn.send(part)
            if chunked:
                conn.send(b"0\r\n\r\n")
        except OSError:
            pass  # the server may close the connection once it has answered
        return conn.getresponse().status
    finally:
        conn.close()


def test_files_write_rejects_oversize(sandbox: SimpleNamespace) -> None:
    """Content-Length over the cap is rejected before the body is read."""
    assert _post_oversize(sandbox, "/home/agent/Shared/big.bin", chunked=False) == 413


def test_files_write_rejects_oversize_chunked(sandbox: SimpleNamespace) -> None:
    """Without Content-Length the byte-counting middleware must answer 413."""
    status = _post_oversize(sandbox, "/home/agent/Shared/big-chunked.bin", chunked=True)
    assert status == 413
    status, _ = sandbox.client.read_file("/home/agent/Shared/big-chunked.bin")
    assert status == 404, "a rejected upload must not leave a file behind"


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


def test_move_positions_the_pointer(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    client.move(200, 150)
    out = client.command("xdotool getmouselocation --shell")
    values = {
        line.partition("=")[0]: line.partition("=")[2]
        for line in str(out["stdout"]).splitlines()
        if "=" in line
    }
    assert values.get("X") == "200", f"pointer did not move: {values}"
    assert values.get("Y") == "150", f"pointer did not move: {values}"


def test_drag_rubber_band(sandbox: SimpleNamespace) -> None:
    # Rubber-band selection on the desktop: harmless with no icons there.
    sandbox.client.drag(200, 200, 420, 320)


def test_right_and_double_click(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    # Right click opens the XFCE desktop context menu; Escape (a named key
    # that was silently broken before the keysym fix) closes it again.
    client.click(640, 400, button="right")
    client.key("Escape")
    client.click(640, 400, count=2)  # double click on empty desktop space


def test_named_keys_backspace_and_return(sandbox: SimpleNamespace) -> None:
    """BackSpace and Return must actually edit the buffer (keysym casing)."""
    client = sandbox.client
    path = "/home/agent/Shared/selftest-keys.txt"
    close_mousepad(client)
    launch_mousepad(client, path)
    wait_for_window(client, "Mousepad")
    x, y = window_center(client)
    client.click(x, y)
    client.type_text("AB", interval_ms=5)
    client.key("backspace")  # "AB" -> "A"
    client.key("return")  # newline
    client.type_text("C", interval_ms=5)
    client.key("ctrl+s")
    poll_file(client, path, "A\nC")
    close_mousepad(client)


def test_unknown_and_punctuation_keys(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    # Punctuation maps to keysym names ("," -> comma) instead of being
    # silently dropped by xdotool; F-keys and Insert are accepted (run on
    # the bare desktop, where none of them has a side effect).
    for combo in ("ctrl+,", "ctrl+plus", "ctrl+minus", "F8", "Insert"):
        client.key("Escape")
        status, payload = client.call("POST", "/key", body={"combo": combo})
        assert status == 200, (combo, payload[:200])
    client.key("Escape")
    # A name that is not an X keysym is rejected, never reported as ok.
    status, _ = client.call("POST", "/key", body={"combo": "ctrl+notakey"})
    assert status == 422


# --- confinement / secret hygiene --------------------------------------------------
def test_files_confined_to_agent_home(sandbox: SimpleNamespace) -> None:
    client = sandbox.client
    for p in ("/etc/passwd", "/home/agent/../../etc/passwd", "/tmp/escape.bin"):
        status, _ = client.read_file(p)
        assert status == 422, f"{p} must be rejected"
    status, _ = client.call("GET", "/files/list?path=%2Fetc")
    assert status == 422
    # A symlink planted inside the sandbox home must not become a read lens
    # into the rest of the container.
    client.command("ln -sfn /etc/passwd /home/agent/Shared/.pwned-link")
    status, _ = client.read_file("/home/agent/Shared/.pwned-link")
    assert status == 422
    client.command("rm -f /home/agent/Shared/.pwned-link")


def test_command_env_has_no_control_token(sandbox: SimpleNamespace) -> None:
    out = sandbox.client.command("printenv CONTROL_TOKEN || true")
    assert str(out["stdout"]).strip() == ""


# --- hardening (review findings) -------------------------------------------------
def test_hardened_runtime(sandbox: SimpleNamespace) -> None:
    """The container must run under the same flags the T07 provider uses."""
    from conftest import _docker

    host = json.loads(_docker("inspect", "-f", "{{json .HostConfig}}", sandbox.name))
    assert host["CapDrop"] == ["ALL"]
    # Docker reports capabilities with or without the CAP_ prefix
    # depending on version; compare the bare names.
    cap_add = sorted(str(c).removeprefix("CAP_") for c in host["CapAdd"] or [])
    assert cap_add == ["SETGID", "SETUID"], (
        "the capability set must stay minimal: supervisord only needs "
        "SETUID/SETGID to drop to the agent user"
    )
    assert any(
        str(opt).startswith("no-new-privileges") for opt in host["SecurityOpt"] or []
    )
    # The healthcheck authenticates through the root-only header file; it
    # must reach "healthy" rather than flap or fail.
    deadline = time.monotonic() + 45
    status = ""
    while time.monotonic() < deadline:
        status = _docker("inspect", "-f", "{{.State.Health.Status}}", sandbox.name)
        if status == "healthy":
            break
        assert status != "unhealthy", "healthcheck failed (header file broken?)"
        time.sleep(2)
    assert status == "healthy", f"healthcheck never turned healthy (last={status!r})"


def test_token_absent_from_process_environments(sandbox: SimpleNamespace) -> None:
    """No process may carry CONTROL_TOKEN in its environment, incl. root's.

    The entrypoint relocates the token into the agent-owned 0400 file and
    unsets the variable before exec'ing supervisord — checked here for
    every process (supervisord, xfce4-session, control server, ...).
    """
    from conftest import _docker

    # Under --cap-drop ALL even root cannot read another uid's environ (no
    # CAP_SYS_PTRACE), so each uid's processes are scanned as that uid, and
    # an unreadable environ fails the test instead of passing silently.
    # Exception: gpg-agent (started by startxfce4) marks itself
    # non-dumpable, so not even its own uid may read its environ.
    # `docker exec` processes inherit the container's Config.Env (which
    # still holds the -e CONTROL_TOKEN value), so the scanner runs without
    # it; its own short-lived helpers are skipped by pid.
    scan = (
        'me=$(id -u); n=0; for d in /proc/[0-9]*; do p=${d#/proc/}; '
        '[ "$p" = "$$" ] && continue; '
        '[ "$(stat -c %u "$d" 2>/dev/null)" = "$me" ] || continue; '
        'env=$(tr "\\0" "\\n" < "$d/environ" 2>/dev/null) || '
        '{ c=$(cat "$d/comm" 2>/dev/null) || continue; '
        '[ "$c" = gpg-agent ] || echo "UNREADABLE $d $c"; continue; }; '
        '[ -n "$env" ] || continue; n=$((n+1)); '
        'printf "%s\\n" "$env" | grep -q "^CONTROL_TOKEN=" && echo "LEAK $d $(cat $d/comm)"; '
        'done; echo "SCANNED $n"'
    )
    scanned: dict[str, int] = {}
    for user in ("root", "agent"):
        out = _docker(
            "exec", "-u", user, sandbox.name, "env", "-u", "CONTROL_TOKEN", "bash", "-c", scan
        )
        assert "LEAK" not in out, out
        assert "UNREADABLE" not in out, out
        scanned[user] = int(out.rsplit("SCANNED", 1)[1].split()[0])
    # supervisord (root) and the whole desktop (agent) must have been seen.
    assert scanned["root"] >= 1 and scanned["agent"] >= 5, scanned
    # The desktop session is what an agent-opened Terminal inherits from:
    # check it explicitly so a scan bug cannot hide a regression there.
    session_env = _docker(
        "exec",
        "-u",
        "agent",
        sandbox.name,
        "env",
        "-u",
        "CONTROL_TOKEN",
        "bash",
        "-c",
        "tr '\\0' '\\n' < /proc/$(pgrep -xo xfce4-session)/environ",
    )
    assert "DISPLAY=" in session_env, "could not read xfce4-session's environment"
    assert "CONTROL_TOKEN=" not in session_env


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


# --- restart (keep last: it restarts the shared container) -----------------------
def test_survives_docker_restart(sandbox: SimpleNamespace) -> None:
    """The entrypoint must re-run cleanly over its own 0400 token file."""
    from conftest import _docker

    _docker("restart", "-t", "5", sandbox.name)
    deadline = time.monotonic() + 90
    status = ""
    while time.monotonic() < deadline:
        status = _docker("inspect", "-f", "{{.State.Health.Status}}", sandbox.name)
        if status == "healthy":
            break
        time.sleep(2)
    logs = _docker("logs", "--tail", "20", sandbox.name, check=False)
    assert status == "healthy", f"not healthy after restart ({status!r}); logs:\n{logs}"
