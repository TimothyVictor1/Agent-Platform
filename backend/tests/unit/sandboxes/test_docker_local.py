"""Unit tests for the docker_local provider with a fake Docker client and a mocked
control server (no Docker, no network). The live behaviour is covered by
``tests/integration/test_sandbox_contract.py`` with ``APP_DOCKER=1``."""

from __future__ import annotations

import builtins
import json
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

import docker
import docker.errors
import httpx
import pytest

from agentplatform.config import Settings
from agentplatform.contracts.common import utcnow
from agentplatform.contracts.sandbox import SandboxSpec
from agentplatform.errors import NotSupportedError, SandboxError, SandboxExpiredError
from agentplatform.sandboxes.base import SandboxHandle, SandboxProvider
from agentplatform.sandboxes.docker_local import (
    LABEL_EXPIRES_AT,
    LABEL_PROVIDER,
    LABEL_SESSION,
    LABEL_WORKTREE,
    NETWORK_NAME,
    DockerLocalHandle,
    DockerLocalProvider,
    InMemoryControlTokenStore,
    worktree_label,
)
from agentplatform.testing.images import make_png

WORKTREE = "unit_wt"
PNG = make_png(1280, 800)


# --- fake Docker SDK ----------------------------------------------------------------


class FakeContainer:
    def __init__(self, client: FakeDockerClient, kwargs: dict[str, Any]) -> None:
        self.client = client
        self.kwargs = kwargs
        self.id: str | None = uuid.uuid4().hex + uuid.uuid4().hex
        self.labels: dict[str, str] = dict(kwargs.get("labels") or {})
        self.status = "created"
        self.removed = False
        self.calls: list[str] = []
        self.publish_ports = True
        self.env = [f"{k}={v}" for k, v in (kwargs.get("environment") or {}).items()]
        self.exit_on_start = False

    @property
    def ports(self) -> dict[str, Any]:
        if self.status != "running" or not self.publish_ports:
            return {}
        return {
            "7000/tcp": [
                {"HostIp": "127.0.0.1", "HostPort": "47000"},
                {"HostIp": "::", "HostPort": "1"},  # must be ignored
            ],
            "6080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "46080"}],
        }

    @property
    def attrs(self) -> dict[str, Any]:
        return {"Config": {"Env": self.env}}

    def _alive(self) -> None:
        if self.removed:
            raise docker.errors.NotFound("No such container")

    def start(self) -> None:
        self._alive()
        self.calls.append("start")
        self.status = "exited" if self.exit_on_start else "running"

    def reload(self) -> None:
        self._alive()
        self.calls.append("reload")

    def pause(self) -> None:
        self._alive()
        self.calls.append("pause")
        self.status = "paused"

    def unpause(self) -> None:
        self._alive()
        self.calls.append("unpause")
        self.status = "running"

    def remove(self, *, force: bool = False) -> None:
        self.calls.append(f"remove(force={force})")
        if self.client.remove_error is not None:
            raise self.client.remove_error
        self._alive()
        self.removed = True
        self.client.containers.by_id.pop(cast(str, self.id), None)


class FakeContainers:
    def __init__(self, client: FakeDockerClient) -> None:
        self.client = client
        self.by_id: dict[str, FakeContainer] = {}
        self.created: list[FakeContainer] = []
        self.create_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.list_filters: list[dict[str, Any]] = []
        self.extra_listed: list[FakeContainer] = []

    def create(self, *args: Any, **kwargs: Any) -> FakeContainer:
        self.create_calls.append((args, kwargs))
        if self.client.create_error is not None:
            raise self.client.create_error
        container = FakeContainer(self.client, kwargs)
        self.by_id[cast(str, container.id)] = container
        self.created.append(container)
        return container

    def get(self, container_id: str) -> FakeContainer:
        for cid, container in self.by_id.items():
            if cid.startswith(container_id):
                return container
        raise docker.errors.NotFound("No such container")

    def list(self, *, all: bool = False, filters: dict[str, Any] | None = None) -> list[Any]:
        self.list_filters.append(filters or {})
        wanted = dict(item.split("=", 1) for item in (filters or {}).get("label", []))
        out = [
            c
            for c in self.by_id.values()
            if builtins.all(c.labels.get(k) == v for k, v in wanted.items())
        ]
        return out + self.extra_listed


