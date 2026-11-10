"""Shared ``SandboxHandle`` / ``SandboxProvider`` contract suite (SPEC §9.4).

Every provider runs the same tests:

* ``fake``         always (``FakeSandboxProvider``, programmed for the commands below);
* ``docker_local`` only with ``APP_DOCKER=1`` (needs ``make sandbox-image``);
* ``e2b``          added by T08 (only with ``APP_LIVE=1`` and ``E2B_API_KEY``).

To keep the Docker run short, read-mostly tests share one module-scoped sandbox;
tests that pause or kill create their own. Tests that need a real desktop (typed
text must reach an editor) are skipped for the fake.
"""

from __future__ import annotations

import asyncio
import io
import shlex
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass

import pytest
import pytest_asyncio
from PIL import Image

from agentplatform.config import Settings
from agentplatform.contracts.sandbox import COMMAND_OUTPUT_LIMIT, CommandResult, SandboxSpec
from agentplatform.errors import SandboxError, SandboxExpiredError
from agentplatform.sandboxes.base import SandboxHandle, SandboxProvider
from agentplatform.testing.fake_sandbox import FakeSandbox, FakeSandboxProvider

pytestmark = pytest.mark.asyncio(loop_scope="module")

CONTRACT_SESSION = "contract-test"
WORK = "/home/agent/contract"

CMD_EXIT = "exit 3"
CMD_STDERR = "echo oops >&2; exit 1"
CMD_BIG = "head -c 20000 /dev/zero | tr '\\0' a"
CMD_SLEEP = "sleep 30"

# What the fake answers for the suite's commands (real providers run them).
FAKE_COMMANDS = {
    CMD_EXIT: CommandResult(exit_code=3),
    CMD_STDERR: CommandResult(stderr="oops\n", exit_code=1),
    CMD_BIG: CommandResult(stdout="a" * COMMAND_OUTPUT_LIMIT, exit_code=0, truncated=True),
    CMD_SLEEP: CommandResult(exit_code=124, timed_out=True),
}


@dataclass
class Target:
    name: str
    provider: SandboxProvider
    real_desktop: bool

    async def create(self) -> SandboxHandle:
        return await self.provider.create(SandboxSpec(session_id=CONTRACT_SESSION))


def _fake_provider() -> FakeSandboxProvider:
    def factory(spec: SandboxSpec) -> FakeSandbox:
        return FakeSandbox(screen_size=spec.screen, command_results=FAKE_COMMANDS)

    return FakeSandboxProvider(factory)


async def _make_target(name: str) -> Target:
    if name == "fake":
        return Target(name, _fake_provider(), real_desktop=False)
    if name == "docker_local":
        from agentplatform.sandboxes.docker_local import DockerLocalProvider

        # 120 s: a loaded dev machine can take ~60 s to boot the desktop.
        provider = DockerLocalProvider.from_settings(Settings(), ready_timeout_s=120)
        status = await provider.health()
        if not status.ok:
            pytest.fail(f"docker_local is not usable: {status.error}")
        return Target(name, provider, real_desktop=True)
    # T08: add the "e2b" branch here (E2BProvider from settings, real_desktop=True).
    raise AssertionError(f"unknown provider {name}")


PROVIDERS = [
    pytest.param("fake", id="fake"),
    pytest.param("docker_local", id="docker_local", marks=pytest.mark.docker),
    # T08: pytest.param("e2b", id="e2b", marks=pytest.mark.live),
]


@pytest_asyncio.fixture(scope="module", loop_scope="module", params=PROVIDERS)
async def target(request: pytest.FixtureRequest) -> Target:
    return await _make_target(str(request.param))


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def shared(target: Target) -> AsyncIterator[SandboxHandle]:
    handle = await target.create()
    try:
        yield handle
    finally:
        await handle.kill()


@pytest_asyncio.fixture(loop_scope="module")
async def fresh(target: Target) -> AsyncIterator[SandboxHandle]:
    handle = await target.create()
    try:
        yield handle
    finally:
        await handle.kill()


@pytest.fixture
def workdir() -> str:
    return f"{WORK}/{uuid.uuid4().hex[:8]}"


async def _eventually(
    check: Callable[[], Awaitable[bool]], timeout_s: float = 30.0, what: str = "condition"
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.4)
    pytest.fail(f"{what} not reached within {timeout_s} s")


