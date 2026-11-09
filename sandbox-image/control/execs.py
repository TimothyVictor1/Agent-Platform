"""run_command: execute a command as the agent user via ``bash -lc``.

stdout/stderr are truncated to 8 KB each (SPEC §9.1); the command is run in
its own process group so the whole tree can be killed on timeout.
"""
from __future__ import annotations

import asyncio
import os
import signal
import time

from .config import (
    DEFAULT_COMMAND_TIMEOUT_S,
    MAX_COMMAND_CHARS,
    MAX_COMMAND_TIMEOUT_S,
    MAX_OUTPUT_BYTES,
)


class CommandOutcome:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool

    def __init__(
        self, stdout: bytes, stderr: bytes, exit_code: int, timed_out: bool
    ) -> None:
        self.stdout = _truncate(stdout)
        self.stderr = _truncate(stderr)
        self.exit_code = exit_code
        self.timed_out = timed_out

    def asdict(self) -> dict[str, object]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
        }


def _truncate(data: bytes) -> str:
    if len(data) > MAX_OUTPUT_BYTES:
        data = data[:MAX_OUTPUT_BYTES]
    return data.decode("utf-8", errors="replace")


def validate_command(cmd: str, timeout_s: int | None) -> int:
    if not cmd or not cmd.strip():
        raise ValueError("cmd must not be empty")
    if "\x00" in cmd:
        raise ValueError("cmd must not contain null bytes")
    if len(cmd) > MAX_COMMAND_CHARS:
        raise ValueError(f"cmd exceeds {MAX_COMMAND_CHARS} characters")
    if timeout_s is None:
        return DEFAULT_COMMAND_TIMEOUT_S
    if not 1 <= timeout_s <= MAX_COMMAND_TIMEOUT_S:
        raise ValueError(
            f"timeout_s must be in 1..{MAX_COMMAND_TIMEOUT_S}"
        )
    return timeout_s


def _command_env() -> dict[str, str]:
    env = dict(os.environ)
    # The control token must not be handed to model-supplied commands; the
    # agent user could read it back with printenv (it remains a per-container
    # capability by design, but there is no reason to re-export it here).
    env.pop("CONTROL_TOKEN", None)
    env.setdefault("HOME", "/home/agent")
    env["USER"] = "agent"
    env["LOGNAME"] = "agent"
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("TERM", "xterm")
    return env


async def run_command(cmd: str, timeout_s: int | None = None) -> CommandOutcome:
    timeout = validate_command(cmd, timeout_s)
    proc = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-lc",
        cmd,
        cwd="/home/agent",
        env=_command_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return CommandOutcome(
            stdout, stderr, proc.returncode if proc.returncode is not None else -1,
            timed_out=False,
        )
    except asyncio.TimeoutError:
        _kill_group(proc.pid)
        stdout, stderr = await proc.communicate()
        return CommandOutcome(stdout, stderr, 124, timed_out=True)


def _kill_group(pid: int) -> None:
    """SIGTERM the process group, then SIGKILL anything that survives."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        try:
            os.getpgid(pid)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
