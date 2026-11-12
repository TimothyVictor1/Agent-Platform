"""Egress proxy (T21): address policy, host-name rules, request parsing, and the
proxy end to end against local servers (an injected resolver stands in for DNS)."""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import AsyncIterator

import pytest

from agentplatform.egress.egress_proxy import (
    EgressProxy,
    Policy,
    Refused,
    build,
    check_host_name,
    is_local_only,
    is_public,
    rebuild_head,
    split_absolute_url,
    split_authority,
)

BLOCKED = [
    "10.0.0.1",
    "172.16.5.4",
    "192.168.65.254",
    "127.0.0.1",
    "127.8.8.8",
    "0.0.0.0",
    "169.254.169.254",
    "100.64.0.1",
    "100.127.255.254",
    "224.0.0.1",
    "239.1.2.3",
    "240.0.0.1",
    "255.255.255.255",
    "192.0.2.1",
    "198.18.0.1",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd12:3456::1",
    "ff02::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
    "::ffff:192.168.65.254",
    "2002:c0a8:4101::1",  # 6to4 wrapping 192.168.65.1
    "2002:7f00:0001::1",  # 6to4 wrapping 127.0.0.1
    "64:ff9b::a00:1",  # NAT64 wrapping 10.0.0.1
    "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
    "::7f00:1",  # IPv4-compatible 127.0.0.1
    "2001:db8::1",  # documentation
]
PUBLIC = [
    "1.1.1.1",
    "93.184.215.14",
    "8.8.8.8",
    "2606:4700:4700::1111",
    "::ffff:1.1.1.1",
    "64:ff9b::101:101",
]


@pytest.mark.parametrize("address", BLOCKED)
def test_non_public_addresses_are_blocked(address: str) -> None:
    assert not is_public(ipaddress.ip_address(address))


@pytest.mark.parametrize("address", PUBLIC)
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public(ipaddress.ip_address(address))


def test_local_only() -> None:
    for a in ("127.0.0.1", "::1", "169.254.1.1", "fe80::1", "0.0.0.0", "::ffff:127.0.0.1"):
        assert is_local_only(ipaddress.ip_address(a))
    for a in ("10.0.0.1", "172.20.0.5", "1.1.1.1"):
        assert not is_local_only(ipaddress.ip_address(a))


@pytest.mark.parametrize(
    "host",
    [
        "localhost",
        "LOCALHOST.",
        "host.docker.internal",
        "gateway.docker.internal",
        "metadata.google.internal",
        "printer.local",
        "x.localhost",
        "postgres",
        "redis",
        "a..b",
        "-x.com",
        "x_y.com",
        "x.com/evil",
        "[fe80::1%eth0]",
        "",
    ],
)
def test_local_or_invalid_names_are_refused(host: str) -> None:
    with pytest.raises(Refused):
        check_host_name(host)


def test_names_are_normalised() -> None:
    assert check_host_name("Example.COM.") == "example.com"
    assert check_host_name("[2606:4700::1111]") == "2606:4700::1111"
    with pytest.raises(Refused):
        check_host_name("eval-fixtures")  # single-label: refused unless the flag is on
    assert check_host_name("eval-fixtures", allow_eval_fixtures=True) == "eval-fixtures"
    assert check_host_name("Eval-Fixtures.", allow_eval_fixtures=True) == "eval-fixtures"


def resolver(table: dict[str, list[str]], calls: list[str] | None = None):  # type: ignore[no-untyped-def]
    async def resolve(host: str, port: int) -> list[str]:
        if calls is not None:
            calls.append(host)
        if host not in table:
            raise OSError("NXDOMAIN")
        return table[host]

    return resolve


async def test_policy_checks_every_resolved_address() -> None:
    policy = Policy()
    ok = resolver({"example.com": ["93.184.215.14"]})
    assert await policy.vet("example.com", 443, ok) == ("example.com", "93.184.215.14")
    # DNS that points a public-looking name at the host, or mixes in a private answer
    for answers in (
        ["127.0.0.1"],
        ["192.168.65.254"],
        ["93.184.215.14", "10.0.0.1"],
        ["::ffff:127.0.0.1"],
        ["100.64.1.1"],
    ):
        with pytest.raises(Refused) as refused:
            await policy.vet("evil.example", 443, resolver({"evil.example": answers}))
        assert refused.value.status == 403
    with pytest.raises(Refused, match="port"):
        await policy.vet("example.com", 5432, ok)
    with pytest.raises(Refused, match="port"):
        await policy.vet("example.com", 6379, ok)
    with pytest.raises(Refused):
        await policy.vet("missing.example", 443, ok)
    for literal in ("127.0.0.1", "192.168.65.254", "[::1]", "169.254.169.254"):
        with pytest.raises(Refused):
            await policy.vet(literal, 80, ok)
    assert (await policy.vet("1.1.1.1", 443, ok))[1] == "1.1.1.1"