# --- screen -------------------------------------------------------------------------


async def test_screenshot_is_png_of_screen_size(shared: SandboxHandle) -> None:
    png = await shared.screenshot()
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(io.BytesIO(png)) as image:
        assert image.size == shared.screen_size == (1280, 800)


async def test_stream_urls(shared: SandboxHandle) -> None:
    urls = await shared.stream_urls()
    assert urls.view_only_url.startswith(("http://", "https://"))
    assert urls.interactive_url.startswith(("http://", "https://"))
    assert "view_only" in urls.view_only_url
    assert urls.view_only_url != urls.interactive_url


async def test_coordinates_outside_screen_raise(shared: SandboxHandle) -> None:
    w, h = shared.screen_size
    with pytest.raises(SandboxError):
        await shared.click(w, 0)
    with pytest.raises(SandboxError):
        await shared.move(0, h)
    with pytest.raises(SandboxError):
        await shared.scroll(-1, 0, "down", 1)
    with pytest.raises(SandboxError):
        await shared.drag(0, 0, w + 10, 0)


async def test_scroll_and_pointer_do_not_error(shared: SandboxHandle) -> None:
    # Every step targets a new pointer position (see the same-position test below).
    await shared.move(600, 380)
    await shared.scroll(620, 390, "down", 3)
    await shared.scroll(630, 395, "up", 1)
    await shared.click(5, 5, "left", 1)
    await shared.key("escape")


# --- commands -----------------------------------------------------------------------


async def test_run_command_exit_codes(shared: SandboxHandle) -> None:
    ok = await shared.run_command("echo hello")
    assert ok.exit_code == 0
    assert ok.stdout == "hello\n"
    failed = await shared.run_command(CMD_EXIT)
    assert failed.exit_code == 3
    assert not failed.timed_out
    err = await shared.run_command(CMD_STDERR)
    assert err.exit_code == 1
    assert err.stderr == "oops\n"


async def test_run_command_truncates_output(shared: SandboxHandle) -> None:
    result = await shared.run_command(CMD_BIG)
    assert result.exit_code == 0
    assert result.truncated
    assert 0 < len(result.stdout) <= COMMAND_OUTPUT_LIMIT
    assert set(result.stdout) == {"a"}


async def test_run_command_timeout(shared: SandboxHandle) -> None:
    started = time.monotonic()
    result = await shared.run_command(CMD_SLEEP, timeout_s=1)
    assert result.timed_out
    assert result.exit_code == 124
    assert time.monotonic() - started < 20


# --- files --------------------------------------------------------------------------


async def test_file_write_read_list_roundtrip(shared: SandboxHandle, workdir: str) -> None:
    await shared.write_file(f"{workdir}/b.txt", b"hello contract\n")
    await shared.write_file(f"{workdir}/a.bin", bytes(range(256)))
    await shared.write_file(f"{workdir}/sub/c.txt", b"c")
    assert await shared.read_file(f"{workdir}/b.txt") == b"hello contract\n"
    assert await shared.read_file(f"{workdir}/a.bin") == bytes(range(256))

    listing = await shared.list_dir(workdir)
    assert [i.name for i in listing] == ["a.bin", "b.txt", "sub"]
    by_name = {i.name: i for i in listing}
    assert by_name["a.bin"].size_bytes == 256
    assert not by_name["b.txt"].is_dir
    assert by_name["sub"].is_dir
    assert by_name["b.txt"].path == f"{workdir}/b.txt"

    cat = await shared.run_command(f"cat {shlex.quote(workdir + '/b.txt')}")
    assert cat.stdout == "hello contract\n"


async def test_file_errors(shared: SandboxHandle, workdir: str) -> None:
    with pytest.raises(SandboxError):
        await shared.read_file(f"{workdir}/missing.txt")
    with pytest.raises(SandboxError):
        await shared.read_file("relative/path.txt")
    with pytest.raises(SandboxError):
        await shared.write_file("relative/path.txt", b"x")
    await shared.write_file(f"{workdir}/big.txt", b"x" * 100)
    with pytest.raises(SandboxError):
        await shared.read_file(f"{workdir}/big.txt", max_bytes=10)
    with pytest.raises(SandboxError):
        await shared.list_dir(f"{workdir}/big.txt")


# --- typing into a real editor --------------------------------------------------------


