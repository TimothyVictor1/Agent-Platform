"""The provider-agnostic sandbox interface (SPEC §9.1).

Implementations: ``docker_local.py`` (T07) and ``e2b_provider.py`` (T08); the test
double is ``agentplatform.testing.fake_sandbox.FakeSandbox``. Every implementation
must pass ``tests/integration/test_sandbox_contract.py``.

Coordinates passed to a handle are always **sandbox pixels** (0 ≤ x < width,
0 ≤ y < height of ``screen_size``). Converting model coordinates is the action
layer's job (``runtime/agent/actions.py``), not the provider's.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Literal, Protocol, runtime_checkable

from agentplatform.contracts.common import HealthStatus, SandboxProviderName
from agentplatform.contracts.sandbox import CommandResult, FileInfo, SandboxSpec, StreamUrls

__all__ = [
    "AgentSandboxSweeper",
    "MouseButton",
    "SandboxHandle",
    "SandboxProvider",
    "ScrollDirection",
]

MouseButton = Literal["left", "right", "middle"]
ScrollDirection = Literal["up", "down", "left", "right"]


@runtime_checkable
class SandboxHandle(Protocol):
    """A live connection to one sandbox desktop.

    General rules for every method:

    * Methods are coroutines and must not block the event loop (wrap sync SDKs in
      ``asyncio.to_thread``).
    * Failures raise ``agentplatform.errors.SandboxError`` (or
      ``SandboxExpiredError`` once the sandbox is gone/expired). Invalid arguments
      (coordinates outside the screen, relative paths) raise ``SandboxError`` with a
      clear message; providers never silently clamp.
    * The handle never receives or stores secrets other than its own per-sandbox
      control token.
    * Calls on one handle may be made concurrently (e.g. a screenshot for the live
      tile while an action runs); implementations serialize internally if needed.
    """

    @property
    def id(self) -> str:
        """Provider sandbox id; ``SandboxProvider.connect(id)`` reattaches to it."""
        ...

    @property
    def provider(self) -> SandboxProviderName:
        """Name of the provider that owns this sandbox."""
        ...

    @property
    def screen_size(self) -> tuple[int, int]:
        """Screen size in pixels ``(width, height)``, e.g. ``(1280, 800)``."""
        ...

    async def screenshot(self) -> bytes:
        """Full-screen PNG at ``screen_size`` resolution."""
        ...

    async def click(self, x: int, y: int, button: MouseButton = "left", count: int = 1) -> None:
        """Move to ``(x, y)`` and click ``count`` times (1..3) with ``button``."""
        ...

    async def move(self, x: int, y: int) -> None:
        """Move the pointer to ``(x, y)`` without clicking."""
        ...

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        """Press the left button at ``(x1, y1)``, move to ``(x2, y2)``, release."""
        ...

    async def scroll(self, x: int, y: int, direction: ScrollDirection, amount: int) -> None:
        """Move to ``(x, y)`` and scroll ``amount`` wheel clicks (1..50) in ``direction``."""
        ...

    async def type_text(self, text: str, interval_ms: int = 10) -> None:
        """Type ``text`` as keystrokes into the focused window.

        ``text`` contains no newlines (the tool layer converts ``\\n`` into ``key("enter")``
        calls). Unicode must be supported. Max length 2000 characters.
        """
        ...

    async def key(self, combo: str) -> None:
        """Press a normalized key combo: lower-case key names joined by ``+``, e.g.
        ``"ctrl+l"``, ``"enter"``, ``"alt+tab"``, ``"ctrl+shift+t"``, ``"super"``.

        Names follow ``actions.normalize_key`` (xdotool-compatible after mapping
        ``page_up``/``page_down``). Unknown keys raise ``SandboxError``.
        """
        ...

    async def run_command(self, cmd: str, timeout_s: int = 60) -> CommandResult:
        """Run ``cmd`` with ``bash -lc`` as the unprivileged sandbox user.

        Never raises for a non-zero exit code: return it in ``exit_code``. On
        timeout kill the process group and return ``timed_out=True`` and
        ``exit_code=124``. stdout/stderr are truncated to 8 KB each
        (``truncated=True`` if either was cut).
        """
        ...

    async def write_file(self, path: str, data: bytes) -> None:
        """Write ``data`` to absolute ``path``, creating parent directories, owned by
        the sandbox user. Max 50 MB."""
        ...

    async def read_file(self, path: str, max_bytes: int = 50_000_000) -> bytes:
        """Read absolute ``path``. Raise ``SandboxError`` if missing, a directory, or
        larger than ``max_bytes``."""
        ...

    async def list_dir(self, path: str) -> list[FileInfo]:
        """List the direct children of absolute directory ``path`` (no recursion),
        sorted by name. Raise ``SandboxError`` if it is not a directory."""
        ...

    async def stream_urls(self) -> StreamUrls:
        """noVNC URLs: view-only for watching, interactive for user takeover. URLs may
        embed a per-sandbox access token but never platform secrets."""
        ...

    async def pause(self) -> None:
        """Suspend the sandbox (keeps state). Raise ``NotSupportedError`` if the
        provider cannot pause."""
        ...

    async def resume(self) -> None:
        """Resume a paused sandbox. No-op if it is running."""
        ...

    async def kill(self) -> None:
        """Destroy the sandbox and free resources. Idempotent; after this every other
        method raises ``SandboxExpiredError``."""
        ...


@runtime_checkable
class SandboxProvider(Protocol):
    """Creates and reconnects sandboxes of one kind."""

    @property
    def name(self) -> SandboxProviderName: ...

    async def create(self, spec: SandboxSpec) -> SandboxHandle:
        """Create a sandbox and wait until its desktop is ready to take actions
        (screenshot works). Raise ``SandboxError`` on failure or timeout (60 s
        default), cleaning up anything partially created.

        ``spec.env`` is passed into the sandbox; the caller guarantees it contains no
        secrets and providers must not add any platform secret to it.
        """
        ...

    async def connect(self, sandbox_id: str) -> SandboxHandle:
        """Reattach to an existing sandbox after a runtime restart. Raise
        ``SandboxExpiredError`` if it no longer exists."""
        ...

    async def health(self) -> HealthStatus:
        """Is the provider usable (Docker daemon reachable / API key valid)? Must not
        raise."""
        ...


@runtime_checkable
class AgentSandboxSweeper(Protocol):
    """Optional provider capability: find and remove one agent's sandboxes.

    A runtime that dies while ``create`` runs leaves a sandbox that no event names.
    Before an adopted agent gets a new sandbox, the pool removes the agent's other
    sandboxes with this, so a crash during boot does not leak a running desktop
    until its lifetime ends (T21)."""

    async def remove_agent_sandboxes(
        self, session_id: str, agent_id: str, *, keep: Collection[str] = ()
    ) -> list[str]:
        """Remove every sandbox labelled with ``session_id`` and ``agent_id`` whose id
        is not in ``keep``; return the removed ids. Never touches other sessions,
        agents or worktrees. Raises ``SandboxError`` if the provider cannot list."""
        ...