async def test_eval_fixtures_exception_is_off_by_default() -> None:
    table = {"eval-fixtures": ["172.20.0.5"]}
    assert Policy().allow_eval_fixtures is False
    with pytest.raises(Refused):
        await Policy().vet("eval-fixtures", 80, resolver(table))


async def test_eval_fixtures_exception_covers_exactly_that_name() -> None:
    """With the flag on, only the exact name ``eval-fixtures`` may resolve to a private
    address; every other private name or IP stays blocked, and even ``eval-fixtures``
    never reaches loopback, link-local or unspecified addresses."""
    policy = Policy(allow_eval_fixtures=True)
    private = ["172.20.0.5"]
    assert (await policy.vet("eval-fixtures", 80, resolver({"eval-fixtures": private})))[
        1
    ] == "172.20.0.5"
    for name in (
        "eval-fixtures.evil.com",
        "evil-eval-fixtures",
        "eval-fixtures-2",
        "xeval-fixtures",
        "eval-fixtures.internal",
        "eval_fixtures",
        "postgres",
        "redis",
        "host.docker.internal",
        "lan.example.com",
    ):
        with pytest.raises(Refused):
            await policy.vet(name, 80, resolver({name: private}))
    for literal in ("172.20.0.5", "192.168.65.254", "10.0.0.1", "100.64.0.1", "[fd00::1]"):
        with pytest.raises(Refused):
            await policy.vet(literal, 80, resolver({}))
    for answers in (
        ["127.0.0.1"],
        ["169.254.169.254"],
        ["0.0.0.0"],
        ["::1"],
        ["::ffff:127.0.0.1"],
        ["172.20.0.5", "127.0.0.1"],
    ):
        with pytest.raises(Refused):
            await policy.vet("eval-fixtures", 80, resolver({"eval-fixtures": answers}))
    with pytest.raises(Refused, match="port"):  # ports stay limited too
        await policy.vet("eval-fixtures", 5432, resolver({"eval-fixtures": private}))


def test_parsing() -> None:
    assert split_authority("example.com:443", default_port=None) == ("example.com", 443)
    assert split_authority("[::1]:8443", default_port=None) == ("[::1]", 8443)
    assert split_absolute_url("http://example.com/a?b=1") == ("example.com", 80, "/a?b=1")
    assert split_absolute_url("http://example.com:8080") == ("example.com", 8080, "/")
    for bad in (
        "user@evil.com:443",
        "example.com",
        "example.com:0",
        "example.com:99999",
        "example.com:44a",
    ):
        with pytest.raises(Refused):
            split_authority(bad, default_port=None)
    with pytest.raises(Refused):
        split_absolute_url("ftp://example.com/")
    head = rebuild_head(
        "GET",
        "/x",
        "HTTP/1.1",
        [
            ("Host", "other.com"),
            ("Proxy-Authorization", "Basic x"),
            ("Accept", "*/*"),
            ("Connection", "keep-alive"),
        ],
        "example.com",
        80,
    )
    assert (
        head == b"GET /x HTTP/1.1\r\nHost: example.com\r\nAccept: */*\r\nConnection: close\r\n\r\n"
    )


