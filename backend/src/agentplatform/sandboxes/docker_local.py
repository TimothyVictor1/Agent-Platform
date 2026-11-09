"""``docker_local`` sandbox provider (SPEC §9.2, task T07).

Runs one ``agentplatform/desktop`` container (built from ``sandbox-image/``) per
sandbox and drives it through the in-container control server (port 7000) over
HTTP. The noVNC viewer (port 6080) gives the stream URLs.

Security model (see ``docs/sandboxes.md``):

* Every container is launched with exactly ``--cap-drop ALL --cap-add SETUID
  --cap-add SETGID --security-opt no-new-privileges`` (the minimal set T01
  verified: root in the entrypoint only drops privileges to ``agent``), plus
  memory/CPU/PID limits, no swap, no volumes, never privileged, on its own bridge
  network ``agentplatform-sandboxes`` with inter-container traffic disabled.
* Ports 7000 and 6080 are published on ``127.0.0.1`` only, on random host ports.
* The per-container control token is generated here (``secrets.token_urlsafe``)
  and passed as the ``CONTROL_TOKEN`` env var as ``sandbox-image/README.md``
  describes; the entrypoint moves it into a file and unsets it. It is never put
  into the image or a label: ``connect()`` gets it back from the
  ``ControlTokenStore`` passed to the provider.
* Nothing from inside the container is ever executed on the host; responses from
  the control server are untrusted data (error details are cleaned and capped).

All Docker SDK calls are blocking and run in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import json as jsonlib
import posixpath
import re
import secrets
import time
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, TypeVar, cast

import docker
import docker.errors
import httpx
import structlog
from docker.models.containers import Container

from agentplatform.config import Settings, find_repo_root
from agentplatform.contracts.common import HealthStatus, SandboxProviderName, utcnow
from agentplatform.contracts.sandbox import (
    COMMAND_OUTPUT_LIMIT,
    CommandResult,
    FileInfo,
    SandboxSpec,
    StreamUrls,
)
from agentplatform.errors import NotSupportedError, SandboxError, SandboxExpiredError
from agentplatform.sandboxes.base import MouseButton, ScrollDirection

__all__ = [
    "CONTROL_PORT",
    "DEFAULT_IMAGE",
    "LABEL_AGENT",
    "LABEL_CREATED_AT",
    "LABEL_EXPIRES_AT",
    "LABEL_MANAGED",
    "LABEL_PROVIDER",
    "LABEL_SESSION",
    "LABEL_WORKTREE",
    "NETWORK_NAME",
    "NOVNC_PORT",
    "RESERVED_ENV",
    "ControlTokenStore",
    "DockerLocalHandle",
    "DockerLocalProvider",
    "InMemoryControlTokenStore",
    "worktree_label",
]

log = structlog.get_logger(__name__)

PROVIDER_NAME: SandboxProviderName = "docker_local"
DEFAULT_IMAGE = "agentplatform/desktop:latest"
NETWORK_NAME = "agentplatform-sandboxes"
CONTROL_PORT = 7000
NOVNC_PORT = 6080

# Hardening: exactly the set T01 verified as the minimum (sandbox-image/README.md).
CAP_DROP = ["ALL"]
CAP_ADD = ["SETUID", "SETGID"]
SECURITY_OPT = ["no-new-privileges"]
MEM_LIMIT = "4g"
MEMSWAP_LIMIT = "4g"  # equal to MEM_LIMIT: no swap
NANO_CPUS = 2_000_000_000  # 2 vCPU
PIDS_LIMIT = 4096  # counts threads; Firefox + LibreOffice stay far below this

LABEL_MANAGED = "agentplatform.managed"
LABEL_PROVIDER = "agentplatform.provider"
LABEL_SESSION = "agentplatform.session"
LABEL_AGENT = "agentplatform.agent"
LABEL_WORKTREE = "agentplatform.worktree"
LABEL_CREATED_AT = "agentplatform.created_at"
LABEL_EXPIRES_AT = "agentplatform.expires_at"

# Env names the image/entrypoint/control server read; ``spec.env`` must not set them.
RESERVED_ENV = frozenset(
    {
        "CONTROL_TOKEN",
        "CONTROL_TOKEN_FILE",
        "SCREEN_WIDTH",
        "SCREEN_HEIGHT",
        "DISPLAY",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "SANDBOX_FILE_ROOT",
        "HOME",
        "USER",
        "PATH",
    }
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Client-side bounds (SandboxHandle contract; the control server has looser ones).
MAX_TYPE_CHARS = 2000
MAX_INTERVAL_MS = 250
MAX_COMBO_CHARS = 64
MAX_COMMAND_TIMEOUT_S = 600
MAX_FILE_BYTES = 50_000_000
SERVER_OUTPUT_BYTES = 8_192  # control server truncation of stdout/stderr
MIN_SCREEN = 320
MAX_SCREEN = 3840

_JSON_BODY_CAP = 1_000_000
_SCREENSHOT_CAP = 64_000_000
_ERROR_DETAIL_CHARS = 500
_ACTION_TIMEOUT_S = 15.0
_DEFAULT_TIMEOUT_S = 30.0
_FILE_TIMEOUT_S = 120.0
_COMMAND_GRACE_S = 15.0  # server kill grace + drain + collect, plus HTTP slack
_RETRIES = 3
_RETRY_BACKOFF_S = 0.2

_T = TypeVar("_T")


# --- token store --------------------------------------------------------------------


class ControlTokenStore(Protocol):
    """Where the provider keeps each sandbox's control token so that ``connect()``
    can reattach after a runtime restart. Never container labels: labels are
    readable by anything that can list containers and end up in debug output.

    The runtime wires a durable implementation; ``InMemoryControlTokenStore`` is
    the default and only survives as long as the process.
    """

    async def put(self, sandbox_id: str, token: str) -> None: ...

    async def get(self, sandbox_id: str) -> str | None: ...

    async def delete(self, sandbox_id: str) -> None: ...


class InMemoryControlTokenStore:
    """Process-local ``ControlTokenStore`` (tests, single-process dev runs)."""

    def __init__(self) -> None:
        self._tokens: dict[str, str] = {}

    async def put(self, sandbox_id: str, token: str) -> None:
        self._tokens[sandbox_id] = token

    async def get(self, sandbox_id: str) -> str | None:
        return self._tokens.get(sandbox_id)

    async def delete(self, sandbox_id: str) -> None:
        self._tokens.pop(sandbox_id, None)

    def __len__(self) -> int:
        return len(self._tokens)

    def __contains__(self, sandbox_id: object) -> bool:
        return sandbox_id in self._tokens


# --- helpers ------------------------------------------------------------------------


def worktree_label(app_worktree: str = "") -> str:
    """The ``agentplatform.worktree`` label value.

    ``APP_WORKTREE`` when set, else the repo folder name; sanitized exactly like
    ``scripts/superset-teardown.sh`` (``tr -c 'a-zA-Z0-9_' '_'``) so its
    ``docker ps --filter label=agentplatform.worktree=...`` finds our containers.
    """
    name = app_worktree.strip() or find_repo_root().name
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _docker_error(exc: Exception, what: str) -> SandboxError:
    if isinstance(exc, docker.errors.NotFound):
        return SandboxExpiredError(f"{what}: sandbox container no longer exists")
    if isinstance(exc, docker.errors.APIError):
        explanation = exc.explanation if isinstance(exc.explanation, str) else str(exc)
        return SandboxError(f"{what}: docker error: {_clean(explanation)}")
    return SandboxError(f"{what}: Docker daemon is not reachable ({type(exc).__name__})")


def _clean(text: str, limit: int = _ERROR_DETAIL_CHARS) -> str:
    """Make untrusted text safe for an error message: no control chars, capped."""
    text = "".join(ch if ch.isprintable() else " " for ch in text).strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _host_port(ports: Mapping[str, Any], container_port: int) -> int | None:
    """Host port bound on 127.0.0.1 for ``container_port``; ignores other bindings."""
    bindings = ports.get(f"{container_port}/tcp") or []
    for binding in bindings:
        if binding.get("HostIp") == "127.0.0.1" and str(binding.get("HostPort", "")).isdigit():
            return int(binding["HostPort"])
    return None


def _check_abs_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/"):
        raise SandboxError(f"path must be absolute: {path!r}")
    if "\x00" in path:
        raise SandboxError("path must not contain null bytes")
    return posixpath.normpath(path)


@dataclass(frozen=True, slots=True)
class _Endpoints:
    control_port: int
    novnc_port: int


# --- provider -----------------------------------------------------------------------


class DockerLocalProvider:
    """``SandboxProvider`` backed by the local Docker daemon."""

    def __init__(
        self,
        token_store: ControlTokenStore | None = None,
        *,
        image: str = DEFAULT_IMAGE,
        worktree: str | None = None,
        network: str = NETWORK_NAME,
        ready_timeout_s: float = 60.0,
        docker_client: docker.DockerClient | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
        poll_interval_s: float = 0.5,
    ) -> None:
        self._tokens: ControlTokenStore = token_store or InMemoryControlTokenStore()
        self._image = image
        self._worktree = worktree_label(worktree or "")
        self._network = network
        self._ready_timeout_s = ready_timeout_s
        self._docker = docker_client
        self._docker_lock = asyncio.Lock()
        self._network_ready = False
        self._network_lock = asyncio.Lock()
        self._transport = http_transport
        self._poll_interval_s = poll_interval_s

    @classmethod
    def from_settings(
        cls, settings: Settings, token_store: ControlTokenStore | None = None, **kwargs: Any
    ) -> DockerLocalProvider:
        return cls(token_store, worktree=settings.app_worktree, **kwargs)

    @property
    def name(self) -> SandboxProviderName:
        return PROVIDER_NAME

    @property
    def worktree(self) -> str:
        return self._worktree

    @property
    def image(self) -> str:
        return self._image

    # -- docker plumbing --------------------------------------------------------------

    async def _client(self) -> docker.DockerClient:
        if self._docker is None:
            async with self._docker_lock:
                if self._docker is None:
                    try:
                        self._docker = await asyncio.to_thread(docker.from_env)
                    except docker.errors.DockerException as exc:
                        raise _docker_error(exc, "connect to Docker") from exc
        return self._docker

    async def _call(self, what: str, fn: Callable[..., _T], *args: Any, **kwargs: Any) -> _T:
        """Run a blocking Docker SDK call in a thread; map its errors."""
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except docker.errors.DockerException as exc:
            raise _docker_error(exc, what) from exc
        except OSError as exc:  # socket errors raised below the SDK
            raise _docker_error(exc, what) from exc

    async def _get_container(self, sandbox_id: str) -> Container:
        client = await self._client()
        container = await self._call("look up sandbox", client.containers.get, sandbox_id)
        if container.labels.get(LABEL_PROVIDER) != PROVIDER_NAME:
            raise SandboxExpiredError(f"{sandbox_id} is not a docker_local sandbox")
        return container

    async def _ensure_network(self) -> None:
        async with self._network_lock:
            if self._network_ready:
                return
            client = await self._client()
            existing = await self._call(
                "list networks", client.networks.list, names=[self._network]
            )
            if not any(net.name == self._network for net in existing):
                try:
                    await asyncio.to_thread(
                        client.networks.create,
                        self._network,
                        driver="bridge",
                        # Sandboxes must not reach each other's control/noVNC ports.
                        options={"com.docker.network.bridge.enable_icc": "false"},
                        labels={LABEL_MANAGED: "1"},
                    )
                except docker.errors.APIError as exc:
                    # Another process created it at the same time: fine.
                    if exc.status_code != 409:
                        raise _docker_error(exc, "create sandbox network") from exc
                except docker.errors.DockerException as exc:
                    raise _docker_error(exc, "create sandbox network") from exc
            self._network_ready = True

    # -- SandboxProvider --------------------------------------------------------------

    async def create(self, spec: SandboxSpec) -> DockerLocalHandle:
        width, height = self._validate_spec(spec)
        await self._ensure_network()
        client = await self._client()
        token = secrets.token_urlsafe(32)
        created = utcnow()
        labels = {
            LABEL_MANAGED: "1",
            LABEL_PROVIDER: PROVIDER_NAME,
            LABEL_WORKTREE: self._worktree,
            LABEL_SESSION: spec.session_id or "",
            LABEL_AGENT: spec.agent_id or "",
            LABEL_CREATED_AT: created.isoformat(),
            LABEL_EXPIRES_AT: (created + timedelta(minutes=spec.timeout_minutes)).isoformat(),
        }
        environment = {
            **spec.env,
            "CONTROL_TOKEN": token,
            "SCREEN_WIDTH": str(width),
            "SCREEN_HEIGHT": str(height),
        }
        # ("127.0.0.1", None): bind on loopback only, Docker picks a free host port.
        ports = cast(
            "dict[str, tuple[str, int]]",
            {f"{CONTROL_PORT}/tcp": ("127.0.0.1", None), f"{NOVNC_PORT}/tcp": ("127.0.0.1", None)},
        )
        container = await self._call(
            "create sandbox",
            client.containers.create,
            self._image,
            detach=True,
            environment=environment,
            labels=labels,
            ports=ports,
            network=self._network,
            cap_drop=list(CAP_DROP),
            cap_add=list(CAP_ADD),
            security_opt=list(SECURITY_OPT),
            mem_limit=MEM_LIMIT,
            memswap_limit=MEMSWAP_LIMIT,
            nano_cpus=NANO_CPUS,
            pids_limit=PIDS_LIMIT,
            privileged=False,
        )
        sandbox_id = container.id
        if sandbox_id is None:  # pragma: no cover - the SDK always sets it
            raise SandboxError("docker returned a container without an id")
        bound = log.bind(
            sandbox_id=sandbox_id[:12], session_id=spec.session_id, agent_id=spec.agent_id
        )
        handle: DockerLocalHandle | None = None
        try:
            await self._tokens.put(sandbox_id, token)
            await self._call("start sandbox", container.start)
            endpoints = await self._wait_for_ports(container)
            handle = DockerLocalHandle(
                provider=self,
                container=container,
                endpoints=endpoints,
                token=token,
                screen_size=(width, height),
            )
            await handle.wait_ready(self._ready_timeout_s, self._poll_interval_s)
        except BaseException as exc:
            bound.warning("sandbox_create_failed", error=type(exc).__name__)
            await self._discard(container, sandbox_id, handle)
            if isinstance(exc, SandboxError) or not isinstance(exc, Exception):
                raise
            raise SandboxError(f"sandbox create failed: {type(exc).__name__}") from exc
        bound.info("sandbox_created", control_port=endpoints.control_port)
        return handle

    def _validate_spec(self, spec: SandboxSpec) -> tuple[int, int]:
        if spec.network != "open":
            raise NotSupportedError(
                "docker_local cannot enforce a network allowlist; use network='open' "
                "or a provider that supports egress filtering"
            )
        width, height = spec.screen
        if not (MIN_SCREEN <= width <= MAX_SCREEN and MIN_SCREEN <= height <= MAX_SCREEN):
            raise SandboxError(f"screen size {width}x{height} is out of range")
        for key, value in spec.env.items():
            if not _ENV_NAME.match(key):
                raise SandboxError(f"invalid env var name: {key!r}")
            if key.upper() in RESERVED_ENV:
                raise SandboxError(f"env var {key} is reserved by the sandbox image")
            if "\x00" in value:
                raise SandboxError(f"env var {key} contains a null byte")
        return width, height

    async def _wait_for_ports(self, container: Container) -> _Endpoints:
        """Docker fills in the published ports shortly after start."""
        deadline = time.monotonic() + 10.0
        while True:
            await self._call("inspect sandbox", container.reload)
            ports = container.ports
            control, novnc = _host_port(ports, CONTROL_PORT), _host_port(ports, NOVNC_PORT)
            if control is not None and novnc is not None:
                return _Endpoints(control_port=control, novnc_port=novnc)
            if container.status not in ("created", "running"):
                raise SandboxError(f"sandbox container stopped during start ({container.status})")
            if time.monotonic() >= deadline:
                raise SandboxError("sandbox ports were not published on 127.0.0.1")
            await asyncio.sleep(0.1)

    async def _discard(
        self, container: Container, sandbox_id: str, handle: DockerLocalHandle | None
    ) -> None:
        """Best-effort cleanup of a partially created sandbox. Never raises."""
        if handle is not None:
            await handle.aclose()
        try:
            await asyncio.to_thread(container.remove, force=True)
        except Exception as exc:
            log.warning("sandbox_cleanup_failed", sandbox_id=sandbox_id[:12], error=str(exc)[:200])
        try:
            await self._tokens.delete(sandbox_id)
        except Exception as exc:
            log.warning("sandbox_token_cleanup_failed", error=type(exc).__name__)

    async def connect(self, sandbox_id: str) -> DockerLocalHandle:
        if not sandbox_id or not re.fullmatch(r"[0-9a-f]{12,64}", sandbox_id):
            raise SandboxExpiredError(f"invalid docker_local sandbox id: {sandbox_id!r}")
        container = await self._get_container(sandbox_id)
        full_id = container.id or sandbox_id
        if container.status not in ("running", "paused"):
            raise SandboxExpiredError(f"sandbox {full_id[:12]} is {container.status}")
        token = await self._tokens.get(full_id)
        if not token:
            raise SandboxExpiredError(
                f"no control token stored for sandbox {full_id[:12]}; it cannot be controlled"
            )
        paused = container.status == "paused"
        control, novnc = (
            _host_port(container.ports, CONTROL_PORT),
            _host_port(container.ports, NOVNC_PORT),
        )
        endpoints: _Endpoints | None = None
        if control is not None and novnc is not None:
            endpoints = _Endpoints(control_port=control, novnc_port=novnc)
        elif not paused:
            raise SandboxError(f"sandbox {full_id[:12]} has no ports published on 127.0.0.1")
        # Docker reports no published ports while a container is paused: the handle
        # learns them on resume().
        handle = DockerLocalHandle(
            provider=self,
            container=container,
            endpoints=endpoints,
            token=token,
            screen_size=(0, 0),
            paused=paused,
        )
        if not paused:
            try:
                await handle.refresh_screen_size()
            except BaseException:
                await handle.aclose()
                raise
        else:
            handle.set_screen_size_from_env(container.attrs)
        log.info("sandbox_connected", sandbox_id=full_id[:12], paused=handle.paused)
        return handle

    async def health(self) -> HealthStatus:
        started = time.monotonic()
        try:
            client = await self._client()
            await asyncio.to_thread(client.ping)
        except Exception:
            return HealthStatus(ok=False, error="Docker daemon is not reachable")
        latency = int((time.monotonic() - started) * 1000)
        try:
            await asyncio.to_thread(client.images.get, self._image)
        except docker.errors.ImageNotFound:
            return HealthStatus(
                ok=False,
                latency_ms=latency,
                error=f"image {self._image} not found; run `make sandbox-image`",
            )
        except Exception:
            return HealthStatus(ok=False, latency_ms=latency, error="could not inspect image")
        return HealthStatus(ok=True, latency_ms=latency)

    # -- maintenance ------------------------------------------------------------------

    async def cleanup_orphans(
        self, session_ids_alive: Collection[str], *, now: datetime | None = None
    ) -> list[str]:
        """Remove this worktree's sandbox containers whose session is not alive (or
        that have no session label), or whose ``expires_at`` has passed. Containers
        of other worktrees are never touched. Returns the removed container ids."""
        client = await self._client()
        now = now or utcnow()
        alive = set(session_ids_alive)
        containers = await self._call(
            "list sandboxes",
            client.containers.list,
            all=True,
            filters={
                "label": [
                    f"{LABEL_PROVIDER}={PROVIDER_NAME}",
                    f"{LABEL_WORKTREE}={self._worktree}",
                ]
            },
        )
        removed: list[str] = []
        for container in containers:
            labels = container.labels
            # Defence in depth: the daemon filtered already, re-check before deleting.
            if (
                labels.get(LABEL_PROVIDER) != PROVIDER_NAME
                or labels.get(LABEL_WORKTREE) != self._worktree
            ):
                continue
            session = labels.get(LABEL_SESSION, "")
            expires = _parse_time(labels.get(LABEL_EXPIRES_AT))
            orphan = not session or session not in alive
            expired = expires is not None and expires <= now
            if not (orphan or expired) or container.id is None:
                continue
            try:
                await asyncio.to_thread(container.remove, force=True)
            except docker.errors.NotFound:
                pass
            except docker.errors.DockerException as exc:
                log.warning(
                    "sandbox_orphan_remove_failed",
                    sandbox_id=container.id[:12],
                    error=str(exc)[:200],
                )
                continue
            await self._tokens.delete(container.id)
            removed.append(container.id)
            log.info(
                "sandbox_orphan_removed",
                sandbox_id=container.id[:12],
                session_id=session or None,
                expired=expired,
            )
        return removed

    def _http_client(self, endpoints: _Endpoints, token: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{endpoints.control_port}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT_S, connect=5.0),
            transport=self._transport,
            # Never route the bearer token through an HTTP(S)_PROXY from the env.
            trust_env=False,
            follow_redirects=False,
        )


# --- handle -------------------------------------------------------------------------


Method = Literal["GET", "POST"]


class DockerLocalHandle:
    """``SandboxHandle`` for one ``docker_local`` container."""

    def __init__(
        self,
        *,
        provider: DockerLocalProvider,
        container: Container,
        endpoints: _Endpoints | None,
        token: str,
        screen_size: tuple[int, int],
        paused: bool = False,
    ) -> None:
        if container.id is None:  # pragma: no cover - the SDK always sets it
            raise SandboxError("container has no id")
        self._provider = provider
        self._container = container
        self._id: str = container.id
        self._endpoints = endpoints
        self._token = token
        self._screen_size = screen_size
        self._http: httpx.AsyncClient | None = (
            provider._http_client(endpoints, token) if endpoints is not None else None
        )
        self._paused = paused
        self._killed = False
        self._state_lock = asyncio.Lock()

    def __repr__(self) -> str:  # never show the token
        port = self._endpoints.control_port if self._endpoints else None
        return f"DockerLocalHandle(id={self._id[:12]!r}, port={port})"

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
    def paused(self) -> bool:
        return self._paused

    @property
    def killed(self) -> bool:
        return self._killed

    # -- lifecycle helpers used by the provider ---------------------------------------

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise SandboxError(f"sandbox {self._id[:12]} has no known ports; resume it first")
        return self._http

    async def wait_ready(self, timeout_s: float, poll_interval_s: float = 0.5) -> None:
        """Wait until ``/health`` answers and a screenshot works (desktop is up)."""
        deadline = time.monotonic() + timeout_s
        last_error = "no response"
        while True:
            try:
                resp = await self._client().get("/health", timeout=5.0)
                if resp.status_code == 200:
                    shot = await self._client().get("/screenshot", timeout=10.0)
                    if shot.status_code == 200 and shot.content.startswith(b"\x89PNG"):
                        return
                    last_error = f"screenshot returned HTTP {shot.status_code}"
                elif resp.status_code in (401, 403):
                    raise SandboxError("sandbox rejected its control token during startup")
                else:
                    last_error = f"health returned HTTP {resp.status_code}"
            except httpx.HTTPError as exc:
                last_error = type(exc).__name__
            await self._provider._call("inspect sandbox", self._container.reload)
            if self._container.status not in ("created", "running"):
                raise SandboxError(
                    f"sandbox container exited during startup ({self._container.status})"
                )
            if time.monotonic() >= deadline:
                raise SandboxError(
                    f"sandbox was not ready within {timeout_s:.0f} s (last: {last_error})"
                )
            await asyncio.sleep(poll_interval_s)

    async def refresh_screen_size(self) -> None:
        data = await self._json("GET", "/screen", idempotent=True)
        width, height = data.get("width"), data.get("height")
        if not isinstance(width, int) or not isinstance(height, int):
            raise SandboxError("sandbox returned an invalid screen size")
        self._screen_size = (width, height)

    def set_screen_size_from_env(self, attrs: Mapping[str, Any]) -> None:
        env = dict(
            item.split("=", 1)
            for item in (attrs.get("Config", {}).get("Env") or [])
            if isinstance(item, str) and "=" in item
        )
        try:
            self._screen_size = (int(env["SCREEN_WIDTH"]), int(env["SCREEN_HEIGHT"]))
        except (KeyError, ValueError):
            self._screen_size = (1280, 800)

    # -- request plumbing -------------------------------------------------------------

    def _check_usable(self) -> None:
        if self._killed:
            raise SandboxExpiredError(f"sandbox {self._id[:12]} was killed")
        if self._paused:
            raise SandboxError(f"sandbox {self._id[:12]} is paused")

    async def _request(
        self,
        method: Method,
        path: str,
        *,
        idempotent: bool,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        max_bytes: int = _JSON_BODY_CAP,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> bytes:
        """One control-server call with retries; returns the (capped) body.

        Retries: a ``ConnectError``/``ConnectTimeout`` means nothing was sent, so
        every request may be retried. Read/protocol errors after sending are only
        retried for idempotent requests (a repeated POST /click would click twice).
        """
        self._check_usable()
        attempt = 0
        while True:
            attempt += 1
            try:
                async with self._client().stream(
                    method,
                    path,
                    params=params,
                    json=json,
                    data=data,
                    files=files,
                    timeout=httpx.Timeout(timeout_s, connect=5.0),
                ) as resp:
                    if resp.status_code != 200:
                        body = await _read_capped(resp, 64_000, allow_partial=True)
                        raise self._status_error(resp.status_code, body)
                    return await _read_capped(resp, max_bytes)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                if attempt < _RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_S * 2 ** (attempt - 1))
                    continue
                raise await self._transport_error(exc) from exc
            except (httpx.ReadError, httpx.RemoteProtocolError) as exc:
                if idempotent and attempt < _RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_S * 2 ** (attempt - 1))
                    continue
                raise await self._transport_error(exc) from exc
            except httpx.HTTPError as exc:
                raise await self._transport_error(exc) from exc

    async def _json(self, method: Method, path: str, **kwargs: Any) -> dict[str, Any]:
        body = await self._request(method, path, **kwargs)
        try:
            parsed = jsonlib.loads(body)
        except ValueError as exc:
            raise SandboxError(f"sandbox returned invalid JSON from {path}") from exc
        if not isinstance(parsed, dict):
            raise SandboxError(f"sandbox returned unexpected JSON from {path}")
        return cast("dict[str, Any]", parsed)

    def _status_error(self, status: int, body: bytes) -> SandboxError:
        if status in (401, 403):
            return SandboxError("sandbox rejected the control token")
        detail = _error_detail(body) or f"HTTP {status}"
        detail = detail.replace(self._token, "***")
        return SandboxError(_clean(detail))

    async def _transport_error(self, exc: httpx.HTTPError) -> SandboxError:
        """Explain a transport failure by looking at the container's state."""
        try:
            await asyncio.to_thread(self._container.reload)
        except docker.errors.NotFound:
            self._killed = True
            await self.aclose()
            return SandboxExpiredError(f"sandbox {self._id[:12]} no longer exists")
        except Exception:
            return SandboxError(f"sandbox control server unreachable ({type(exc).__name__})")
        status = self._container.status
        if status == "paused":
            self._paused = True
            return SandboxError(f"sandbox {self._id[:12]} is paused")
        if status not in ("running", "created", "restarting"):
            return SandboxExpiredError(f"sandbox {self._id[:12]} is {status}")
        if isinstance(exc, httpx.TimeoutException):
            return SandboxError("sandbox control server timed out")
        return SandboxError(f"sandbox control server unreachable ({type(exc).__name__})")

    def _check_xy(self, x: int, y: int) -> None:
        w, h = self._screen_size
        if not (0 <= x < w and 0 <= y < h):
            raise SandboxError(f"coordinates ({x}, {y}) are outside the {w}x{h} screen")

    # -- SandboxHandle ----------------------------------------------------------------

    async def screenshot(self) -> bytes:
        png = await self._request("GET", "/screenshot", idempotent=True, max_bytes=_SCREENSHOT_CAP)
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            raise SandboxError("sandbox returned a screenshot that is not a PNG")
        return png

    async def click(self, x: int, y: int, button: MouseButton = "left", count: int = 1) -> None:
        self._check_xy(x, y)
        if button not in ("left", "right", "middle"):
            raise SandboxError(f"invalid mouse button: {button!r}")
        if not 1 <= count <= 3:
            raise SandboxError("click count must be 1..3")
        await self._request(
            "POST",
            "/click",
            idempotent=False,
            timeout_s=_ACTION_TIMEOUT_S,
            json={"x": x, "y": y, "button": button, "count": count},
        )

    async def move(self, x: int, y: int) -> None:
        self._check_xy(x, y)
        await self._request(
            "POST", "/move", idempotent=True, timeout_s=_ACTION_TIMEOUT_S, json={"x": x, "y": y}
        )

    async def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._check_xy(x1, y1)
        self._check_xy(x2, y2)
        await self._request(
            "POST",
            "/drag",
            idempotent=False,
            timeout_s=_DEFAULT_TIMEOUT_S,
            json={"x1": x1, "y1": y1, "x2": x2, "y2": y2},
        )

    async def scroll(self, x: int, y: int, direction: ScrollDirection, amount: int) -> None:
        self._check_xy(x, y)
        if direction not in ("up", "down", "left", "right"):
            raise SandboxError(f"invalid scroll direction: {direction!r}")
        if not 1 <= amount <= 50:
            raise SandboxError("scroll amount must be 1..50")
        await self._request(
            "POST",
            "/scroll",
            idempotent=False,
            timeout_s=_ACTION_TIMEOUT_S,
            json={"x": x, "y": y, "direction": direction, "amount": amount},
        )

    async def type_text(self, text: str, interval_ms: int = 10) -> None:
        if len(text) > MAX_TYPE_CHARS:
            raise SandboxError(f"text is longer than {MAX_TYPE_CHARS} characters")
        if not 0 <= interval_ms <= MAX_INTERVAL_MS:
            raise SandboxError(f"interval_ms must be 0..{MAX_INTERVAL_MS}")
        if not text:
            return
        typing_s = len(text) * max(interval_ms, 5) / 1000
        await self._request(
            "POST",
            "/type",
            idempotent=False,
            timeout_s=typing_s + _DEFAULT_TIMEOUT_S,
            json={"text": text, "interval_ms": interval_ms},
        )

    async def key(self, combo: str) -> None:
        if not combo or not combo.strip() or len(combo) > MAX_COMBO_CHARS:
            raise SandboxError(f"invalid key combo: {combo!r}")
        await self._request(
            "POST", "/key", idempotent=False, timeout_s=_ACTION_TIMEOUT_S, json={"combo": combo}
        )

    async def run_command(self, cmd: str, timeout_s: int = 60) -> CommandResult:
        if not cmd or not cmd.strip():
            raise SandboxError("cmd must not be empty")
        if not 1 <= timeout_s <= MAX_COMMAND_TIMEOUT_S:
            raise SandboxError(f"timeout_s must be 1..{MAX_COMMAND_TIMEOUT_S}")
        data = await self._json(
            "POST",
            "/command",
            idempotent=False,
            timeout_s=timeout_s + _COMMAND_GRACE_S,
            json={"cmd": cmd, "timeout_s": timeout_s},
        )
        stdout, stderr = data.get("stdout", ""), data.get("stderr", "")
        exit_code, timed_out = data.get("exit_code"), data.get("timed_out", False)
        if not (
            isinstance(stdout, str)
            and isinstance(stderr, str)
            and isinstance(exit_code, int)
            and isinstance(timed_out, bool)
        ):
            raise SandboxError("sandbox returned a malformed command result")
        # The server keeps the first 8 KB of each stream and does not say whether it
        # cut anything: output that reaches the cap is reported as truncated.
        truncated = any(
            len(s.encode("utf-8", "surrogatepass")) >= SERVER_OUTPUT_BYTES
            or len(s) > COMMAND_OUTPUT_LIMIT
            for s in (stdout, stderr)
        )
        return CommandResult(
            stdout=stdout[:COMMAND_OUTPUT_LIMIT],
            stderr=stderr[:COMMAND_OUTPUT_LIMIT],
            exit_code=exit_code,
            truncated=truncated,
            timed_out=timed_out,
        )

    async def write_file(self, path: str, data: bytes) -> None:
        norm = _check_abs_path(path)
        if len(data) > MAX_FILE_BYTES:
            raise SandboxError(f"file is larger than {MAX_FILE_BYTES} bytes")
        await self._request(
            "POST",
            "/files/write",
            idempotent=True,  # overwriting with the same bytes is safe to repeat
            timeout_s=_FILE_TIMEOUT_S,
            data={"path": norm},
            files={
                "file": (
                    posixpath.basename(norm) or "file",
                    bytes(data),
                    "application/octet-stream",
                )
            },
        )

    async def read_file(self, path: str, max_bytes: int = 50_000_000) -> bytes:
        norm = _check_abs_path(path)
        if max_bytes < 0:
            raise SandboxError("max_bytes must be >= 0")
        return await self._request(
            "GET",
            "/files/read",
            idempotent=True,
            timeout_s=_FILE_TIMEOUT_S,
            params={"path": norm},
            max_bytes=max_bytes,
        )

    async def list_dir(self, path: str) -> list[FileInfo]:
        norm = _check_abs_path(path)
        data = await self._json(
            "GET",
            "/files/list",
            idempotent=True,
            timeout_s=_DEFAULT_TIMEOUT_S,
            params={"path": norm},
        )
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise SandboxError("sandbox returned a malformed directory listing")
        result: list[FileInfo] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                raise SandboxError("sandbox returned a malformed directory entry")
            name: str = entry["name"]
            if not name or "/" in name or name in (".", ".."):
                raise SandboxError("sandbox returned an invalid file name")
            is_dir = bool(entry.get("is_dir"))
            size = entry.get("size")
            mtime = entry.get("mtime")
            result.append(
                FileInfo(
                    path=posixpath.join(norm, name),
                    name=name,
                    is_dir=is_dir,
                    size_bytes=size if isinstance(size, int) and size >= 0 and not is_dir else 0,
                    modified_at=(
                        datetime.fromtimestamp(mtime, UTC)
                        if isinstance(mtime, int | float)
                        else None
                    ),
                )
            )
        result.sort(key=lambda info: info.name)
        return result

    async def stream_urls(self) -> StreamUrls:
        if self._killed:
            raise SandboxExpiredError(f"sandbox {self._id[:12]} was killed")
        if self._endpoints is None:
            raise SandboxError(f"sandbox {self._id[:12]} is paused; resume it first")
        base = f"http://127.0.0.1:{self._endpoints.novnc_port}/vnc.html"
        return StreamUrls(
            view_only_url=f"{base}?view_only=1&autoconnect=1&resize=scale",
            interactive_url=f"{base}?autoconnect=1&resize=scale",
        )

    async def pause(self) -> None:
        async with self._state_lock:
            if self._killed:
                raise SandboxExpiredError(f"sandbox {self._id[:12]} was killed")
            await self._provider._call("inspect sandbox", self._container.reload)
            if self._container.status != "paused":
                await self._provider._call("pause sandbox", self._container.pause)
            self._paused = True

    async def resume(self) -> None:
        async with self._state_lock:
            if self._killed:
                raise SandboxExpiredError(f"sandbox {self._id[:12]} was killed")
            await self._provider._call("inspect sandbox", self._container.reload)
            if self._container.status == "paused":
                await self._provider._call("resume sandbox", self._container.unpause)
            if self._endpoints is None:
                self._endpoints = await self._provider._wait_for_ports(self._container)
                self._http = self._provider._http_client(self._endpoints, self._token)
                self._paused = False
                await self.refresh_screen_size()
            self._paused = False

    async def kill(self) -> None:
        async with self._state_lock:
            if self._killed:
                return
            try:
                await asyncio.to_thread(self._container.remove, force=True)
            except docker.errors.NotFound:
                pass
            except docker.errors.APIError as exc:
                # 409: removal already in progress (another kill raced us).
                if exc.status_code != 409:
                    raise _docker_error(exc, "kill sandbox") from exc
            except docker.errors.DockerException as exc:
                raise _docker_error(exc, "kill sandbox") from exc
            self._killed = True
            await self._provider._tokens.delete(self._id)
            await self.aclose()
            log.info("sandbox_killed", sandbox_id=self._id[:12])


async def _read_capped(resp: httpx.Response, cap: int, *, allow_partial: bool = False) -> bytes:
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap and not allow_partial:
        raise SandboxError(f"response is larger than {cap} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > cap:
            if allow_partial:
                chunks.append(chunk[: cap - (total - len(chunk))])
                break
            raise SandboxError(f"response is larger than {cap} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _error_detail(body: bytes) -> str:
    try:
        parsed = jsonlib.loads(body)
    except ValueError:
        return body.decode("utf-8", "replace")[:_ERROR_DETAIL_CHARS]
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):  # FastAPI validation errors
        msgs = [str(item.get("msg", "")) for item in detail if isinstance(item, dict)]
        return "invalid request: " + "; ".join(m for m in msgs if m)
    return ""