async def _open_mousepad(handle: SandboxHandle, path: str) -> tuple[int, int]:
    await handle.run_command("pkill -x mousepad >/dev/null 2>&1 || true")

    async def gone() -> bool:
        out = await handle.run_command("pgrep -x mousepad >/dev/null && echo running || true")
        return out.stdout.strip() != "running"

    await _eventually(gone, 10, "old mousepad exit")
    quoted = shlex.quote(path)
    await handle.run_command(f"mkdir -p $(dirname {quoted}); rm -f {quoted}; touch {quoted}")
    await handle.run_command(f"nohup mousepad {quoted} >/dev/null 2>&1 & disown; sleep 0.2")

    async def visible() -> bool:
        out = await handle.run_command("wmctrl -l || true")
        return "mousepad" in out.stdout.lower()

    await _eventually(visible, 30, "mousepad window")
    await handle.run_command("wmctrl -a Mousepad")
    await asyncio.sleep(0.4)
    win = await handle.run_command("xdotool search --onlyvisible --name Mousepad | tail -n1")
    geo = await handle.run_command(f"xdotool getwindowgeometry --shell {win.stdout.strip()}")
    values = {
        k.strip(): int(v) for k, _, v in (ln.partition("=") for ln in geo.stdout.splitlines()) if v
    }
    return values["X"] + values["WIDTH"] // 2, values["Y"] + values["HEIGHT"] // 2


async def _file_becomes(handle: SandboxHandle, path: str, want: str) -> None:
    async def check() -> bool:
        try:
            return (await handle.read_file(path)).decode("utf-8", "replace") == want
        except SandboxError:
            return False

    await _eventually(check, 15, f"{path} == {want!r}")


async def test_type_into_editor_with_key_combos(
    target: Target, shared: SandboxHandle, workdir: str
) -> None:
    if not target.real_desktop:
        pytest.skip("needs a real desktop: typed text must reach an editor")
    path = f"{workdir}/typed.txt"
    x, y = await _open_mousepad(shared, path)
    await shared.click(x, y)
    await shared.type_text("0123456789", interval_ms=5)
    await shared.key("ctrl+a")
    await shared.type_text("Typed by the contract suite", interval_ms=5)
    await shared.key("ctrl+s")
    await _file_becomes(shared, path, "Typed by the contract suite")
    out = await shared.run_command(f"cat {shlex.quote(path)}")
    assert out.stdout == "Typed by the contract suite"
    await shared.run_command("pkill -x mousepad >/dev/null 2>&1 || true")


async def test_pointer_action_at_current_position_is_prompt(
    target: Target, shared: SandboxHandle, request: pytest.FixtureRequest
) -> None:
    """Clicking the same spot twice is common for agents and must not stall."""
    if target.name == "docker_local":
        request.applymarker(
            pytest.mark.xfail(
                strict=True,
                raises=SandboxError,
                reason=(
                    "T01 bug: control/input.py runs `xdotool mousemove --sync`, which "
                    "stalls ~20 s when the pointer is already at the target"
                ),
            )
        )
    await shared.move(200, 210)
    started = time.monotonic()
    await shared.move(200, 210)
    await shared.click(200, 210)
    if time.monotonic() - started > 5:
        raise SandboxError("pointer action at the current position took longer than 5 s")


# --- lifecycle ----------------------------------------------------------------------


async def test_connect_reattaches(target: Target, shared: SandboxHandle) -> None:
    again = await target.provider.connect(shared.id)
    assert again.id == shared.id
    assert again.screen_size == shared.screen_size
    assert (await again.screenshot()).startswith(b"\x89PNG")


async def test_pause_and_resume(fresh: SandboxHandle) -> None:
    await fresh.pause()
    with pytest.raises(SandboxError):
        await fresh.click(10, 10)
    await fresh.resume()
    await fresh.resume()  # no-op when running
    await fresh.click(10, 10)
    assert (await fresh.screenshot()).startswith(b"\x89PNG")


async def test_kill_is_final_and_idempotent(target: Target, fresh: SandboxHandle) -> None:
    await fresh.kill()
    await fresh.kill()
    with pytest.raises(SandboxExpiredError):
        await fresh.screenshot()
    with pytest.raises(SandboxExpiredError):
        await fresh.run_command("true")
    with pytest.raises(SandboxExpiredError):
        await target.provider.connect(fresh.id)


async def test_health(target: Target) -> None:
    status = await target.provider.health()
    assert status.ok
