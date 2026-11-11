"""FakeSandbox / FakeSandboxProvider: in-memory ``SandboxHandle`` for tests.

Behaviour:

* ``screenshot()`` returns the canned PNGs in order; after the last one it keeps
  returning the last (so "screen unchanged" is the default once the list ends).
  ``push_screens()`` appends more. Default: one ``make_png`` frame.
* Every action (click, move, drag, scroll, type_text, key, run_command, write_file,
  pause, resume, kill) is appended to ``actions`` as ``RecordedAction(name, args)``.
  Reads (screenshot, read_file, list_dir, stream_urls) are not recorded.
* Coordinates outside ``screen_size`` raise ``SandboxError`` like a real provider.
* Files live in an in-memory dict keyed by absolute path; directories are implied by
  file paths (plus ``/home/agent`` and ``/home/agent/Shared`` which always exist).
* ``run_command(cmd)``: if ``command_handler`` is set it decides; else an exact match
  in ``command_results``; else a few built-ins (``cat <abs path>``, ``echo <text>``,
  ``true``/``false``); else exit code 0 with empty output.
* After ``kill()`` every method raises ``SandboxExpiredError``. While paused, actions
  raise ``SandboxError``.
"""

from __future__ import annotations

import posixpath
import shlex
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentplatform.contracts.common import HealthStatus, SandboxProviderName, new_id, utcnow
from agentplatform.contracts.sandbox import (
    COMMAND_OUTPUT_LIMIT,
    CommandResult,
    FileInfo,
    SandboxSpec,
    StreamUrls,
)
from agentplatform.errors import SandboxError, SandboxExpiredError
from agentplatform.sandboxes.base import MouseButton, ScrollDirection
from agentplatform.testing.images import make_png

__all__ = ["FakeSandbox", "FakeSandboxProvider", "RecordedAction"]

_ALWAYS_DIRS = {"/", "/home", "/home/agent", "/home/agent/Shared", "/tmp"}  # noqa: S108


@dataclass(frozen=True, slots=True)
class RecordedAction:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


class FakeSandbox:
    """In-memory ``SandboxHandle``. See the module docstring."""

    def __init__(
        self,
        screens: Sequence[bytes] | None = None,
        *,
        screen_size: tuple[int, int] = (1280, 800),
        sandbox_id: str | None = None,
        provider: SandboxProviderName = "docker_local",
        command_results: dict[str, CommandResult] | None = None,
        command_handler: Callable[[str], CommandResult] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self._id = sandbox_id or f"fake-{new_id()}"
        self._provider: SandboxProviderName = provider
        self._screen_size = screen_size
        self._screens: list[bytes] = list(screens) if screens else [make_png(*screen_size)]
        self._screen_index = 0
        self.actions: list[RecordedAction] = []
        self.files: dict[str, bytes] = {}
        self.command_results = dict(command_results or {})
        self.command_handler = command_handler
        self.env = dict(env or {})
        self.paused = False
        self.killed = False
        self.screenshots_taken = 0

    # -- properties -------------------------------------------------------------------

    @property
    def id(self) -> str:
        return self._id

    @property
    def provider(self) -> SandboxProviderName:
        return self._provider

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._screen_size

    # -- helpers ----------------------------------------------------------------------

    def push_screens(self, *screens: bytes) -> None:
        self._screens.extend(screens)

    def action_names(self) -> list[str]:
        return [a.name for a in self.actions]

    def _alive(self) -> None:
        if self.killed:
            raise SandboxExpiredError(f"sandbox {self._id} was killed")

    def _act(self, name: str, **args: Any) -> None:
        self._alive()
        if self.paused:
            raise SandboxError(f"sandbox {self._id} is paused")
        self.actions.append(RecordedAction(name, args))

    def _check_xy(self, x: int, y: int) -> None:
        w, h = self._screen_size
        if not (0 <= x < w and 0 <= y < h):
            raise SandboxError(f"coordinates ({x}, {y}) are outside the {w}x{h} screen")

    @staticmethod
    def _check_path(path: str) -> str:
        if not path.startswith("/"):
            raise SandboxError(f"path must be absolute: {path!r}")
        return posixpath.normpath(path)

    def _is_dir(self, path: str) -> bool:
        prefix = path.rstrip("/") + "/"
        return path in _ALWAYS_DIRS or any(p.startswith(prefix) for p in self.files)

    # -- SandboxHandle ----------------------------------------------------------------

    async def screenshot(self) -> bytes:
        self._alive()
        self.screenshots_taken += 1
        png = self._screens[min(self._screen_index, len(self._screens) - 1)]
        self._screen_index += 1
        return png

    async def click(self, x: int, y: int, button: MouseButton = "left", count: int = 1) -> None:
        self._check_xy(x, y)
        self._act("click", x=x, y=y, button=button, count=count)

    async def move(self, x: int, y: int) -> None:
        self._check_xy(x, y)
        self._act("move", x=x, y=y)

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._check_xy(x1, y1)
        self._check_xy(x2, y2)
        self._act("drag", x1=x1, y1=y1, x2=x2, y2=y2)

    async def scroll(self, x: int, y: int, direction: ScrollDirection, amount: int) -> None:
        self._check_xy(x, y)
        self._act("scroll", x=x, y=y, direction=direction, amount=amount)

    async def type_text(self, text: str, interval_ms: int = 10) -> None:
        self._act("type_text", text=text)

    async def key(self, combo: str) -> None:
        self._act("key", combo=combo)

    async def run_command(self, cmd: str, timeout_s: int = 60) -> CommandResult:
        self._act("run_command", cmd=cmd, timeout_s=timeout_s)
        if self.command_handler is not None:
            return self.command_handler(cmd)
        if cmd in self.command_results:
            return self.command_results[cmd]
        return self._builtin(cmd)

    def _builtin(self, cmd: str) -> CommandResult:
        try:
            argv = shlex.split(cmd)
        except ValueError:
            return CommandResult(exit_code=2, stderr="syntax error")
        if argv == ["true"] or not argv:
            return CommandResult(exit_code=0)
        if argv == ["false"]:
            return CommandResult(exit_code=1)
        if argv[0] == "echo":
            return _limited(" ".join(argv[1:]) + "\n", "", 0)
        if argv[0] == "cat" and len(argv) == 2 and argv[1].startswith("/"):
            data = self.files.get(posixpath.normpath(argv[1]))
            if data is None:
                return CommandResult(exit_code=1, stderr=f"cat: {argv[1]}: No such file")
            return _limited(data.decode("utf-8", "replace"), "", 0)
        return CommandResult(exit_code=0)

    async def write_file(self, path: str, data: bytes) -> None:
        norm = self._check_path(path)
        self._act("write_file", path=norm, size=len(data))
        self.files[norm] = bytes(data)

    async def read_file(self, path: str, max_bytes: int = 50_000_000) -> bytes:
        self._alive()
        norm = self._check_path(path)
        if norm not in self.files:
            raise SandboxError(f"no such file: {norm}")
        data = self.files[norm]
        if len(data) > max_bytes:
            raise SandboxError(f"{norm} is larger than {max_bytes} bytes")
        return data

    async def list_dir(self, path: str) -> list[FileInfo]:
        self._alive()
        norm = self._check_path(path)
        if not self._is_dir(norm):
            raise SandboxError(f"not a directory: {norm}")
        prefix = norm.rstrip("/") + "/"
        children: dict[str, FileInfo] = {}
        for file_path, data in self.files.items():
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix) :]
            name = rest.split("/", 1)[0]
            is_dir = "/" in rest
            children.setdefault(
                name,
                FileInfo(
                    path=prefix + name,
                    name=name,
                    is_dir=is_dir,
                    size_bytes=0 if is_dir else len(data),
                    modified_at=utcnow(),
                ),
            )
        return [children[k] for k in sorted(children)]

    async def stream_urls(self) -> StreamUrls:
        self._alive()
        base = f"http://127.0.0.1:6080/{self._id}/vnc.html?autoconnect=1&resize=scale"
        return StreamUrls(view_only_url=base + "&view_only=1", interactive_url=base)

    async def pause(self) -> None:
        self._alive()
        self.actions.append(RecordedAction("pause"))
        self.paused = True

    async def resume(self) -> None:
        self._alive()
        self.actions.append(RecordedAction("resume"))
        self.paused = False

    async def kill(self) -> None:
        if not self.killed:
            self.actions.append(RecordedAction("kill"))
        self.killed = True