def test_build_reads_arguments(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("EGRESS_ALLOW_EVAL_FIXTURES", raising=False)
    proxy, host, port = build(["--listen", "127.0.0.1:3999", "--ports", "443"])
    assert (host, port) == ("127.0.0.1", 3999)
    assert proxy.policy.allowed_ports == frozenset({443})
    assert proxy.policy.allow_eval_fixtures is False  # off by default
    monkeypatch.setenv("EGRESS_ALLOW_EVAL_FIXTURES", "1")
    enabled, _, _ = build([])
    assert enabled.policy.allow_eval_fixtures is True
    with pytest.raises(SystemExit):
        build(["--allow-eval-fixtures", "maybe"])


# -- end to end ------------------------------------------------------------------------


class Loopback(Policy):
    """Test policy: vets like ``Policy`` but treats 127.0.0.1 as public, so the upstream
    can be a local server. Only ``public.test`` maps there."""

    async def vet(self, host, port, resolve):  # type: ignore[no-untyped-def]
        if port not in self.allowed_ports:
            raise Refused(403, f"port {port} not allowed")
        name = host.lower()
        addresses = await resolve(name, port)
        if addresses != ["127.0.0.1"]:
            return await super().vet(host, port, resolve)
        return name, "127.0.0.1"


@pytest.fixture
async def upstream() -> AsyncIterator[tuple[int, list[bytes]]]:
    seen: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        data = await reader.read(65536)
        seen.append(data)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    async with server:
        yield port, seen


async def _proxy(
    policy: Policy, table: dict[str, list[str]], calls: list[str]
) -> tuple[asyncio.Server, int, list[dict[str, object]]]:
    logs: list[dict[str, object]] = []
    proxy = EgressProxy(policy=policy, resolve=resolver(table, calls), log=logs.append)
    server = await proxy.serve("127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1], logs


async def _request(port: int, raw: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(raw)
    await writer.drain()
    out = await asyncio.wait_for(reader.read(), 5)
    writer.close()
    return out


async def test_http_and_connect_through_the_proxy(upstream: tuple[int, list[bytes]]) -> None:
    up_port, seen = upstream
    calls: list[str] = []
    server, port, logs = await _proxy(
        Loopback(allowed_ports=frozenset({up_port})), {"public.test": ["127.0.0.1"]}, calls
    )
    async with server:
        out = await _request(
            port,
            f"GET http://public.test:{up_port}/page?q=1 HTTP/1.1\r\nHost: x\r\n"
            "Proxy-Authorization: Basic abc\r\n\r\n".encode(),
        )
        assert out.endswith(b"hello")
        assert seen[-1].startswith(b"GET /page?q=1 HTTP/1.1\r\nHost: public.test:")
        assert b"Proxy-Authorization" not in seen[-1]

        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(f"CONNECT public.test:{up_port} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 200")
        writer.write(b"PING tunnel\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 5)).endswith(b"hello")
        writer.close()
        assert seen[-1] == b"PING tunnel\r\n\r\n"
    assert calls == ["public.test", "public.test"]  # resolved once per request
    assert [entry["event"] for entry in logs] == ["allowed", "allowed"]
    assert all("page" not in str(entry) for entry in logs)  # no paths in logs


async def test_blocked_destinations_get_403_and_no_connection(
    upstream: tuple[int, list[bytes]],
) -> None:
    up_port, seen = upstream
    server, port, logs = await _proxy(
        Policy(allowed_ports=frozenset({80, 443, up_port})),
        {"rebind.example": ["127.0.0.1"], "lan.example": ["192.168.65.254"]},
        [],
    )
    async with server:
        for raw in (
            f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\n\r\n",
            f"CONNECT rebind.example:{up_port} HTTP/1.1\r\n\r\n",
            "CONNECT host.docker.internal:443 HTTP/1.1\r\n\r\n",
            "CONNECT lan.example:443 HTTP/1.1\r\n\r\n",
            "CONNECT example.com:5432 HTTP/1.1\r\n\r\n",
            "CONNECT example.com:6379 HTTP/1.1\r\n\r\n",
            f"GET http://127.0.0.1:{up_port}/ HTTP/1.1\r\n\r\n",
            "GET /relative HTTP/1.1\r\nHost: example.com\r\n\r\n",
            "garbage\r\n\r\n",
        ):
            out = await _request(port, raw.encode())
            assert out.split(b"\r\n", 1)[0] in (
                b"HTTP/1.1 403 Forbidden",
                b"HTTP/1.1 400 Bad Request",
            ), raw
    assert seen == []  # nothing ever reached the local server
    assert all(entry["event"] == "refused" for entry in logs)


async def test_oversized_head_is_refused() -> None:
    server, port, _ = await _proxy(Policy(), {}, [])
    async with server:
        out = await _request(port, b"GET http://example.com/ HTTP/1.1\r\nX: " + b"a" * 20000)
    assert out.startswith(b"HTTP/1.1 431")
