"""Unit tests for the e2b sandbox provider with the E2B Desktop SDK mocked (no network)."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import structlog
from e2b import (
    AuthenticationException,
    CommandExitException,
    CommandResult,
    FileNotFoundException,
    FileType,
    RateLimitException,
    SandboxException,
    SandboxNotFoundException,
    SandboxNotRunningException,
    SandboxUnreachableException,
    TimeoutException,
)
from pydantic import SecretStr

from agentplatform.contracts.sandbox import COMMAND_OUTPUT_LIMIT, SandboxSpec
from agentplatform.errors import SandboxError, SandboxExpiredError
from agentplatform.sandboxes.base import SandboxHandle, SandboxProvider
from agentplatform.sandboxes.e2b_provider import (
    E2BProvider,
    E2BSandbox,
    _map_error,
    make_e2b_contract_provider,
)

FAKE_KEY = "e2b_0123456789abcdef0123456789abcdef01234567"  # gitleaks:allow
PNG = b"\x89PNG\r\n\x1a\nfake"
T0 = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

# --- fake SDK --------------------------------------------------------------------------


@dataclass
class FakeStream:
    started_with: dict[str, Any] | None = None
    fail: bool = False

    def start(self, **kwargs: Any) -> None:
        if self.fail:
            raise TimeoutException("Could not start noVNC server")
        self.started_with = kwargs

    def get_auth_key(self) -> str:
        return "streampw"

    def get_url(self, view_only: bool = False, auth_key: str | None = None) -> str:
        url = f"https://6080-sbx.e2b.app/vnc.html?password={auth_key}"
        return url + "&view_only=true" if view_only else url


class FakeHandle:
    def __init__(self) -> None:
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


class FakeCommands:
    def __init__(self, sbx: FakeDesktop) -> None:
        self._sbx = sbx
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.handler: Callable[[str], CommandResult] | None = None

    def run(self, cmd: str, **kwargs: Any) -> Any:
        self.calls.append((cmd, kwargs))
        if kwargs.get("background"):
            return FakeHandle()
        if self._sbx.error is not None:
            raise self._sbx.error
        if "netstat" in cmd:
            return CommandResult(
                stdout="tcp 0.0.0.0:6080 LISTEN", stderr="", exit_code=0, error=None
            )
        if self.handler is not None:
            res = self.handler(cmd)
            if res.exit_code != 0:
                raise CommandExitException(
                    stdout=res.stdout, stderr=res.stderr, exit_code=res.exit_code, error=None
                )
            return res
        return CommandResult(stdout="", stderr="", exit_code=0, error=None)


class FakeFiles:
    def __init__(self, sbx: FakeDesktop) -> None:
        self._sbx = sbx
        self.data: dict[str, bytes] = {}
        self.dirs: set[str] = {"/", "/home/user"}

    def _guard(self) -> None:
        if self._sbx.error is not None:
            raise self._sbx.error

    def write(self, path: str, data: bytes) -> None:
        self._guard()
        self.data[path] = data

    def get_info(self, path: str) -> SimpleNamespace:
        self._guard()
        if path in self.dirs:
            return SimpleNamespace(type=FileType.DIR, size=0)
        if path in self.data:
            return SimpleNamespace(type=FileType.FILE, size=len(self.data[path]))
        raise FileNotFoundException(f"path '{path}' does not exist")

    def read(self, path: str, format: str = "text") -> bytearray:
        self._guard()
        assert format == "bytes"
        return bytearray(self.data[path])

    def list(self, path: str, depth: int = 1) -> list[SimpleNamespace]:
        self._guard()
        assert depth == 1
        out = []
        for p, d in self.data.items():
            parent, _, name = p.rpartition("/")
            if (parent or "/") == path:
                out.append(
                    SimpleNamespace(
                        path=p,
                        name=name,
                        type=FileType.FILE,
                        size=len(d),
                        modified_time=datetime(2026, 1, 1),  # naive, as some SDK builds return
                    )
                )
        return out


class FakeDesktop:
    def __init__(self, sandbox_id: str = "sbx-1", end_at: datetime = T0) -> None:
        self.sandbox_id = sandbox_id
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.error: BaseException | None = None
        self.stream = FakeStream()
        self.commands = FakeCommands(self)
        self.files = FakeFiles(self)
        self.end_at = end_at
        self.killed = False
        self.kill_error: BaseException | None = None

    def _rec(self, name: str, *args: Any, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append((name, args, kwargs))

    def screenshot(self) -> bytearray:
        self._rec("screenshot")
        return bytearray(PNG)

    def move_mouse(self, x: int, y: int) -> None:
        self._rec("move_mouse", x, y)

    def left_click(self, x: int | None = None, y: int | None = None) -> None:
        self._rec("left_click", x, y)

    def right_click(self, x: int | None = None, y: int | None = None) -> None:
        self._rec("right_click", x, y)

    def middle_click(self, x: int | None = None, y: int | None = None) -> None:
        self._rec("middle_click", x, y)

    def double_click(self, x: int | None = None, y: int | None = None) -> None:
        self._rec("double_click", x, y)

    def scroll(self, direction: str = "down", amount: int = 1) -> None:
        self._rec("scroll", direction, amount)

    def drag(self, fr: tuple[int, int], to: tuple[int, int]) -> None:
        self._rec("drag", fr, to)

    def write(self, text: str, *, chunk_size: int = 25, delay_in_ms: int = 75) -> None:
        self._rec("write", text, delay_in_ms=delay_in_ms)

    def press(self, key: str | list[str]) -> None:
        self._rec("press", key)

    def pause(self) -> bool:
        self._rec("pause")
        return True

    def kill(self) -> bool:
        if self.kill_error is not None:
            raise self.kill_error
        self.killed = True
        return True

    def get_info(self) -> SimpleNamespace:
        return SimpleNamespace(end_at=self.end_at)

    def get_screen_size(self) -> tuple[int, int]:
        return (1024, 768)

    def get_host(self, port: int) -> str:
        return f"{port}-{self.sandbox_id}.e2b.app"

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


@dataclass
class FakeSdk:
    """Stands in for the ``e2b_desktop.Sandbox`` class."""

    desktop: FakeDesktop = field(default_factory=FakeDesktop)
    create_kwargs: dict[str, Any] | None = None
    create_error: BaseException | None = None
    create_gate: threading.Event | None = None
    connect_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    connect_error: BaseException | None = None
    list_kwargs: dict[str, Any] | None = None
    list_error: BaseException | None = None

    def create(self, **kwargs: Any) -> FakeDesktop:
        self.create_kwargs = kwargs
        if self.create_gate is not None:
            self.create_gate.wait(5)
        if self.create_error is not None:
            raise self.create_error
        return self.desktop

    def connect(self, sandbox_id: str, **kwargs: Any) -> FakeDesktop:
        self.connect_calls.append((sandbox_id, kwargs))
        if self.connect_error is not None:
            raise self.connect_error
        return self.desktop

    def list(self, **kwargs: Any) -> SimpleNamespace:
        self.list_kwargs = kwargs
        if self.list_error is not None:
            raise self.list_error
        return SimpleNamespace(next_items=lambda: [])


def rate_limited(msg: str) -> RateLimitException:
    """``RateLimitException.__init__`` is unannotated in the SDK."""
    exc: RateLimitException = cast(Any, RateLimitException)(msg)
    return exc


async def _eventually(check: Callable[[], bool], tries: int = 200) -> bool:
    for _ in range(tries):
        if check():
            return True
        await asyncio.sleep(0.01)
    return False


class Clock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# --- fixtures --------------------------------------------------------------------------


@pytest.fixture
def sdk() -> FakeSdk:
    return FakeSdk()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def provider(sdk: FakeSdk, clock: Clock) -> E2BProvider:
    return E2BProvider(SecretStr(FAKE_KEY), sdk=sdk, clock=clock)


@pytest.fixture
async def handle(provider: E2BProvider) -> E2BSandbox:
    return await provider.create(SandboxSpec(screen=(1280, 800), timeout_minutes=60))


# --- protocol conformance --------------------------------------------------------------


async def test_provider_and_handle_satisfy_protocols(
    provider: E2BProvider, handle: E2BSandbox
) -> None:
    assert isinstance(provider, SandboxProvider)
    assert isinstance(handle, SandboxHandle)
    assert provider.name == "e2b"
    assert handle.provider == "e2b"
    assert handle.id == "sbx-1"
    assert handle.screen_size == (1280, 800)


# --- create ----------------------------------------------------------------------------


async def test_create_passes_resolution_timeout_env_copy_and_key(
    provider: E2BProvider, sdk: FakeSdk
) -> None:
    env = {"LANG": "C.UTF-8"}
    spec = SandboxSpec(
        screen=(1440, 900), timeout_minutes=30, env=env, session_id="s1", agent_id="a1"
    )
    await provider.create(spec)
    kw = sdk.create_kwargs
    assert kw is not None
    assert kw["resolution"] == (1440, 900)
    assert kw["timeout"] == 30 * 60
    assert kw["envs"] == {"LANG": "C.UTF-8"}
    assert kw["envs"] is not env  # the SDK mutates the dict it gets (adds DISPLAY)
    assert kw["api_key"] == FAKE_KEY
    assert FAKE_KEY not in kw["envs"].values()
    assert kw["metadata"] == {"agentplatform.session": "s1", "agentplatform.agent": "a1"}
    assert "network" not in kw


async def test_create_allowlist_maps_to_network_rules(provider: E2BProvider, sdk: FakeSdk) -> None:
    await provider.create(SandboxSpec(network="allowlist", allowlist=["example.com", "1.1.1.1"]))
    assert sdk.create_kwargs is not None
    assert sdk.create_kwargs["network"] == {
        "allow_out": ["example.com", "1.1.1.1"],
        "deny_out": ["0.0.0.0/0"],
    }


async def test_create_refuses_env_containing_the_api_key(
    provider: E2BProvider, sdk: FakeSdk
) -> None:
    with pytest.raises(SandboxError, match="API key") as info:
        await provider.create(SandboxSpec(env={"X": f"prefix-{FAKE_KEY}"}))
    assert FAKE_KEY not in str(info.value)
    assert sdk.create_kwargs is None


async def test_create_without_key_raises() -> None:
    provider = E2BProvider(None, sdk=FakeSdk())
    with pytest.raises(SandboxError, match="E2B_API_KEY"):
        await provider.create(SandboxSpec())
    provider_blank = E2BProvider(SecretStr(""), sdk=FakeSdk())
    with pytest.raises(SandboxError, match="E2B_API_KEY"):
        await provider_blank.create(SandboxSpec())


async def test_create_starts_password_protected_stream(handle: E2BSandbox, sdk: FakeSdk) -> None:
    assert sdk.desktop.stream.started_with == {"require_auth": True}
    urls = await handle.stream_urls()
    assert "view_only=true" in urls.view_only_url
    assert "view_only" not in urls.interactive_url
    assert "password=streampw" in urls.interactive_url
    assert FAKE_KEY not in urls.view_only_url + urls.interactive_url
    assert "screenshot" in sdk.desktop.names()  # readiness check


async def test_create_kills_sandbox_when_stream_fails(provider: E2BProvider, sdk: FakeSdk) -> None:
    sdk.desktop.stream.fail = True
    with pytest.raises(SandboxError, match="create"):
        await provider.create(SandboxSpec())
    assert sdk.desktop.killed


async def test_create_maps_sdk_errors(provider: E2BProvider, sdk: FakeSdk) -> None:
    sdk.create_error = AuthenticationException(f"invalid key {FAKE_KEY}")
    with pytest.raises(SandboxError, match="rejected the API key") as info:
        await provider.create(SandboxSpec())
    assert FAKE_KEY not in str(info.value)


async def test_create_timeout_kills_late_sandbox(sdk: FakeSdk) -> None:
    gate = threading.Event()
    sdk.create_gate = gate
    provider = E2BProvider(SecretStr(FAKE_KEY), sdk=sdk, create_timeout_s=0.05)
    with pytest.raises(SandboxError, match="not ready"):
        await provider.create(SandboxSpec())
    assert not sdk.desktop.killed
    gate.set()  # the SDK call finishes after we gave up
    assert await _eventually(lambda: sdk.desktop.killed)
    assert sdk.desktop.stream.started_with is None  # stopped early, no stream started


async def test_create_sets_expiry_from_timeout(handle: E2BSandbox, clock: Clock) -> None:
    assert handle.expires_at == T0 + timedelta(minutes=60)
    assert handle.time_left() == timedelta(minutes=60)
    clock.now = T0 + timedelta(minutes=90)
    assert handle.time_left() == timedelta(0)


# --- input mapping ---------------------------------------------------------------------


async def test_screenshot_returns_png_bytes(handle: E2BSandbox) -> None:
    data = await handle.screenshot()
    assert isinstance(data, bytes)
    assert data == PNG


@pytest.mark.parametrize(
    ("button", "count", "expected"),
    [
        ("left", 1, ["move_mouse", "left_click"]),
        ("right", 1, ["move_mouse", "right_click"]),
        ("middle", 1, ["move_mouse", "middle_click"]),
        ("left", 2, ["move_mouse", "double_click"]),
    ],
)
async def test_click_maps_to_sdk(
    handle: E2BSandbox, sdk: FakeSdk, button: Any, count: int, expected: list[str]
) -> None:
    sdk.desktop.calls.clear()
    await handle.click(0, 0, button=button, count=count)
    assert sdk.desktop.names() == expected
    # Moves explicitly (the SDK's own click helpers skip the move when x or y is 0)
    # and then clicks at the pointer.
    assert sdk.desktop.calls[0][1] == (0, 0)
    assert sdk.desktop.calls[1][1] == (None, None)


@pytest.mark.parametrize(
    ("button", "count", "cmd"),
    [
        ("left", 3, "xdotool click --repeat 3 1"),
        ("right", 2, "xdotool click --repeat 2 3"),
        ("middle", 3, "xdotool click --repeat 3 2"),
    ],
)
async def test_multi_click_uses_xdotool(
    handle: E2BSandbox, sdk: FakeSdk, button: Any, count: int, cmd: str
) -> None:
    sdk.desktop.calls.clear()
    await handle.click(10, 20, button=button, count=count)
    assert sdk.desktop.names() == ["move_mouse"]
    assert sdk.desktop.commands.calls[-1][0] == cmd


@pytest.mark.parametrize(("x", "y"), [(-1, 0), (1280, 0), (0, 800), (5000, 5000)])
async def test_click_outside_screen_raises(
    handle: E2BSandbox, sdk: FakeSdk, x: int, y: int
) -> None:
    sdk.desktop.calls.clear()
    with pytest.raises(SandboxError, match="outside"):
        await handle.click(x, y)
    assert sdk.desktop.calls == []


@pytest.mark.parametrize(("button", "count"), [("left", 0), ("left", 4), ("back", 1)])
async def test_click_rejects_bad_arguments(handle: E2BSandbox, button: Any, count: int) -> None:
    with pytest.raises(SandboxError):
        await handle.click(1, 1, button=button, count=count)


async def test_move_and_drag(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.calls.clear()
    await handle.move(5, 6)
    await handle.drag(1, 2, 300, 400)
    assert sdk.desktop.calls == [
        ("move_mouse", (5, 6), {}),
        ("drag", ((1, 2), (300, 400)), {}),
    ]
    with pytest.raises(SandboxError, match="outside"):
        await handle.drag(1, 2, 1280, 400)


@pytest.mark.parametrize("direction", ["up", "down"])
async def test_scroll_vertical_uses_sdk(handle: E2BSandbox, sdk: FakeSdk, direction: Any) -> None:
    sdk.desktop.calls.clear()
    await handle.scroll(100, 200, direction, 3)
    assert sdk.desktop.calls == [
        ("move_mouse", (100, 200), {}),
        ("scroll", (direction, 3), {}),
    ]


@pytest.mark.parametrize(("direction", "btn"), [("left", 6), ("right", 7)])
async def test_scroll_horizontal_uses_xdotool(
    handle: E2BSandbox, sdk: FakeSdk, direction: Any, btn: int
) -> None:
    await handle.scroll(100, 200, direction, 4)
    assert sdk.desktop.commands.calls[-1][0] == f"xdotool click --repeat 4 {btn}"


@pytest.mark.parametrize("amount", [0, 51])
async def test_scroll_amount_bounds(handle: E2BSandbox, amount: int) -> None:
    with pytest.raises(SandboxError, match="amount"):
        await handle.scroll(1, 1, "down", amount)


async def test_type_text_passes_interval_and_unicode(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.calls.clear()
    await handle.type_text("héllo wörld ✓", interval_ms=25)
    assert sdk.desktop.calls == [("write", ("héllo wörld ✓",), {"delay_in_ms": 25})]


async def test_type_text_limits(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.calls.clear()
    with pytest.raises(SandboxError, match="max 2000"):
        await handle.type_text("x" * 2001)
    with pytest.raises(SandboxError, match="interval_ms"):
        await handle.type_text("x", interval_ms=-1)
    await handle.type_text("")
    assert sdk.desktop.calls == []


@pytest.mark.parametrize(
    ("combo", "parts"),
    [
        ("enter", ["enter"]),
        ("ctrl+l", ["ctrl", "l"]),
        ("ctrl+shift+t", ["ctrl", "shift", "t"]),
        ("alt+tab", ["alt", "tab"]),
        ("page_up", ["page_up"]),
        ("super", ["super"]),
        ("f5", ["f5"]),
    ],
)
async def test_key_combos_map_to_press_list(
    handle: E2BSandbox, sdk: FakeSdk, combo: str, parts: list[str]
) -> None:
    sdk.desktop.calls.clear()
    await handle.key(combo)
    assert sdk.desktop.calls == [("press", (parts,), {})]


@pytest.mark.parametrize("combo", ["", "ctrl+", "Ctrl+L", "a;reboot", "ctrl+$(id)", "a b"])
async def test_key_rejects_unknown_or_unsafe_names(
    handle: E2BSandbox, sdk: FakeSdk, combo: str
) -> None:
    sdk.desktop.calls.clear()
    with pytest.raises(SandboxError, match="unknown key"):
        await handle.key(combo)
    assert sdk.desktop.calls == []


async def test_key_failing_in_xdotool_is_sandbox_error(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.error = CommandExitException(
        stdout="", stderr="(symbol) No such key name 'nosuchkey'", exit_code=1, error=None
    )
    with pytest.raises(SandboxError, match="exit code 1"):
        await handle.key("nosuchkey")


# --- run_command -----------------------------------------------------------------------


def _ok(stdout: str = "", stderr: str = "", code: int = 0) -> CommandResult:
    return CommandResult(stdout=stdout, stderr=stderr, exit_code=code, error=None)


async def test_run_command_wraps_with_timeout_and_quotes(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.commands.handler = lambda _cmd: _ok("hi\n")
    res = await handle.run_command("echo hi; echo 'q' $(whoami)", timeout_s=7)
    assert res.stdout == "hi\n"
    assert res.exit_code == 0
    assert not res.timed_out
    cmd, kwargs = sdk.desktop.commands.calls[-1]
    assert cmd == "timeout -k 5 7 bash -lc 'echo hi; echo '\"'\"'q'\"'\"' $(whoami)'"
    assert kwargs["timeout"] == 7 + 15


async def test_run_command_nonzero_exit_is_returned(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.commands.handler = lambda _cmd: _ok("", "boom", 3)
    res = await handle.run_command("false")
    assert res.exit_code == 3
    assert res.stderr == "boom"
    assert not res.timed_out


async def test_run_command_timeout_exit_code(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.commands.handler = lambda _cmd: _ok("partial", "", 124)
    res = await handle.run_command("sleep 100", timeout_s=1)
    assert res.timed_out
    assert res.exit_code == 124
    assert res.stdout == "partial"


async def test_run_command_sdk_deadline_is_timeout(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.error = TimeoutException("deadline_exceeded")
    res = await handle.run_command("sleep 100", timeout_s=1)
    assert res.timed_out
    assert res.exit_code == 124


async def test_run_command_sandbox_gone_is_expired(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.error = SandboxNotRunningException("sandbox was not found")
    with pytest.raises(SandboxExpiredError):
        await handle.run_command("true")


async def test_run_command_unreachable_is_error_not_timeout(
    handle: E2BSandbox, sdk: FakeSdk
) -> None:
    sdk.desktop.error = SandboxUnreachableException("port is not open")
    with pytest.raises(SandboxError, match="unreachable"):
        await handle.run_command("true")


async def test_run_command_truncates_each_stream(handle: E2BSandbox, sdk: FakeSdk) -> None:
    big = "x" * (COMMAND_OUTPUT_LIMIT + 10)
    sdk.desktop.commands.handler = lambda _cmd: _ok(big, "e")
    res = await handle.run_command("yes")
    assert len(res.stdout) == COMMAND_OUTPUT_LIMIT
    assert res.stderr == "e"
    assert res.truncated
    sdk.desktop.commands.handler = lambda _cmd: _ok("o", big, 2)
    res = await handle.run_command("yes >&2")
    assert len(res.stderr) == COMMAND_OUTPUT_LIMIT
    assert res.truncated
    assert res.exit_code == 2


async def test_run_command_rejects_bad_timeout(handle: E2BSandbox) -> None:
    with pytest.raises(SandboxError, match="timeout_s"):
        await handle.run_command("true", timeout_s=0)


# --- files -----------------------------------------------------------------------------


async def test_file_roundtrip_and_listing(handle: E2BSandbox, sdk: FakeSdk) -> None:
    await handle.write_file("/home/user/b.txt", b"bee")
    await handle.write_file("/home/user/a.txt", b"a")
    assert await handle.read_file("/home/user/b.txt") == b"bee"
    items = await handle.list_dir("/home/user")
    assert [i.name for i in items] == ["a.txt", "b.txt"]
    assert items[1].size_bytes == 3
    assert not items[0].is_dir
    assert items[0].modified_at is not None
    assert items[0].modified_at.tzinfo is not None


async def test_file_paths_must_be_absolute(handle: E2BSandbox) -> None:
    with pytest.raises(SandboxError, match="absolute"):
        await handle.write_file("rel.txt", b"")
    with pytest.raises(SandboxError, match="absolute"):
        await handle.read_file("rel.txt")
    with pytest.raises(SandboxError, match="absolute"):
        await handle.list_dir("home")


async def test_read_file_errors(handle: E2BSandbox, sdk: FakeSdk) -> None:
    with pytest.raises(SandboxError, match="no such file"):
        await handle.read_file("/nope")
    with pytest.raises(SandboxError, match="is a directory"):
        await handle.read_file("/home/user")
    sdk.desktop.files.data["/big"] = b"x" * 11
    with pytest.raises(SandboxError, match="larger than 10"):
        await handle.read_file("/big", max_bytes=10)


async def test_list_dir_of_file_raises(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.files.data["/f"] = b"x"
    with pytest.raises(SandboxError, match="not a directory"):
        await handle.list_dir("/f")


async def test_write_file_size_cap(handle: E2BSandbox, sdk: FakeSdk) -> None:
    with pytest.raises(SandboxError, match="max 50000000"):
        await handle.write_file("/big", b"\0" * 50_000_001)
    assert "/big" not in sdk.desktop.files.data


# --- error mapping ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "kind", "fragment"),
    [
        (SandboxNotRunningException("x"), SandboxExpiredError, "reached its time limit"),
        (SandboxNotFoundException("x"), SandboxExpiredError, "no longer exists"),
        (FileNotFoundException("x"), SandboxError, "no such file"),
        (AuthenticationException("x"), SandboxError, "API key"),
        (rate_limited("x"), SandboxError, "rate limit"),
        (SandboxUnreachableException("x"), SandboxError, "unreachable"),
        (TimeoutException("x"), SandboxError, "timed out"),
        (SandboxException("x"), SandboxError, "e2b error"),
        (RuntimeError("x"), SandboxError, "RuntimeError"),
    ],
)
def test_map_error(exc: BaseException, kind: type[SandboxError], fragment: str) -> None:
    err = _map_error(exc, "act")
    assert type(err) is kind
    assert fragment in str(err)


@pytest.mark.parametrize(
    "exc",
    [
        AuthenticationException(f"bad key {FAKE_KEY}"),
        SandboxException(f"api_key={FAKE_KEY}"),
        RuntimeError(f"Bearer {FAKE_KEY}"),
        CommandExitException(stdout=FAKE_KEY, stderr=FAKE_KEY, exit_code=1, error=FAKE_KEY),
    ],
)
async def test_errors_never_contain_sdk_text_or_key(
    handle: E2BSandbox, sdk: FakeSdk, exc: BaseException
) -> None:
    sdk.desktop.error = exc
    with structlog.testing.capture_logs() as logs, pytest.raises(SandboxError) as info:
        await handle.screenshot()
    assert FAKE_KEY not in str(info.value)
    assert FAKE_KEY not in repr(logs)


async def test_errors_are_chained_for_debugging(handle: E2BSandbox, sdk: FakeSdk) -> None:
    original = rate_limited("slow down")
    sdk.desktop.error = original
    with pytest.raises(SandboxError) as info:
        await handle.move(1, 1)
    assert info.value.__cause__ is original


# --- lifecycle -------------------------------------------------------------------------


async def test_pause_resume(handle: E2BSandbox, sdk: FakeSdk, clock: Clock) -> None:
    await handle.pause()
    await handle.pause()  # no-op
    assert sdk.desktop.names().count("pause") == 1
    with pytest.raises(SandboxError, match="paused"):
        await handle.click(1, 1)
    with pytest.raises(SandboxError, match="paused"):
        await handle.screenshot()
    urls = await handle.stream_urls()  # cached URLs stay available while paused
    sdk.desktop.end_at = T0 + timedelta(hours=2)
    await handle.resume()
    assert sdk.connect_calls == [("sbx-1", {"api_key": FAKE_KEY})]
    assert handle.expires_at == T0 + timedelta(hours=2)
    await handle.click(1, 1)
    assert await handle.stream_urls() == urls  # memory restored: same x11vnc password
    await handle.resume()  # running: no-op
    assert len(sdk.connect_calls) == 1


async def test_resume_of_gone_sandbox_is_expired(handle: E2BSandbox, sdk: FakeSdk) -> None:
    await handle.pause()
    sdk.connect_error = SandboxNotFoundException("sandbox not found")
    with pytest.raises(SandboxExpiredError):
        await handle.resume()


async def test_kill_is_idempotent_and_expires_handle(handle: E2BSandbox, sdk: FakeSdk) -> None:
    await handle.kill()
    await handle.kill()
    assert sdk.desktop.killed
    for call in (
        handle.screenshot(),
        handle.click(1, 1),
        handle.run_command("true"),
        handle.read_file("/x"),
        handle.stream_urls(),
        handle.pause(),
        handle.resume(),
    ):
        with pytest.raises(SandboxExpiredError):
            await call


async def test_kill_of_already_gone_sandbox_succeeds(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.kill_error = SandboxNotFoundException("gone")
    await handle.kill()
    with pytest.raises(SandboxExpiredError):
        await handle.screenshot()


async def test_kill_failure_can_be_retried(handle: E2BSandbox, sdk: FakeSdk) -> None:
    sdk.desktop.kill_error = rate_limited("slow down")
    with pytest.raises(SandboxError, match="rate limit"):
        await handle.kill()
    sdk.desktop.kill_error = None
    await handle.kill()
    assert sdk.desktop.killed


# --- connect ---------------------------------------------------------------------------


async def test_connect_reads_expiry_and_screen(provider: E2BProvider, sdk: FakeSdk) -> None:
    sdk.desktop.end_at = datetime(2026, 11, 9, 12, 30)  # naive from the API → treated as UTC
    h = await provider.connect("sbx-1")
    assert sdk.connect_calls == [("sbx-1", {"api_key": FAKE_KEY})]
    assert h.id == "sbx-1"
    assert h.screen_size == (1024, 768)
    assert h.expires_at == T0 + timedelta(minutes=30)


@pytest.mark.parametrize(
    "exc", [SandboxNotFoundException("not found"), SandboxNotRunningException("was not found")]
)
async def test_connect_to_gone_sandbox_is_expired(
    provider: E2BProvider, sdk: FakeSdk, exc: BaseException
) -> None:
    sdk.connect_error = exc
    with pytest.raises(SandboxExpiredError):
        await provider.connect("sbx-gone")


async def test_connect_restarts_stream_with_fresh_password(
    provider: E2BProvider, sdk: FakeSdk
) -> None:
    h = await provider.connect("sbx-1")
    urls = await h.stream_urls()
    cmds = [c for c, _ in sdk.desktop.commands.calls]
    assert any(c.startswith("pkill -x x11vnc") and "'[n]ovnc_proxy'" in c for c in cmds)
    store = next(c for c in cmds if "-storepasswd" in c)
    password = store.split()[2]
    assert len(password) == 16
    assert password.isalnum()
    assert any("-usepw" in c and "-display :0" in c for c in cmds)
    novnc = [(c, kw) for c, kw in sdk.desktop.commands.calls if "novnc_proxy --vnc" in c]
    assert novnc
    assert novnc[0][1] == {"background": True, "timeout": 0}
    assert urls.view_only_url.startswith("https://6080-sbx-1.e2b.app/vnc.html?")
    assert "view_only=true" in urls.view_only_url
    assert "view_only" not in urls.interactive_url
    assert f"password={password}" in urls.interactive_url
    # Cached: a second call does not restart the stream.
    n = len(sdk.desktop.commands.calls)
    assert await h.stream_urls() == urls
    assert len(sdk.desktop.commands.calls) == n


async def test_concurrent_stream_urls_restart_once(provider: E2BProvider, sdk: FakeSdk) -> None:
    h = await provider.connect("sbx-1")
    a, b = await asyncio.gather(h.stream_urls(), h.stream_urls())
    assert a == b
    cmds = [c for c, _ in sdk.desktop.commands.calls if "-storepasswd" in c]
    assert len(cmds) == 1


async def test_connect_without_key_raises() -> None:
    with pytest.raises(SandboxError, match="E2B_API_KEY"):
        await E2BProvider(None, sdk=FakeSdk()).connect("x")


# --- expiry warning --------------------------------------------------------------------


async def test_expiry_warning_logged_once(handle: E2BSandbox, clock: Clock) -> None:
    with structlog.testing.capture_logs() as logs:
        await handle.screenshot()
        clock.now = T0 + timedelta(minutes=56)
        await handle.screenshot()
        await handle.screenshot()
    warnings = [e for e in logs if e["event"] == "sandbox_expiring"]
    assert len(warnings) == 1
    assert warnings[0]["seconds_left"] == 240
    assert warnings[0]["sandbox_id"] == "sbx-1"


# --- concurrency -----------------------------------------------------------------------


async def test_input_actions_do_not_interleave(handle: E2BSandbox, sdk: FakeSdk) -> None:
    """Two clicks run concurrently must not interleave move/click pairs."""
    sdk.desktop.calls.clear()
    await asyncio.gather(*(handle.click(i, i) for i in range(1, 6)))
    names = sdk.desktop.names()
    assert names == ["move_mouse", "left_click"] * 5


# --- health ----------------------------------------------------------------------------


async def test_health_ok(provider: E2BProvider, sdk: FakeSdk) -> None:
    status = await provider.health()
    assert status.ok
    assert status.latency_ms is not None
    assert sdk.list_kwargs is not None
    assert sdk.list_kwargs["api_key"] == FAKE_KEY
    assert sdk.list_kwargs["limit"] == 1


async def test_health_without_key() -> None:
    status = await E2BProvider(None, sdk=FakeSdk()).health()
    assert not status.ok
    assert status.error == "E2B_API_KEY is not set"


async def test_health_failure_does_not_raise_or_leak(provider: E2BProvider, sdk: FakeSdk) -> None:
    sdk.list_error = AuthenticationException(f"invalid {FAKE_KEY}")
    status = await provider.health()
    assert not status.ok
    assert status.error is not None
    assert "API key" in status.error
    assert FAKE_KEY not in status.error


# --- contract factory ------------------------------------------------------------------


def test_make_e2b_contract_provider_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("E2B_API_KEY", FAKE_KEY)
    provider = make_e2b_contract_provider()
    assert isinstance(provider, E2BProvider)
    assert provider.name == "e2b"