def _limited(stdout: str, stderr: str, code: int) -> CommandResult:
    truncated = len(stdout) > COMMAND_OUTPUT_LIMIT or len(stderr) > COMMAND_OUTPUT_LIMIT
    return CommandResult(
        stdout=stdout[:COMMAND_OUTPUT_LIMIT],
        stderr=stderr[:COMMAND_OUTPUT_LIMIT],
        exit_code=code,
        truncated=truncated,
    )


class FakeSandboxProvider:
    """``SandboxProvider`` that creates ``FakeSandbox``es.

    ``factory(spec)`` builds each sandbox (default: ``FakeSandbox(screen_size=spec.screen,
    env=spec.env)``). Created sandboxes are kept in ``sandboxes`` by id so
    ``connect`` returns the same object; ``fail_creates`` makes the next N creates
    raise ``SandboxError``.
    """

    def __init__(
        self,
        factory: Callable[[SandboxSpec], FakeSandbox] | None = None,
        *,
        name: SandboxProviderName = "docker_local",
    ) -> None:
        self._factory = factory
        self._name: SandboxProviderName = name
        self.sandboxes: dict[str, FakeSandbox] = {}
        self.specs: list[SandboxSpec] = []
        self.spec_of: dict[str, SandboxSpec] = {}
        """The spec each sandbox was created from (by sandbox id)."""
        self.fail_creates = 0

    @property
    def name(self) -> SandboxProviderName:
        return self._name

    async def create(self, spec: SandboxSpec) -> FakeSandbox:
        self.specs.append(spec)
        if self.fail_creates > 0:
            self.fail_creates -= 1
            raise SandboxError("fake create failure")
        if self._factory is not None:
            sandbox = self._factory(spec)
        else:
            sandbox = FakeSandbox(screen_size=spec.screen, env=spec.env, provider=self._name)
        self.sandboxes[sandbox.id] = sandbox
        self.spec_of[sandbox.id] = spec
        return sandbox

    async def remove_agent_sandboxes(
        self, session_id: str, agent_id: str, *, keep: Collection[str] = ()
    ) -> list[str]:
        """``AgentSandboxSweeper``: kill the agent's live sandboxes not in ``keep``."""
        removed: list[str] = []
        for sid, sandbox in self.sandboxes.items():
            spec = self.spec_of.get(sid)
            if spec is None or (spec.session_id, spec.agent_id) != (session_id, agent_id):
                continue
            if sid in keep or sandbox.killed:
                continue
            await sandbox.kill()
            removed.append(sid)
        return removed

    async def connect(self, sandbox_id: str) -> FakeSandbox:
        sandbox = self.sandboxes.get(sandbox_id)
        if sandbox is None or sandbox.killed:
            raise SandboxExpiredError(f"sandbox {sandbox_id} does not exist")
        return sandbox

    async def health(self) -> HealthStatus:
        return HealthStatus(ok=True, latency_ms=0)
