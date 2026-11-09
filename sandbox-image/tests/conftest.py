"""Fixtures for the control-server tests (SPEC §9.4, task T01).

Runs pytest against a real container started via the docker CLI. Uses only
the standard library plus pytest so it can run inside the backend dev env
(uv) without extra dependencies. Skipped entirely unless APP_DOCKER=1.

Env knobs:
    APP_DOCKER=1      required to run the suite
    APP_TEST_IMAGE    image to use            (default agentplatform/desktop:latest)
    APP_TEST_BUILD=0  never build the image   (default: build if missing)
"""
from __future__ import annotations

import http.client
import json
import os
import secrets
import string
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from types import SimpleNamespace

import pytest

IMAGE = os.environ.get("APP_TEST_IMAGE", "agentplatform/desktop:latest")
SANDBOX_IMAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEALTH_TIMEOUT_S = 150.0


requires_docker = pytest.mark.skipif(
    os.environ.get("APP_DOCKER") != "1",
    reason="set APP_DOCKER=1 to run the sandbox-image tests against Docker",
)


def _docker(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["docker", *args], capture_output=True, text=True, check=check
    )
    return proc.stdout.strip()


def _ensure_image() -> None:
    done = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True
    ).returncode == 0
    if done:
        return
    if os.environ.get("APP_TEST_BUILD", "1") != "0":
        subprocess.run(
            ["docker", "build", "-t", IMAGE, SANDBOX_IMAGE_DIR], check=True
        )


class ControlClient:
    """Tiny stdlib HTTP client for the in-sandbox control server."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    # -- generic ------------------------------------------------------------
    def call(
        self,
        method: str,
        path: str,
        *,
        token: str | None | object = "default",
        body: dict[str, object] | None = None,
        raw: tuple[bytes, str] | None = None,
        timeout: float = 60.0,
    ) -> tuple[int, bytes]:
        req = urllib.request.Request(self.base_url + path, method=method)
        if token is not None:
            tok = self.token if token == "default" else str(token)
            if tok:
                req.add_header("Authorization", f"Bearer {tok}")
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            req.add_header("Content-Type", "application/json")
        elif raw is not None:
            data, ctype = raw
            req.add_header("Content-Type", ctype)
            req.add_header("Content-Length", str(len(data)))
        try:
            with urllib.request.urlopen(req, data=data, timeout=timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json(self, status: int, payload: bytes) -> dict[str, object]:
        assert status == 200, (status, payload[:300])
        return json.loads(payload)

    # -- SandboxHandle-ish convenience ---------------------------------------
    def command(self, cmd: str, timeout_s: int = 60) -> dict[str, object]:
        return self.json(
            *self.call("POST", "/command", body={"cmd": cmd, "timeout_s": timeout_s})
        )

    def type_text(self, text: str, interval_ms: int = 5) -> None:
        status, payload = self.call(
            "POST", "/type", body={"text": text, "interval_ms": interval_ms}
        )
        assert status == 200, payload[:300]

    def key(self, combo: str) -> None:
        status, payload = self.call("POST", "/key", body={"combo": combo})
        assert status == 200, payload[:300]

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        status, payload = self.call(
            "POST", "/click", body={"x": x, "y": y, "button": button, "count": count}
        )
        assert status == 200, payload[:300]

    def scroll(self, x: int, y: int, direction: str, amount: int) -> None:
        status, payload = self.call(
            "POST",
            "/scroll",
            body={"x": x, "y": y, "direction": direction, "amount": amount},
        )
        assert status == 200, payload[:300]

    def screenshot(self) -> bytes:
        status, payload = self.call("GET", "/screenshot")
        assert status == 200, payload[:60]
        return payload

    def write_file(self, path: str, data: bytes, filename: str = "upload.bin") -> None:
        body, ctype = _multipart_body(path, filename, data)
        status, payload = self.call("POST", "/files/write", raw=(body, ctype))
        assert status == 200, payload[:300]

    def read_file(self, path: str) -> tuple[int, bytes]:
        return self.call("GET", f"/files/read?path={urllib.parse.quote(path)}")


def _multipart_body(
    path_field: str, filename: str, data: bytes
) -> tuple[bytes, str]:
    boundary = "----agentplatformt01" + "".join(
        secrets.choice(string.ascii_letters) for _ in range(16)
    )
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="path"\r\n\r\n'
            f"{path_field}\r\n"
        ).encode("utf-8"),
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8"),
        data,
        b"\r\n",
        f"--{boundary}--\r\n".encode("utf-8"),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _public_port(name: str, spec: str) -> str:
    out = _docker("port", name, spec)
    line = out.splitlines()[-1]
    return line.split("/")[-1].strip()  # "127.0.0.1:49153"


@pytest.fixture(scope="session")
def sandbox() -> SimpleNamespace:
    """One container for the whole test session; killed afterwards."""
    _ensure_image()
    name = "agentplatform-t01-selftest-" + secrets.token_hex(4)
    token = secrets.token_hex(32)
    started = time.monotonic()
    _docker(
        "run",
        "-d",
        "--name",
        name,
        "-e",
        f"CONTROL_TOKEN={token}",
        "-p",
        "127.0.0.1::7000",
        "-p",
        "127.0.0.1::6080",
        "--shm-size",
        "1g",
        "--memory",
        "3g",
        "--cpus",
        "2",
        IMAGE,
    )
    try:
        base = "http://" + _public_port(name, "7000/tcp")
        novnc = "http://" + _public_port(name, "6080/tcp")
        client = ControlClient(base, token)
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        last: tuple[int, bytes] | None = None
        while time.monotonic() < deadline:
            try:
                status, payload = client.call("GET", "/health", timeout=5.0)
            except (
                urllib.error.URLError,
                http.client.HTTPException,
                OSError,
            ):
                # Docker's port proxy accepts TCP before uvicorn binds, so
                # early polls surface as closed connections, not HTTP errors.
                last = None
                time.sleep(1.0)
                continue
            if status == 200:
                break
            last = (status, payload)
            time.sleep(1.0)
        else:
            logs = _docker("logs", "--tail", "80", name, check=False)
            raise AssertionError(
                f"container never became healthy (last={last}); logs:\n{logs}"
            )
        print(f"\n[startup] healthy after {time.monotonic() - started:.1f}s")
        yield SimpleNamespace(client=client, novnc=novnc, name=name)
    finally:
        subprocess.run(
            ["docker", "rm", "-f", name], capture_output=True, check=False
        )