class FakeNetwork:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeNetworks:
    def __init__(self) -> None:
        self.existing: list[FakeNetwork] = []
        self.create_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.create_error: Exception | None = None

    def list(self, names: list[str] | None = None) -> list[FakeNetwork]:
        return [n for n in self.existing if names is None or n.name in names]

    def create(self, *args: Any, **kwargs: Any) -> FakeNetwork:
        self.create_calls.append((args, kwargs))
        if self.create_error is not None:
            raise self.create_error
        net = FakeNetwork(args[0])
        self.existing.append(net)
        return net


class FakeImages:
    def __init__(self) -> None:
        self.present = True

    def get(self, name: str) -> object:
        if not self.present:
            raise docker.errors.ImageNotFound(f"No such image: {name}")
        return object()


class FakeDockerClient:
    def __init__(self) -> None:
        self.containers = FakeContainers(self)
        self.networks = FakeNetworks()
        self.images = FakeImages()
        self.ping_ok = True
        self.create_error: Exception | None = None
        self.remove_error: Exception | None = None

    def ping(self) -> bool:
        if not self.ping_ok:
            raise docker.errors.DockerException("daemon down")
        return True


# --- fake control server ------------------------------------------------------------

Handler = Callable[[httpx.Request], httpx.Response]


class FakeControl:
    """Routes requests to per-path handlers and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Handler] = {
            "/health": lambda r: httpx.Response(200, json={"status": "ok"}),
            "/screen": lambda r: httpx.Response(200, json={"width": 1280, "height": 800}),
            "/screenshot": lambda r: httpx.Response(200, content=PNG),
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get(request.url.path)
        if handler is None:
            return httpx.Response(200, json={"status": "ok"})
        return handler(request)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]

    def count(self, path: str) -> int:
        return self.paths().count(path)


@pytest.fixture
def fake_docker() -> FakeDockerClient:
    return FakeDockerClient()


@pytest.fixture
def control() -> FakeControl:
    return FakeControl()


@pytest.fixture
def store() -> InMemoryControlTokenStore:
    return InMemoryControlTokenStore()


@pytest.fixture
def provider(
    fake_docker: FakeDockerClient, control: FakeControl, store: InMemoryControlTokenStore
) -> DockerLocalProvider:
    return DockerLocalProvider(
        store,
        worktree=WORKTREE,
        docker_client=cast(docker.DockerClient, fake_docker),
        http_transport=httpx.MockTransport(control.handle),
        ready_timeout_s=0.3,
        poll_interval_s=0.01,
    )


async def _create(provider: DockerLocalProvider, **spec: Any) -> DockerLocalHandle:
    return await provider.create(SandboxSpec(**spec))


def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agentplatform.sandboxes.docker_local._RETRY_BACKOFF_S", 0.0)


# --- create -------------------------------------------------------------------------


async def test_protocols(provider: DockerLocalProvider) -> None:
    assert isinstance(provider, SandboxProvider)
    handle = await _create(provider)
    assert isinstance(handle, SandboxHandle)
    assert provider.name == handle.provider == "docker_local"


async def test_create_uses_exact_hardening_flags_and_no_mounts(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    await _create(provider, session_id="s1", agent_id="a1")
    (args, kwargs) = fake_docker.containers.create_calls[0]
    assert args == ("agentplatform/desktop:latest",)
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["cap_add"] == ["SETUID", "SETGID"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["privileged"] is False
    assert kwargs["mem_limit"] == "4g"
    assert kwargs["memswap_limit"] == "4g"
    assert kwargs["nano_cpus"] == 2_000_000_000
    assert kwargs["pids_limit"] == 4096
    assert kwargs["network"] == NETWORK_NAME
    for forbidden in ("volumes", "mounts", "devices", "volumes_from", "network_mode", "pid_mode"):
        assert forbidden not in kwargs
    assert kwargs["ports"] == {"7000/tcp": ("127.0.0.1", None), "6080/tcp": ("127.0.0.1", None)}


async def test_create_labels_and_token_handling(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient, store: InMemoryControlTokenStore
) -> None:
    handle = await _create(provider, session_id="s1", agent_id="a1", timeout_minutes=30)
    kwargs = fake_docker.containers.create_calls[0][1]
    labels: dict[str, str] = kwargs["labels"]
    assert labels[LABEL_WORKTREE] == WORKTREE
    assert labels[LABEL_SESSION] == "s1"
    assert labels["agentplatform.agent"] == "a1"
    assert labels[LABEL_PROVIDER] == "docker_local"
    env: dict[str, str] = kwargs["environment"]
    token = env["CONTROL_TOKEN"]
    assert len(token) >= 40
    assert env["SCREEN_WIDTH"] == "1280"
    assert env["SCREEN_HEIGHT"] == "800"
    # The token goes into the store, never into a label (and never into repr).
    assert all(token not in value for value in labels.values())
    assert await store.get(handle.id) == token
    assert token not in repr(handle)
    created = labels["agentplatform.created_at"]
    expires = labels[LABEL_EXPIRES_AT]
    assert expires > created


async def test_create_waits_for_health_and_screenshot_with_bearer(
    provider: DockerLocalProvider, control: FakeControl, fake_docker: FakeDockerClient
) -> None:
    await _create(provider)
    assert control.paths()[:2] == ["/health", "/screenshot"]
    token = fake_docker.containers.create_calls[0][1]["environment"]["CONTROL_TOKEN"]
    request = control.requests[0]
    assert request.headers["authorization"] == f"Bearer {token}"
    assert request.url.host == "127.0.0.1"
    assert request.url.port == 47000


async def test_create_retries_until_ready(
    provider: DockerLocalProvider, control: FakeControl
) -> None:
    calls = {"n": 0}

    def health(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"status": "ok"})

    control.routes["/health"] = health
    await _create(provider)
    assert calls["n"] == 3


async def test_create_passes_user_env_and_screen(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    handle = await _create(provider, env={"LANG": "C.UTF-8"}, screen=(1024, 768))
    env = fake_docker.containers.create_calls[0][1]["environment"]
    assert env["LANG"] == "C.UTF-8"
    assert (env["SCREEN_WIDTH"], env["SCREEN_HEIGHT"]) == ("1024", "768")
    assert handle.screen_size == (1024, 768)


async def test_allowlist_network_is_not_supported(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    with pytest.raises(NotSupportedError):
        await _create(provider, network="allowlist", allowlist=["example.com"])
    assert fake_docker.containers.create_calls == []


@pytest.mark.parametrize("key", ["CONTROL_TOKEN", "control_token_file", "SCREEN_WIDTH", "PATH"])
async def test_reserved_env_is_rejected(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient, key: str
) -> None:
    with pytest.raises(SandboxError, match="reserved"):
        await _create(provider, env={key: "x"})
    assert fake_docker.containers.create_calls == []


@pytest.mark.parametrize("env", [{"BAD-NAME": "x"}, {"OK": "nul\x00"}])
async def test_invalid_env_is_rejected(provider: DockerLocalProvider, env: dict[str, str]) -> None:
    with pytest.raises(SandboxError):
        await _create(provider, env=env)


async def test_network_is_created_with_icc_disabled_once(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    await _create(provider)
    await _create(provider)
    assert len(fake_docker.networks.create_calls) == 1
    args, kwargs = fake_docker.networks.create_calls[0]
    assert args == (NETWORK_NAME,)
    assert kwargs["driver"] == "bridge"
    assert kwargs["options"] == {"com.docker.network.bridge.enable_icc": "false"}
    assert kwargs.get("internal", False) is False  # agents need the web


async def test_existing_network_is_reused(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    fake_docker.networks.existing.append(FakeNetwork(NETWORK_NAME))
    await _create(provider)
    assert fake_docker.networks.create_calls == []


async def test_network_create_race_is_tolerated(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    fake_docker.networks.create_error = docker.errors.APIError(
        "conflict", response=cast(Any, _Resp(409)), explanation="network exists"
    )
    await _create(provider)
    assert len(fake_docker.networks.create_calls) == 1


class _Resp:
    """Minimal ``requests.Response`` stand-in for ``docker.errors.APIError``."""

    def __init__(self, status: int) -> None:
        self.status_code = status
        self.reason = "x"
        self.url = "http://docker/x"


async def test_create_cleans_up_when_never_ready(
    provider: DockerLocalProvider,
    fake_docker: FakeDockerClient,
    control: FakeControl,
    store: InMemoryControlTokenStore,
) -> None:
    control.routes["/health"] = lambda r: httpx.Response(503)
    with pytest.raises(SandboxError, match="not ready"):
        await _create(provider)
    container = fake_docker.containers.created[0]
    assert container.removed
    assert "remove(force=True)" in container.calls
    assert len(store) == 0


async def test_create_fails_fast_when_token_rejected(
    provider: DockerLocalProvider,
    fake_docker: FakeDockerClient,
    control: FakeControl,
    store: InMemoryControlTokenStore,
) -> None:
    control.routes["/health"] = lambda r: httpx.Response(401)
    with pytest.raises(SandboxError, match="token"):
        await _create(provider)
    assert control.count("/health") == 1
    assert fake_docker.containers.created[0].removed
    assert len(store) == 0


async def test_create_fails_when_container_exits(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    original = fake_docker.containers.create

    def create(*args: Any, **kwargs: Any) -> FakeContainer:
        container = original(*args, **kwargs)
        container.exit_on_start = True
        return container

    fake_docker.containers.create = create  # type: ignore[method-assign]
    with pytest.raises(SandboxError, match="stopped during start"):
        await _create(provider)
    assert fake_docker.containers.created[0].removed


async def test_create_maps_missing_image(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    fake_docker.create_error = docker.errors.ImageNotFound("No such image")
    with pytest.raises(SandboxError, match="create sandbox"):
        await _create(provider)


# --- requests, retries, errors --------------------------------------------------------


async def test_actions_send_expected_bodies(
    provider: DockerLocalProvider, control: FakeControl
) -> None:
    handle = await _create(provider)
    await handle.click(10, 20, "right", 2)
    await handle.move(1, 2)
    await handle.drag(1, 2, 3, 4)
    await handle.scroll(5, 6, "down", 3)
    await handle.type_text("héllo", interval_ms=0)
    await handle.key("ctrl+l")
    bodies = {r.url.path: json.loads(r.content) for r in control.requests if r.method == "POST"}
    assert bodies["/click"] == {"x": 10, "y": 20, "button": "right", "count": 2}
    assert bodies["/move"] == {"x": 1, "y": 2}
    assert bodies["/drag"] == {"x1": 1, "y1": 2, "x2": 3, "y2": 4}
    assert bodies["/scroll"] == {"x": 5, "y": 6, "direction": "down", "amount": 3}
    assert bodies["/type"] == {"text": "héllo", "interval_ms": 0}
    assert bodies["/key"] == {"combo": "ctrl+l"}


@pytest.mark.parametrize(
    "call",
    [
        lambda h: h.click(1280, 0),
        lambda h: h.click(-1, 0),
        lambda h: h.click(0, 0, count=4),
        lambda h: h.click(0, 0, button="side"),
        lambda h: h.move(0, 800),
        lambda h: h.drag(0, 0, 0, 9999),
        lambda h: h.scroll(0, 0, "down", 0),
        lambda h: h.scroll(0, 0, "down", 51),
        lambda h: h.type_text("x" * 2001),
        lambda h: h.type_text("x", interval_ms=-1),
        lambda h: h.key(""),
        lambda h: h.run_command(""),
        lambda h: h.run_command("true", timeout_s=0),
        lambda h: h.write_file("relative.txt", b""),
        lambda h: h.read_file("relative.txt"),
        lambda h: h.list_dir("home/agent"),
        lambda h: h.read_file("/home/agent/\x00"),
    ],
)
async def test_invalid_arguments_raise_without_a_request(
    provider: DockerLocalProvider, control: FakeControl, call: Callable[[Any], Any]
) -> None:
    handle = await _create(provider)
    before = len(control.requests)
    with pytest.raises(SandboxError):
        await call(handle)
    assert len(control.requests) == before


async def test_post_is_retried_only_when_nothing_was_sent(
    provider: DockerLocalProvider, control: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_sleep(monkeypatch)
    handle = await _create(provider)
    attempts = {"n": 0}

    def flaky_connect(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json={"status": "ok"})

    control.routes["/click"] = flaky_connect
    await handle.click(1, 1)
    assert attempts["n"] == 2

    def reset_after_send(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadError("connection reset", request=request)

    control.routes["/click"] = reset_after_send
    before = control.count("/click")
    with pytest.raises(SandboxError, match="unreachable"):
        await handle.click(1, 1)
    assert control.count("/click") == before + 1  # a click is never repeated


async def test_get_is_retried_on_read_errors(
    provider: DockerLocalProvider, control: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_sleep(monkeypatch)
    handle = await _create(provider)
    attempts = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.RemoteProtocolError("reset", request=request)
        return httpx.Response(200, content=PNG)

    control.routes["/screenshot"] = flaky
    assert await handle.screenshot() == PNG
    assert attempts["n"] == 3


async def test_retries_are_bounded(
    provider: DockerLocalProvider, control: FakeControl, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_sleep(monkeypatch)
    handle = await _create(provider)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    control.routes["/screenshot"] = refuse
    before = control.count("/screenshot")
    with pytest.raises(SandboxError):
        await handle.screenshot()
    assert control.count("/screenshot") == before + 3


async def test_http_errors_map_to_sandbox_error_with_clean_detail(
    provider: DockerLocalProvider, control: FakeControl, fake_docker: FakeDockerClient
) -> None:
    handle = await _create(provider)
    token = fake_docker.containers.create_calls[0][1]["environment"]["CONTROL_TOKEN"]
    control.routes["/files/read"] = lambda r: httpx.Response(
        404, json={"detail": f"no such file\x1b[31m {token} " + "y" * 2000}
    )
    with pytest.raises(SandboxError) as info:
        await handle.read_file("/home/agent/missing")
    message = str(info.value)
    assert message.startswith("no such file")
    assert token not in message
    assert "\x1b" not in message
    assert len(message) <= 500

    control.routes["/key"] = lambda r: httpx.Response(
        422, json={"detail": [{"msg": "unknown key 'foo'"}]}
    )
    with pytest.raises(SandboxError, match="unknown key 'foo'"):
        await handle.key("foo")

    control.routes["/screenshot"] = lambda r: httpx.Response(401)
    with pytest.raises(SandboxError, match="rejected the control token"):
        await handle.screenshot()


async def test_transport_error_on_removed_container_is_expired(
    provider: DockerLocalProvider,
    control: FakeControl,
    fake_docker: FakeDockerClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _no_sleep(monkeypatch)
    handle = await _create(provider)
    fake_docker.containers.created[0].remove(force=True)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    control.routes["/screenshot"] = refuse
    with pytest.raises(SandboxExpiredError):
        await handle.screenshot()
    with pytest.raises(SandboxExpiredError):
        await handle.click(1, 1)


async def test_timeout_reports_timeout(provider: DockerLocalProvider, control: FakeControl) -> None:
    handle = await _create(provider)

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    control.routes["/command"] = slow
    with pytest.raises(SandboxError, match="timed out"):
        await handle.run_command("sleep 1")


async def test_non_png_screenshot_is_rejected(
    provider: DockerLocalProvider, control: FakeControl
) -> None:
    handle = await _create(provider)
    control.routes["/screenshot"] = lambda r: httpx.Response(200, content=b"<html>")
    with pytest.raises(SandboxError, match="PNG"):
        await handle.screenshot()


# --- commands and files -------------------------------------------------------------


async def test_run_command_maps_result_and_infers_truncation(
    provider: DockerLocalProvider, control: FakeControl
) -> None:
    handle = await _create(provider)
    result = {"stdout": "hi\n", "stderr": "", "exit_code": 3, "timed_out": False}
    control.routes["/command"] = lambda r: httpx.Response(200, json=result)
    out = await handle.run_command("echo hi; exit 3", timeout_s=5)
    assert (out.stdout, out.exit_code, out.truncated, out.timed_out) == ("hi\n", 3, False, False)
    assert json.loads(control.requests[-1].content) == {"cmd": "echo hi; exit 3", "timeout_s": 5}

    result = {"stdout": "a" * 8192, "stderr": "", "exit_code": 0, "timed_out": False}
    out = await handle.run_command("yes a")
    assert out.truncated
    assert len(out.stdout) == 8192

    result = {"stdout": "", "stderr": "", "exit_code": 124, "timed_out": True}
    out = await handle.run_command("sleep 99", timeout_s=1)
    assert out.timed_out
    assert out.exit_code == 124


async def test_run_command_rejects_malformed_result(
    provider: DockerLocalProvider, control: FakeControl
) -> None:
    handle = await _create(provider)
    control.routes["/command"] = lambda r: httpx.Response(200, json={"stdout": 1})
    with pytest.raises(SandboxError, match="malformed"):
        await handle.run_command("true")
    control.routes["/command"] = lambda r: httpx.Response(200, content=b"not json")
    with pytest.raises(SandboxError, match="invalid JSON"):
        await handle.run_command("true")


async def test_files_roundtrip_requests(
    provider: DockerLocalProvider, control: FakeControl
) -> None:
    handle = await _create(provider)
    await handle.write_file("/home/agent/a/../b.txt", b"data")
    write = control.requests[-1]
    assert write.url.path == "/files/write"
    assert b'name="path"\r\n\r\n/home/agent/b.txt' in write.content
    assert b"data" in write.content

    control.routes["/files/read"] = lambda r: httpx.Response(200, content=b"x" * 100)
    assert await handle.read_file("/home/agent/b.txt") == b"x" * 100
    assert control.requests[-1].url.params["path"] == "/home/agent/b.txt"
    with pytest.raises(SandboxError, match="larger than 10"):
        await handle.read_file("/home/agent/b.txt", max_bytes=10)


async def test_list_dir_sorts_by_name_and_builds_paths(
    provider: DockerLocalProvider, control: FakeControl
) -> None:
    handle = await _create(provider)
    entries = [
        {"name": "zdir", "size": None, "is_dir": True, "mtime": 1.5},
        {"name": "b.txt", "size": 5, "is_dir": False, "mtime": 2},
        {"name": "a.txt", "size": 7, "is_dir": False, "mtime": None},
    ]
    control.routes["/files/list"] = lambda r: httpx.Response(
        200, json={"path": "/home/agent/", "entries": entries}
    )
    listing = await handle.list_dir("/home/agent/")
    assert [i.name for i in listing] == ["a.txt", "b.txt", "zdir"]
    assert [i.path for i in listing] == [
        "/home/agent/a.txt",
        "/home/agent/b.txt",
        "/home/agent/zdir",
    ]
    assert [i.size_bytes for i in listing] == [7, 5, 0]
    assert listing[2].is_dir
    assert listing[0].modified_at is None
    assert listing[1].modified_at is not None


@pytest.mark.parametrize("bad", [{"name": "../x"}, {"name": "a/b"}, {"size": 1}, "x"])
async def test_list_dir_rejects_bad_entries(
    provider: DockerLocalProvider, control: FakeControl, bad: object
) -> None:
    handle = await _create(provider)
    control.routes["/files/list"] = lambda r: httpx.Response(200, json={"entries": [bad]})
    with pytest.raises(SandboxError):
        await handle.list_dir("/home/agent")


async def test_stream_urls_use_loopback_novnc(provider: DockerLocalProvider) -> None:
    handle = await _create(provider)
    urls = await handle.stream_urls()
    assert urls.view_only_url == (
        "http://127.0.0.1:46080/vnc.html?view_only=1&autoconnect=1&resize=scale"
    )
    assert urls.interactive_url == "http://127.0.0.1:46080/vnc.html?autoconnect=1&resize=scale"


# --- pause / resume / kill ------------------------------------------------------------


async def test_pause_blocks_requests_and_resume_restores(
    provider: DockerLocalProvider, control: FakeControl, fake_docker: FakeDockerClient
) -> None:
    handle = await _create(provider)
    container = fake_docker.containers.created[0]
    await handle.pause()
    await handle.pause()  # already paused: no second docker pause
    assert container.calls.count("pause") == 1
    before = len(control.requests)
    with pytest.raises(SandboxError, match="paused"):
        await handle.click(1, 1)
    with pytest.raises(SandboxError, match="paused"):
        await handle.screenshot()
    assert len(control.requests) == before
    await handle.resume()
    await handle.resume()  # running: no-op
    assert container.calls.count("unpause") == 1
    await handle.click(1, 1)


async def test_kill_is_idempotent_and_expires_the_handle(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient, store: InMemoryControlTokenStore
) -> None:
    handle = await _create(provider)
    container = fake_docker.containers.created[0]
    await handle.kill()
    await handle.kill()
    assert container.removed
    assert container.calls.count("remove(force=True)") == 1
    assert handle.id not in store
    for call in (
        handle.screenshot(),
        handle.click(1, 1),
        handle.run_command("true"),
        handle.stream_urls(),
        handle.pause(),
        handle.resume(),
    ):
        with pytest.raises(SandboxExpiredError):
            await call


async def test_kill_tolerates_gone_and_racing_removal(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    handle = await _create(provider)
    fake_docker.containers.created[0].remove(force=True)
    await handle.kill()
    assert handle.killed

    handle2 = await _create(provider)
    fake_docker.remove_error = docker.errors.APIError(
        "conflict", response=cast(Any, _Resp(409)), explanation="removal in progress"
    )
    await handle2.kill()
    assert handle2.killed


async def test_kill_failure_is_reported_and_retryable(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient
) -> None:
    handle = await _create(provider)
    fake_docker.remove_error = docker.errors.APIError(
        "boom", response=cast(Any, _Resp(500)), explanation="daemon error"
    )
    with pytest.raises(SandboxError, match="daemon error"):
        await handle.kill()
    assert not handle.killed
    fake_docker.remove_error = None
    await handle.kill()
    assert handle.killed


# --- connect ------------------------------------------------------------------------


async def test_connect_reattaches_with_stored_token(
    provider: DockerLocalProvider, control: FakeControl, fake_docker: FakeDockerClient
) -> None:
    handle = await _create(provider)
    token = fake_docker.containers.create_calls[0][1]["environment"]["CONTROL_TOKEN"]
    again = await provider.connect(handle.id)
    assert again.id == handle.id
    assert again.screen_size == (1280, 800)
    assert control.requests[-1].url.path == "/screen"
    assert control.requests[-1].headers["authorization"] == f"Bearer {token}"


async def test_connect_after_restart_uses_durable_store(
    fake_docker: FakeDockerClient, control: FakeControl, store: InMemoryControlTokenStore
) -> None:
    def make() -> DockerLocalProvider:
        return DockerLocalProvider(
            store,
            worktree=WORKTREE,
            docker_client=cast(docker.DockerClient, fake_docker),
            http_transport=httpx.MockTransport(control.handle),
            poll_interval_s=0.01,
        )

    handle = await make().create(SandboxSpec())
    reattached = await make().connect(handle.id)  # a fresh provider, same store
    await reattached.click(1, 1)


async def test_connect_errors(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient, store: InMemoryControlTokenStore
) -> None:
    with pytest.raises(SandboxExpiredError):
        await provider.connect("f" * 64)
    with pytest.raises(SandboxExpiredError, match="invalid"):
        await provider.connect("../etc")

    handle = await _create(provider)
    await store.delete(handle.id)
    with pytest.raises(SandboxExpiredError, match="no control token"):
        await provider.connect(handle.id)

    other = await _create(provider)
    fake_docker.containers.created[-1].labels[LABEL_PROVIDER] = "something_else"
    with pytest.raises(SandboxExpiredError):
        await provider.connect(other.id)

    stopped = await _create(provider)
    fake_docker.containers.created[-1].status = "exited"
    with pytest.raises(SandboxExpiredError, match="exited"):
        await provider.connect(stopped.id)


async def test_connect_to_paused_sandbox_learns_ports_on_resume(
    provider: DockerLocalProvider, control: FakeControl, fake_docker: FakeDockerClient
) -> None:
    handle = await _create(provider, screen=(1024, 768))
    await handle.pause()  # the fake publishes no ports while paused, like Docker
    reattached = await provider.connect(handle.id)
    assert reattached.paused
    assert reattached.screen_size == (1024, 768)  # from the container env
    with pytest.raises(SandboxError, match="paused"):
        await reattached.stream_urls()
    control.routes["/screen"] = lambda r: httpx.Response(200, json={"width": 1024, "height": 768})
    await reattached.resume()
    assert not _is_paused(reattached)
    assert (await reattached.stream_urls()).interactive_url.startswith("http://127.0.0.1:46080/")
    await reattached.click(1, 1)


def _is_paused(handle: DockerLocalHandle) -> bool:
    return handle.paused  # a call, so mypy does not keep the narrowed property type


# --- cleanup / health / config ------------------------------------------------------


async def test_cleanup_orphans_only_touches_this_worktree(
    provider: DockerLocalProvider, fake_docker: FakeDockerClient, store: InMemoryControlTokenStore
) -> None:
    alive = await _create(provider, session_id="alive")
    dead = await _create(provider, session_id="dead")
    unlabeled = await _create(provider)
    expired = await _create(provider, session_id="alive")
    fake_docker.containers.created[-1].labels[LABEL_EXPIRES_AT] = (
        utcnow() - timedelta(minutes=1)
    ).isoformat()

    stranger = FakeContainer(
        fake_docker,
        {
            "labels": {
                LABEL_PROVIDER: "docker_local",
                LABEL_WORKTREE: "other_wt",
                LABEL_SESSION: "dead",
            }
        },
    )
    fake_docker.containers.extra_listed.append(stranger)  # a daemon that ignored the filter

    removed = await provider.cleanup_orphans({"alive"})
    assert set(removed) == {dead.id, unlabeled.id, expired.id}
    assert alive.id in store
    assert dead.id not in store
    assert not stranger.removed
    labels = fake_docker.containers.list_filters[-1]["label"]
    assert f"{LABEL_WORKTREE}={WORKTREE}" in labels


async def test_health(provider: DockerLocalProvider, fake_docker: FakeDockerClient) -> None:
    status = await provider.health()
    assert status.ok
    assert status.latency_ms is not None
    fake_docker.images.present = False
    status = await provider.health()
    assert not status.ok
    assert "make sandbox-image" in (status.error or "")
    fake_docker.ping_ok = False
    status = await provider.health()
    assert not status.ok
    assert status.error == "Docker daemon is not reachable"


def test_worktree_label_matches_teardown_sanitising() -> None:
    assert worktree_label("working-task.t07/x") == "working_task_t07_x"
    assert worktree_label("ok_name_1") == "ok_name_1"
    assert worktree_label("") != ""  # falls back to the repo folder name


def test_from_settings_uses_app_worktree() -> None:
    settings = Settings(app_worktree="wt-a")
    assert DockerLocalProvider.from_settings(settings).worktree == "wt_a"


async def test_http_client_ignores_proxy_env(
    provider: DockerLocalProvider, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    handle = await _create(provider)
    client = handle._client()
    assert client.trust_env is False


async def test_mem_limit_applies_to_memory_and_swap(
    fake_docker: FakeDockerClient, control: FakeControl, store: InMemoryControlTokenStore
) -> None:
    provider = DockerLocalProvider(
        store,
        worktree=WORKTREE,
        docker_client=cast(docker.DockerClient, fake_docker),
        http_transport=httpx.MockTransport(control.handle),
        ready_timeout_s=0.3,
        poll_interval_s=0.01,
        mem_limit="2g",
    )
    await _create(provider)
    (_args, kwargs) = fake_docker.containers.create_calls[0]
    assert kwargs["mem_limit"] == "2g"
    assert kwargs["memswap_limit"] == "2g"
