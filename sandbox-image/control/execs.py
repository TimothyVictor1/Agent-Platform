"""run_command: execute a command as the agent user via ``bash -lc``.

stdout/stderr are truncated to 8 KB each (SPEC §9.1): output is drained in a
bounded loop that keeps the first 8 KB and discards the rest, so even a
runaway producer cannot grow memory. The command runs in its own process
group (``start_new_session=True``, so the group id is ``proc.pid``) and the
whole group is killed on timeout.

Completion is decided by the *shell* exiting, not by the pipes reaching
EOF: ``bash -lc 'firefox &'`` exits at once while firefox keeps the pipes
open. (``Process.wait()`` cannot be used for this: asyncio only resolves it
once every pipe has closed.) Output is then collected for at most
COLLECT_TIMEOUT_S; pipes still held by background processes keep being
drained (and discarded) by detached tasks, so those processes never hit
SIGPIPE and the pipe file descriptors close when they exit.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal

from .config import (
    DEFAULT_COMMAND_TIMEOUT_S,
    MAX_COMMAND_CHARS,
    MAX_COMMAND_TIMEOUT_S,
    MAX_OUTPUT_BYTES,
)

# Bounds for the kill/drain choreography: how long a killed group may take
# to exit, and how long we wait for the pipe readers to reach EOF before
# returning what has been buffered so far.
KILL_GRACE_S = 2.0
DRAIN_AFTER_KILL_S = 5.0
COLLECT_TIMEOUT_S = 1.0
_POLL_INTERVAL_S = 0.05


class CommandOutcome:
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool

    def __init__(self, stdout: bytes, stderr: bytes, exit_code: int, timed_out: bool) -> None:
        self.stdout = _decode(stdout)
        self.stderr = _decode(stderr)
        self.exit_code = exit_code
        self.timed_out = timed_out

    def asdict(self) -> dict[str, object]:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
        }


def _decode(data: bytes) -> str:
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
        raise ValueError(f"timeout_s must be in 1..{MAX_COMMAND_TIMEOUT_S}")
    return timeout_s


def _command_env() -> dict[str, str]:
    env = dict(os.environ)
    # The control token must not be handed to model-supplied commands; the
    # agent user could read it back with printenv. (It remains a
    # per-container capability by design, but there is no reason to
    # re-export it here.)
    env.pop("CONTROL_TOKEN", None)
    env.setdefault("HOME", "/home/agent")
    env["USER"] = "agent"
    env["LOGNAME"] = "agent"
    env.setdefault("LANG", "C.UTF-8")
    env.setdefault("TERM", "xterm")
    return env


def _kill_group(pid: int, sig: signal.Signals) -> None:
    """Signal the process group; it may already be gone."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


async def _terminate_group(pid: int) -> None:
    """SIGTERM the group, then SIGKILL anything that survives."""
    _kill_group(pid, signal.SIGTERM)
    deadline: float | None = None
    while True:
        try:
            os.killpg(pid, 0)  # liveness probe: raises when the group is gone
        except (ProcessLookupError, PermissionError):
            return
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + KILL_GRACE_S
        elif asyncio.get_running_loop().time() > deadline:
            _kill_group(pid, signal.SIGKILL)
            return
        await asyncio.sleep(_POLL_INTERVAL_S)


async def _drain(stream: asyncio.StreamReader, buf: bytearray) -> None:
    """Read a pipe until EOF, keeping the first MAX_OUTPUT_BYTES bytes."""
    while True:
        chunk = await stream.read(65_536)
        if not chunk:
            return
        if len(buf) < MAX_OUTPUT_BYTES:
            buf.extend(chunk[: MAX_OUTPUT_BYTES - len(buf)])
        # else: discard, so a runaway producer cannot grow memory


async def _wait_for_exit(proc: asyncio.subprocess.Process, limit_s: float) -> bool:
    """Poll until the shell itself has been reaped; False on timeout.

    ``proc.returncode`` is set as soon as the child is reaped, independent
    of the pipes, unlike ``proc.wait()``.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + limit_s
    while proc.returncode is None:
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(_POLL_INTERVAL_S)
    return True


# Drain tasks that outlive their request (pipes held by background
# processes). Strong references so they are not garbage-collected mid-read.
_DETACHED_DRAINS: set[asyncio.Task[None]] = set()


async def _collect(tasks: set[asyncio.Task[None]]) -> None:
    """Give the drains COLLECT_TIMEOUT_S to finish, then detach the rest."""
    _done, pending = await asyncio.wait(tasks, timeout=COLLECT_TIMEOUT_S)
    for task in pending:
        _DETACHED_DRAINS.add(task)
        task.add_done_callback(_DETACHED_DRAINS.discard)


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
    out_buf = bytearray()
    err_buf = bytearray()
    if proc.stdout is None or proc.stderr is None:  # pragma: no cover - PIPE set above
        raise RuntimeError("subprocess pipes unavailable")
    drains = {
        asyncio.create_task(_drain(proc.stdout, out_buf)),
        asyncio.create_task(_drain(proc.stderr, err_buf)),
    }
    timed_out = False
    try:
        if not await _wait_for_exit(proc, timeout):
            timed_out = True
            await _terminate_group(proc.pid)
            await _wait_for_exit(proc, DRAIN_AFTER_KILL_S)
        await _collect(drains)
    except BaseException:
        # Cancellation (or any other failure) must not orphan the group.
        _kill_group(proc.pid, signal.SIGKILL)
        for task in drains:
            if not task.done():
                _DETACHED_DRAINS.add(task)
                task.add_done_callback(_DETACHED_DRAINS.discard)
        raise
    # Snapshot: detached drains only discard from here on (or top up a
    # buffer nobody reads any more).
    stdout, stderr = bytes(out_buf), bytes(err_buf)
    exit_code = 124 if timed_out else proc.returncode if proc.returncode is not None else -1
    return CommandOutcome(stdout, stderr, exit_code, timed_out)
