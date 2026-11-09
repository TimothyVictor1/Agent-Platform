"""``e2b`` sandbox provider: cloud desktops via the E2B Desktop SDK (SPEC §9.3, T08).

How the protocol maps onto the SDK (``e2b-desktop`` 2.x, sync API):

* Every SDK call is blocking, so it runs in ``asyncio.to_thread``. Composite input
  actions (move + click, drag) run as one thread call under a per-handle input lock so
  two actions never interleave; screenshots, commands and file calls stay concurrent.
* The SDK's own click helpers skip the pointer move when ``x`` or ``y`` is 0, so the
  handle always calls ``move_mouse`` first and then clicks at the pointer.
* The SDK only scrolls up/down; left/right use xdotool buttons 6/7. Only validated
  integers are ever formatted into those xdotool commands.
* ``run_command`` wraps the command in ``timeout -k 5 <t> bash -lc '<cmd>'`` so the
  sandbox kills the process group itself; the SDK's own deadline is a backstop.
* Streams are password-protected noVNC (``require_auth=True``); the password is made by
  the SDK per sandbox and is not a platform secret. ``Sandbox.connect`` does not
  rebuild the SDK's stream object, so after a reconnect the first ``stream_urls()``
  restarts x11vnc/noVNC with a fresh password (URLs handed out earlier stop working).
* SDK exceptions become ``SandboxError`` / ``SandboxExpiredError`` with short messages
  that never include SDK text, URLs or the API key.

The E2B API key comes only from ``E2B_API_KEY`` (``Settings.e2b_api_key``), is passed
per call as ``api_key=`` and is never put into the sandbox env, logs or errors.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import shlex
import string
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

import structlog
from e2b import (
    ALL_TRAFFIC,
    AuthenticationException,
    CommandExitException,
    FileNotFoundException,
    FileType,
    InvalidArgumentException,
    NotEnoughSpaceException,
    NotFoundException,
    RateLimitException,
    SandboxException,
    SandboxNotRunningException,
    SandboxUnreachableException,
    TimeoutException,
)
from e2b_desktop import Sandbox as DesktopSandbox
from pydantic import SecretStr

from agentplatform.config import Settings, load_settings
from agentplatform.contracts.common import HealthStatus, SandboxProviderName, utcnow
from agentplatform.contracts.sandbox import (
    COMMAND_OUTPUT_LIMIT,
    CommandResult,
    FileInfo,
    SandboxSpec,
    StreamUrls,
)
from agentplatform.errors import SandboxError, SandboxExpiredError
from agentplatform.sandboxes.base import MouseButton, ScrollDirection

__all__ = [
    "E2BProvider",
    "E2BSandbox",
    "make_e2b_contract_provider",
]

log = structlog.get_logger(__name__)

T = TypeVar("T")

PROVIDER_NAME: SandboxProviderName = "e2b"
DISPLAY = ":0"
VNC_PORT = 5900
STREAM_PORT = 6080
CREATE_TIMEOUT_S = 60.0
HEALTH_TIMEOUT_S = 10.0
EXPIRY_WARNING = timedelta(minutes=5)
COMMAND_KILL_AFTER_S = 5
COMMAND_GRACE_S = 15
TIMEOUT_EXIT_CODE = 124
MAX_FILE_BYTES = 50_000_000
MAX_TYPE_TEXT_CHARS = 2000
MAX_TYPE_INTERVAL_MS = 1000
MAX_SCROLL_AMOUNT = 50
MAX_CLICK_COUNT = 3
STREAM_PASSWORD_LENGTH = 16
STREAM_START_WAIT_S = 10.0

_MOUSE_BUTTONS: dict[str, int] = {"left": 1, "middle": 2, "right": 3}
_SCROLL_BUTTONS: dict[str, int] = {"left": 6, "right": 7}
_KEY_PART = re.compile(r"^[a-z0-9_]+$")

SdkSandbox = Any
"""An ``e2b_desktop.Sandbox`` instance (the SDK ships no type information)."""

SdkFactory = Any
"""The ``e2b_desktop.Sandbox`` class (``create`` / ``connect`` / ``list``) or a test double."""

Clock = Callable[[], datetime]


# --- error mapping ---------------------------------------------------------------------


def _map_error(exc: BaseException, action: str) -> SandboxError:
    """Turn any SDK/transport exception into a typed, secret-free platform error."""
    if isinstance(exc, SandboxError):
        return exc
    if isinstance(exc, SandboxNotRunningException):
        return SandboxExpiredError(
            f"{action}: the e2b sandbox was killed or reached its time limit"
        )
    if isinstance(exc, FileNotFoundException):
        return SandboxError(f"{action}: no such file or directory")
    if isinstance(exc, NotFoundException):  # SandboxNotFoundException and the legacy base
        return SandboxExpiredError(f"{action}: the e2b sandbox no longer exists")
    if isinstance(exc, AuthenticationException):
        return SandboxError(f"{action}: e2b rejected the API key (check E2B_API_KEY)")
    if isinstance(exc, RateLimitException):
        return SandboxError(f"{action}: e2b rate limit reached; retry later")
    if isinstance(exc, SandboxUnreachableException):
        return SandboxError(f"{action}: the e2b sandbox is unreachable")
    if isinstance(exc, TimeoutException):
        return SandboxError(f"{action}: the e2b request timed out")
    if isinstance(exc, NotEnoughSpaceException):
        return SandboxError(f"{action}: the sandbox disk is full")
    if isinstance(exc, InvalidArgumentException):
        return SandboxError(f"{action}: e2b rejected an argument")
    if isinstance(exc, CommandExitException):
        return SandboxError(f"{action}: failed inside the sandbox (exit code {exc.exit_code})")
    if isinstance(exc, SandboxException):
        return SandboxError(f"{action}: e2b error")
    return SandboxError(f"{action}: unexpected error ({type(exc).__name__})")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _truncate(text: str) -> tuple[str, bool]:
    if len(text) > COMMAND_OUTPUT_LIMIT:
        return text[:COMMAND_OUTPUT_LIMIT], True
    return text, False


def _check_path(path: str) -> None:
    if not path.startswith("/") or "\x00" in path:
        raise SandboxError(f"path must be absolute: {path!r}")


def _key_parts(combo: str) -> list[str]:
    parts = combo.split("+")
    for part in parts:
        if not _KEY_PART.match(part):
            raise SandboxError(
                f"unknown key {part!r} in {combo!r}; use lower-case names like 'ctrl+l' or 'enter'"
            )
    return parts


def _stream_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(STREAM_PASSWORD_LENGTH))


@dataclass(frozen=True, slots=True)
class _Created:
    sbx: SdkSandbox
    urls: StreamUrls


# --- handle ----------------------------------------------------------------------------


class E2BSandbox:
    """``SandboxHandle`` backed by one E2B desktop sandbox."""

    def __init__(
        self,
        sbx: SdkSandbox,
        *,
        sdk: SdkFactory,
        api_key: SecretStr,
        screen_size: tuple[int, int],
        expires_at: datetime,
        stream: StreamUrls | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self._sbx = sbx
        self._id: str = str(sbx.sandbox_id)
        self._sdk = sdk
        self._api_key = api_key
        self._screen_size = screen_size
        self._expires_at = _aware(expires_at)
        self._stream = stream
        self._clock = clock
        self._input_lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._paused = False
        self._killed = False
        self._expiry_warned = False
        self._log = log.bind(sandbox_id=self._id, provider=PROVIDER_NAME)

    # -- properties -------------------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def provider(self) -> SandboxProviderName:
        return PROVIDER_NAME

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._screen_size

    @property
    def expires_at(self) -> datetime:
        """When E2B will end this sandbox (account-tier limit or the requested timeout)."""
        return self._expires_at

    def time_left(self) -> timedelta:
        """Time until ``expires_at`` (never negative)."""
        return max(timedelta(0), self._expires_at - self._clock())

    # -- plumbing ---------------------------------------------------------------------

    def _alive(self) -> None:
        if self._killed:
            raise SandboxExpiredError(f"sandbox {self._id} was killed")

    def _running(self) -> None:
        self._alive()
        if self._paused:
            raise SandboxError(f"sandbox {self._id} is paused; resume it first")

    def _note_expiry(self) -> None:
        if self._expiry_warned:
            return
        left = self.time_left()
        if left <= EXPIRY_WARNING:
            self._expiry_warned = True
            self._log.warning("sandbox_expiring", seconds_left=int(left.total_seconds()))

    async def _call(self, action: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        self._note_expiry()
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except Exception as exc:
            err = _map_error(exc, action)
            self._log.warning("sandbox_call_failed", action=action, error_type=type(exc).__name__)
            raise err from exc

    def _check_xy(self, x: int, y: int) -> None:
        w, h = self._screen_size
        if not (0 <= x < w and 0 <= y < h):
            raise SandboxError(f"coordinates ({x}, {y}) are outside the {w}x{h} screen")

    # -- screen and input ---------------------------------------------------------------

    async def screenshot(self) -> bytes:
        self._running()
        data = await self._call("screenshot", self._sbx.screenshot)
        return bytes(data)

    async def click(self, x: int, y: int, button: MouseButton = "left", count: int = 1) -> None:
        self._running()
        self._check_xy(x, y)
        if button not in _MOUSE_BUTTONS:
            raise SandboxError(f"unknown mouse button {button!r}")
        if not 1 <= count <= MAX_CLICK_COUNT:
            raise SandboxError(f"click count must be 1..{MAX_CLICK_COUNT}, got {count}")
        sbx = self._sbx

        def do() -> None:
            sbx.move_mouse(x, y)
            if count == 1:
                {"left": sbx.left_click, "right": sbx.right_click, "middle": sbx.middle_click}[
                    button
                ]()
            elif button == "left" and count == 2:
                sbx.double_click()
            else:
                sbx.commands.run(f"xdotool click --repeat {count} {_MOUSE_BUTTONS[button]}")

        async with self._input_lock:
            await self._call("click", do)

    async def move(self, x: int, y: int) -> None:
        self._running()
        self._check_xy(x, y)
        async with self._input_lock:
            await self._call("move", self._sbx.move_mouse, x, y)

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._running()
        self._check_xy(x1, y1)
        self._check_xy(x2, y2)
        async with self._input_lock:
            await self._call("drag", self._sbx.drag, (x1, y1), (x2, y2))

    async def scroll(self, x: int, y: int, direction: ScrollDirection, amount: int) -> None:
        self._running()
        self._check_xy(x, y)
        if direction not in ("up", "down", "left", "right"):
            raise SandboxError(f"unknown scroll direction {direction!r}")
        if not 1 <= amount <= MAX_SCROLL_AMOUNT:
            raise SandboxError(f"scroll amount must be 1..{MAX_SCROLL_AMOUNT}, got {amount}")
        sbx = self._sbx

        def do() -> None:
            sbx.move_mouse(x, y)
            if direction in ("up", "down"):
                sbx.scroll(direction, amount)
            else:
                sbx.commands.run(f"xdotool click --repeat {amount} {_SCROLL_BUTTONS[direction]}")

        async with self._input_lock:
            await self._call("scroll", do)

    async def type_text(self, text: str, interval_ms: int = 10) -> None:
        self._running()
        if len(text) > MAX_TYPE_TEXT_CHARS:
            raise SandboxError(f"text is {len(text)} characters (max {MAX_TYPE_TEXT_CHARS})")
        if not 0 <= interval_ms <= MAX_TYPE_INTERVAL_MS:
            raise SandboxError(f"interval_ms must be 0..{MAX_TYPE_INTERVAL_MS}, got {interval_ms}")
        if not text:
            return
        async with self._input_lock:
            await self._call("type_text", self._sbx.write, text, delay_in_ms=interval_ms)

    async def key(self, combo: str) -> None:
        self._running()
        parts = _key_parts(combo)
        async with self._input_lock:
            await self._call("key", self._sbx.press, parts)

    # -- commands and files -------------------------------------------------------------

    async def run_command(self, cmd: str, timeout_s: int = 60) -> CommandResult:
        self._running()
        if timeout_s < 1:
            raise SandboxError(f"timeout_s must be at least 1, got {timeout_s}")
        wrapped = f"timeout -k {COMMAND_KILL_AFTER_S} {int(timeout_s)} bash -lc {shlex.quote(cmd)}"
        sbx = self._sbx

        def do() -> CommandResult:
            try:
                res = sbx.commands.run(wrapped, timeout=timeout_s + COMMAND_GRACE_S)
                stdout, stderr, code = str(res.stdout), str(res.stderr), int(res.exit_code)
            except CommandExitException as exc:
                stdout, stderr, code = str(exc.stdout), str(exc.stderr), int(exc.exit_code)
            except TimeoutException as exc:
                if isinstance(exc, SandboxNotRunningException | SandboxUnreachableException):
                    raise
                # The SDK deadline fired before `timeout` could report back.
                return CommandResult(
                    stderr="command timed out", exit_code=TIMEOUT_EXIT_CODE, timed_out=True
                )
            out, out_cut = _truncate(stdout)
            err, err_cut = _truncate(stderr)
            return CommandResult(
                stdout=out,
                stderr=err,
                exit_code=code,
                truncated=out_cut or err_cut,
                timed_out=code == TIMEOUT_EXIT_CODE,
            )

        return await self._call("run_command", do)

    async def write_file(self, path: str, data: bytes) -> None:
        self._running()
        _check_path(path)
        if len(data) > MAX_FILE_BYTES:
            raise SandboxError(f"file is {len(data)} bytes (max {MAX_FILE_BYTES})")
        await self._call("write_file", self._sbx.files.write, path, data)

    async def read_file(self, path: str, max_bytes: int = MAX_FILE_BYTES) -> bytes:
        self._running()
        _check_path(path)
        sbx = self._sbx

        def do() -> bytes:
            info = sbx.files.get_info(path)
            if info.type == FileType.DIR:
                raise SandboxError(f"{path} is a directory")
            if int(info.size) > max_bytes:
                raise SandboxError(f"{path} is larger than {max_bytes} bytes")
            data = bytes(sbx.files.read(path, format="bytes"))
            if len(data) > max_bytes:  # grew between the size check and the read
                raise SandboxError(f"{path} is larger than {max_bytes} bytes")
            return data

        return await self._call("read_file", do)

    async def list_dir(self, path: str) -> list[FileInfo]:
        self._running()
        _check_path(path)
        sbx = self._sbx

        def do() -> list[FileInfo]:
            info = sbx.files.get_info(path)
            if info.type != FileType.DIR:
                raise SandboxError(f"not a directory: {path}")
            entries = sbx.files.list(path, depth=1)
            items = [
                FileInfo(
                    path=str(e.path),
                    name=str(e.name),
                    is_dir=e.type == FileType.DIR,
                    size_bytes=max(0, int(e.size)),
                    modified_at=_aware(e.modified_time) if e.modified_time else None,
                )
                for e in entries
            ]
            return sorted(items, key=lambda f: f.name)

        return await self._call("list_dir", do)

    # -- stream -------------------------------------------------------------------------

    async def stream_urls(self) -> StreamUrls:
        self._alive()
        async with self._stream_lock:
            if self._stream is None:
                self._running()
                self._stream = await self._call("stream_urls", _restart_stream, self._sbx)
            return self._stream

    # -- lifecycle ----------------------------------------------------------------------

    async def pause(self) -> None:
        async with self._state_lock:
            self._alive()
            if self._paused:
                return
            async with self._input_lock:
                await self._call("pause", self._sbx.pause)
            self._paused = True
            self._log.info("sandbox_paused")

    async def resume(self) -> None:
        async with self._state_lock:
            self._alive()
            if not self._paused:
                return
            key = self._api_key.get_secret_value()

            def do() -> tuple[SdkSandbox, datetime]:
                sbx = self._sdk.connect(self._id, api_key=key)
                return sbx, _aware(sbx.get_info().end_at)

            sbx, expires_at = await self._call("resume", do)
            # Memory is restored, so x11vnc keeps its password and old URLs keep working.
            self._sbx = sbx
            self._expires_at = expires_at
            self._expiry_warned = False
            self._paused = False
            self._log.info("sandbox_resumed")

    async def kill(self) -> None:
        async with self._state_lock:
            if self._killed:
                return
            try:
                await asyncio.to_thread(self._sbx.kill)
            except (NotFoundException, SandboxNotRunningException):
                pass  # already gone: that is what we wanted
            except Exception as exc:
                raise _map_error(exc, "kill") from exc
            self._killed = True
            self._log.info("sandbox_killed")


def _wait_for_port(sbx: SdkSandbox, port: int) -> bool:
    deadline = time.monotonic() + STREAM_START_WAIT_S
    while time.monotonic() < deadline:
        try:
            res = sbx.commands.run(f"netstat -tuln | grep ':{port} '")
            if str(res.stdout).strip():
                return True
        except CommandExitException:
            pass
        time.sleep(0.5)
    return False


def _restart_stream(sbx: SdkSandbox) -> StreamUrls:
    """(Re)start x11vnc + noVNC with a fresh password after ``Sandbox.connect``.

    Mirrors ``e2b_desktop``'s own stream start (which ``connect`` does not restore).
    The ``[x]`` patterns keep ``pkill -f`` from matching the shell running it.
    """
    password = _stream_password()
    sbx.commands.run(
        "pkill -x x11vnc; pkill -f '[n]ovnc_proxy'; pkill -f '[w]ebsockify'; mkdir -p ~/.vnc; true"
    )
    sbx.commands.run(f"x11vnc -storepasswd {password} ~/.vnc/passwd")
    sbx.commands.run(
        f"x11vnc -bg -display {DISPLAY} -forever -wait 50 -shared "
        f"-rfbport {VNC_PORT} -usepw 2>/tmp/x11vnc_stderr.log"
    )
    handle = sbx.commands.run(
        f"cd /opt/noVNC/utils && ./novnc_proxy --vnc localhost:{VNC_PORT} "
        f"--listen {STREAM_PORT} --web /opt/noVNC > /tmp/novnc.log 2>&1",
        background=True,
        timeout=0,
    )
    handle.disconnect()
    if not _wait_for_port(sbx, STREAM_PORT):
        raise SandboxError("stream_urls: the noVNC server did not start")
    base = f"https://{sbx.get_host(STREAM_PORT)}/vnc.html?autoconnect=true&resize=scale"
    return StreamUrls(
        view_only_url=f"{base}&view_only=true&password={password}",
        interactive_url=f"{base}&password={password}",
    )


# --- provider --------------------------------------------------------------------------


class E2BProvider:
    """``SandboxProvider`` for E2B cloud desktops.

    ``sdk`` and ``clock`` are injection points for tests; production uses the real
    ``e2b_desktop.Sandbox`` class and ``utcnow``.
    """

    def __init__(
        self,
        api_key: SecretStr | None,
        *,
        sdk: SdkFactory | None = None,
        create_timeout_s: float = CREATE_TIMEOUT_S,
        clock: Clock = utcnow,
    ) -> None:
        self._api_key = api_key if api_key and api_key.get_secret_value() else None
        self._sdk_override = sdk
        self._create_timeout_s = create_timeout_s
        self._clock = clock
        self._cleanup: set[asyncio.Future[None]] = set()

    @classmethod
    def from_settings(cls, settings: Settings) -> E2BProvider:
        return cls(settings.e2b_api_key)

    @property
    def name(self) -> SandboxProviderName:
        return PROVIDER_NAME

    @property
    def _sdk(self) -> SdkFactory:
        return self._sdk_override if self._sdk_override is not None else DesktopSandbox

    def _key(self) -> SecretStr:
        if self._api_key is None:
            raise SandboxError("E2B_API_KEY is not set; the e2b provider is unavailable")
        return self._api_key

    def _handle(
        self,
        sbx: SdkSandbox,
        key: SecretStr,
        screen: tuple[int, int],
        expires_at: datetime,
        stream: StreamUrls | None,
    ) -> E2BSandbox:
        return E2BSandbox(
            sbx,
            sdk=self._sdk,
            api_key=key,
            screen_size=screen,
            expires_at=expires_at,
            stream=stream,
            clock=self._clock,
        )

    async def create(self, spec: SandboxSpec) -> E2BSandbox:
        key = self._key()
        secret = key.get_secret_value()
        if any(secret in v for v in spec.env.values()):
            raise SandboxError("spec.env contains the E2B API key; refusing to create")
        sdk = self._sdk
        abandoned = threading.Event()
        started = self._clock()

        def build() -> _Created:
            kwargs: dict[str, Any] = {
                "resolution": spec.screen,
                "timeout": spec.timeout_minutes * 60,
                "envs": dict(spec.env),  # the SDK adds DISPLAY to the dict it is given
                "metadata": {
                    k: v
                    for k, v in (
                        ("agentplatform.session", spec.session_id),
                        ("agentplatform.agent", spec.agent_id),
                    )
                    if v
                },
                "api_key": secret,
            }
            if spec.network == "allowlist":
                kwargs["network"] = {"allow_out": list(spec.allowlist), "deny_out": [ALL_TRAFFIC]}
            sbx = sdk.create(**kwargs)
            try:
                if abandoned.is_set():
                    raise SandboxError("create: abandoned after timeout")
                sbx.stream.start(require_auth=True)
                auth = sbx.stream.get_auth_key()
                urls = StreamUrls(
                    view_only_url=sbx.stream.get_url(view_only=True, auth_key=auth),
                    interactive_url=sbx.stream.get_url(auth_key=auth),
                )
                sbx.screenshot()  # the desktop answers: ready for actions
            except BaseException:
                _quiet_kill(sbx)
                raise
            return _Created(sbx, urls)

        task: asyncio.Future[_Created] = asyncio.ensure_future(asyncio.to_thread(build))
        try:
            done, _ = await asyncio.wait({task}, timeout=self._create_timeout_s)
        finally:
            if not task.done():
                # A worker thread cannot be cancelled: make sure a sandbox it still
                # creates is killed instead of running (and billing) unowned.
                abandoned.set()
                task.add_done_callback(self._kill_late)
        if not done:
            raise SandboxError(f"create: e2b sandbox not ready after {self._create_timeout_s:g} s")
        try:
            created = task.result()
        except Exception as exc:
            raise _map_error(exc, "create") from exc
        handle = self._handle(
            created.sbx,
            key,
            spec.screen,
            started + timedelta(minutes=spec.timeout_minutes),
            created.urls,
        )
        log.info("sandbox_created", sandbox_id=handle.id, provider=PROVIDER_NAME)
        return handle

    def _kill_late(self, task: asyncio.Future[_Created]) -> None:
        if task.cancelled() or task.exception() is not None:
            return
        sbx = task.result().sbx
        fut = asyncio.ensure_future(asyncio.to_thread(_quiet_kill, sbx))
        self._cleanup.add(fut)
        fut.add_done_callback(self._cleanup.discard)
        log.warning("sandbox_create_abandoned", sandbox_id=str(sbx.sandbox_id))

    async def connect(self, sandbox_id: str) -> E2BSandbox:
        key = self._key()
        sdk = self._sdk

        def do() -> tuple[SdkSandbox, datetime, tuple[int, int]]:
            sbx = sdk.connect(sandbox_id, api_key=key.get_secret_value())
            end_at = _aware(sbx.get_info().end_at)
            w, h = sbx.get_screen_size()
            return sbx, end_at, (int(w), int(h))

        try:
            sbx, end_at, screen = await asyncio.to_thread(do)
        except Exception as exc:
            raise _map_error(exc, "connect") from exc
        log.info("sandbox_connected", sandbox_id=sandbox_id, provider=PROVIDER_NAME)
        return self._handle(sbx, key, screen, end_at, stream=None)

    async def health(self) -> HealthStatus:
        if self._api_key is None:
            return HealthStatus(ok=False, error="E2B_API_KEY is not set")
        secret = self._api_key.get_secret_value()
        start = time.monotonic()
        try:
            sdk = self._sdk

            def probe() -> None:
                sdk.list(limit=1, api_key=secret, request_timeout=HEALTH_TIMEOUT_S).next_items()

            await asyncio.wait_for(asyncio.to_thread(probe), HEALTH_TIMEOUT_S + 5)
        except Exception as exc:
            reason = "timed out" if isinstance(exc, TimeoutError) else _map_error(exc, "health")
            return HealthStatus(ok=False, error=str(reason))
        return HealthStatus(ok=True, latency_ms=int((time.monotonic() - start) * 1000))


def _quiet_kill(sbx: SdkSandbox) -> None:
    try:
        sbx.kill()
    except Exception:
        log.warning("sandbox_cleanup_failed", sandbox_id=str(getattr(sbx, "sandbox_id", "?")))


def make_e2b_contract_provider() -> E2BProvider:
    """Provider for the shared contract suite (live only: ``APP_LIVE=1`` + ``E2B_API_KEY``)."""
    return E2BProvider.from_settings(load_settings())
