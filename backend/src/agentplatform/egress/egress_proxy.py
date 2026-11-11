"""Egress proxy for docker_local sandboxes (T21, docs/security.md known issue 1).

Standard library only, so it runs unchanged in a pinned ``python:*-alpine`` image
(``docker compose --profile egress up -d egress-proxy``) with this one file mounted
read-only. Sandboxes in egress mode have no default route; this proxy is the only
host they can reach.

Rules, for every request (HTTP ``CONNECT host:port`` and absolute-form ``GET
http://host/...``):

* the port must be in ``allowed_ports`` (80 and 443 by default);
* the host name must not be a local/internal name (``localhost``, ``*.localhost``,
  ``*.local``, ``*.internal`` such as ``host.docker.internal``), unless it is listed
  in ``allow_private_hosts``;
* the name is resolved once, and **every** resolved address must be public: no
  private, loopback, link-local, CGNAT (100.64/10), multicast, reserved or
  unspecified address, also when wrapped in IPv6 (IPv4-mapped, 6to4, Teredo, NAT64);
* the proxy then connects to the vetted address itself (never re-resolving), so a
  DNS answer that changes between check and connect (rebinding) cannot reach the
  host.

Hosts in ``allow_private_hosts`` (exact names, e.g. ``eval-fixtures``) may resolve to
private addresses, but never to loopback, link-local or unspecified ones.

Logs one JSON line per decision (host, port, verdict, reason) without paths or
queries.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import ipaddress
import json
import os
import re
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], Awaitable[list[str]]]

DEFAULT_PORTS = frozenset({80, 443})
MAX_HEAD_BYTES = 16 * 1024
_HOST_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?)*\.?$"
)
_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".localdomain", ".home.arpa")
_BLOCKED_NAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_HOP_HEADERS = frozenset(
    {"proxy-connection", "proxy-authorization", "proxy-authenticate", "connection", "keep-alive"}
)


class Refused(Exception):
    """A request the policy refuses; ``status`` is sent to the client."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64 or ip in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if int(ip) >> 32 == 0 and int(ip) > 1:  # deprecated IPv4-compatible ::a.b.c.d
        return ipaddress.IPv4Address(int(ip))
    return None


def is_public(ip: IPAddress) -> bool:
    """Whether ``ip`` is a public unicast address (see the module docstring)."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner is not None:
            return is_public(inner)
        if ip.scope_id:
            return False
    if ip in _CGNAT if isinstance(ip, ipaddress.IPv4Address) else False:
        return False
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_unspecified
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
    )


def is_local_only(ip: IPAddress) -> bool:
    """Loopback, link-local or unspecified: never reachable, even for allowed hosts."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner is not None:
            return is_local_only(inner)
    return bool(ip.is_loopback or ip.is_link_local or ip.is_unspecified)


def check_host_name(host: str, allow_private_hosts: frozenset[str]) -> str:
    """The normalised host (lower case, no trailing dot, brackets removed from IPv6
    literals), or ``Refused``."""
    raw = host.strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1]
    try:
        ipaddress.ip_address(raw.split("%", 1)[0])
    except ValueError:
        pass
    else:
        if "%" in raw:
            raise Refused(403, "scoped address")
        return raw.lower()
    name = raw.lower().rstrip(".")
    if not name or len(name) > 253 or not _HOST_RE.match(name):
        raise Refused(400, "invalid host name")
    if name in allow_private_hosts:
        return name
    if name in _BLOCKED_NAMES or name.endswith(_BLOCKED_SUFFIXES):
        raise Refused(403, "local host name")
    if "." not in name:
        raise Refused(403, "single-label host name")
    return name


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass
class Policy:
    allowed_ports: frozenset[int] = DEFAULT_PORTS
    allow_private_hosts: frozenset[str] = frozenset()

    async def vet(self, host: str, port: int, resolve: Resolver) -> tuple[str, str]:
        """``(host, address)`` to connect to, or ``Refused``."""
        if port not in self.allowed_ports:
            raise Refused(403, f"port {port} not allowed")
        name = check_host_name(host, self.allow_private_hosts)
        try:
            literal: IPAddress | None = ipaddress.ip_address(name)
        except ValueError:
            literal = None
        if literal is not None:
            addresses = [str(literal)]
        else:
            try:
                addresses = await resolve(name, port)
            except (OSError, UnicodeError):
                raise Refused(502, "name does not resolve") from None
        if not addresses:
            raise Refused(502, "name does not resolve")
        parsed = []
        for a in addresses:
            try:
                parsed.append(ipaddress.ip_address(a.split("%", 1)[0]))
            except ValueError:
                raise Refused(502, "resolver returned an invalid address") from None
            if "%" in a:
                raise Refused(403, "scoped address")
        private_ok = literal is None and name in self.allow_private_hosts
        for ip in parsed:
            if is_local_only(ip):
                raise Refused(403, "destination is local to the host")
            if not is_public(ip) and not private_ok:
                raise Refused(403, "destination is not a public address")
        return name, str(parsed[0])


@dataclass
class ProxyStats:
    allowed: int = 0
    refused: int = 0


@dataclass
class EgressProxy:
    policy: Policy = field(default_factory=Policy)
    resolve: Resolver = system_resolver
    connect_timeout_s: float = 10.0
    head_timeout_s: float = 15.0
    idle_timeout_s: float = 120.0
    max_connections: int = 256
    log: Callable[[dict[str, object]], None] = lambda record: print(json.dumps(record), flush=True)
    stats: ProxyStats = field(default_factory=ProxyStats)

    def __post_init__(self) -> None:
        self._slots = asyncio.Semaphore(self.max_connections)

    async def serve(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEAD_BYTES)

    # -- one client connection ---------------------------------------------------------

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._slots.locked():
            await self._reply(writer, 503, "too many connections")
            return
        async with self._slots:
            try:
                await self._handle(reader, writer)
            except Exception as exc:  # never let one client crash the server
                self.log({"event": "error", "error": type(exc).__name__})
            finally:
                writer.close()
                with contextlib.suppress(Exception):
                    await writer.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.head_timeout_s)
        except (asyncio.LimitOverrunError, ValueError):
            await self._reply(writer, 431, "request head too large")
            return
        except (asyncio.IncompleteReadError, TimeoutError, ConnectionError):
            return
        try:
            method, target, version, headers = parse_head(head)
            if method == "CONNECT":
                host, port = split_authority(target, default_port=None)
                rest = b""
            else:
                host, port, path = split_absolute_url(target)
                rest = rebuild_head(method, path, version, headers, host, port)
            name, address = await self.policy.vet(host, port, self.resolve)
        except Refused as refused:
            self.stats.refused += 1
            self.log({"event": "refused", "target": _safe_target(head), "reason": refused.reason})
            await self._reply(writer, refused.status, refused.reason)
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(address, port), self.connect_timeout_s
            )
        except (OSError, TimeoutError):
            self.stats.refused += 1
            self.log({"event": "unreachable", "host": name, "port": port})
            await self._reply(writer, 502, "upstream unreachable")
            return
        self.stats.allowed += 1
        self.log({"event": "allowed", "host": name, "port": port, "method": method})
        try:
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                up_writer.write(rest)
                await up_writer.drain()
            await self._pipe(reader, writer, up_reader, up_writer)
        finally:
            up_writer.close()
            with contextlib.suppress(Exception):
                await up_writer.wait_closed()

    async def _pipe(
        self,
        c_reader: asyncio.StreamReader,
        c_writer: asyncio.StreamWriter,
        u_reader: asyncio.StreamReader,
        u_writer: asyncio.StreamWriter,
    ) -> None:
        last = [time.monotonic()]

        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(64 * 1024):
                    last[0] = time.monotonic()
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    if dst.can_write_eof():
                        dst.write_eof()

        tasks = [
            asyncio.create_task(copy(c_reader, u_writer)),
            asyncio.create_task(copy(u_reader, c_writer)),
        ]
        try:
            while not all(t.done() for t in tasks):
                await asyncio.wait(tasks, timeout=1.0)
                if time.monotonic() - last[0] > self.idle_timeout_s:
                    break
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    @staticmethod
    async def _reply(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        body = f"egress proxy: {reason}\n".encode()
        phrase = {
            400: "Bad Request",
            403: "Forbidden",
            431: "Request Header Fields Too Large",
            502: "Bad Gateway",
            503: "Service Unavailable",
        }.get(status, "Error")
        writer.write(
            f"HTTP/1.1 {status} {phrase}\r\nContent-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        with contextlib.suppress(Exception):
            await writer.drain()


# -- request parsing ------------------------------------------------------------------


def parse_head(head: bytes) -> tuple[str, str, str, list[tuple[str, str]]]:
    try:
        text = head.decode("latin-1")
    except UnicodeDecodeError:  # pragma: no cover - latin-1 decodes everything
        raise Refused(400, "bad request") from None
    lines = text.split("\r\n")
    parts = lines[0].split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise Refused(400, "bad request line")
    method, target, version = parts
    if not re.fullmatch(r"[A-Z]{3,10}", method):
        raise Refused(400, "bad method")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if ":" not in line or line[0] in " \t":
            raise Refused(400, "bad header")
        name, value = line.split(":", 1)
        headers.append((name.strip(), value.strip()))
    return method, target, version, headers


def split_authority(authority: str, *, default_port: int | None) -> tuple[str, int]:
    if "@" in authority or "/" in authority:
        raise Refused(400, "bad authority")
    if authority.startswith("["):
        end = authority.find("]")
        if end < 0:
            raise Refused(400, "bad authority")
        host, rest = authority[: end + 1], authority[end + 1 :]
        port_text = rest[1:] if rest.startswith(":") else ("" if not rest else None)
    else:
        host, _, port_text = authority.partition(":")
    if port_text is None:
        raise Refused(400, "bad authority")
    if not port_text:
        if default_port is None:
            raise Refused(400, "port required")
        return host, default_port
    if not port_text.isdigit() or not 0 < int(port_text) < 65536:
        raise Refused(400, "bad port")
    return host, int(port_text)


def split_absolute_url(target: str) -> tuple[str, int, str]:
    if not target.lower().startswith("http://"):
        raise Refused(400, "only http:// URLs and CONNECT are proxied")
    rest = target[7:]
    slash = rest.find("/")
    authority, path = (rest, "/") if slash < 0 else (rest[:slash], rest[slash:])
    host, port = split_authority(authority, default_port=80)
    return host, port, path


def rebuild_head(
    method: str, path: str, version: str, headers: Sequence[tuple[str, str]], host: str, port: int
) -> bytes:
    """Origin-form request for the upstream: proxy and hop-by-hop headers dropped, the
    ``Host`` header set from the URL, and ``Connection: close`` (one request per
    connection, so a later request on the same client connection cannot go to a
    different, unvetted host)."""
    kept = [(n, v) for n, v in headers if n.lower() not in _HOP_HEADERS and n.lower() != "host"]
    host_header = host if port == 80 else f"{host}:{port}"
    lines = [f"{method} {path} {version}", f"Host: {host_header}"]
    lines += [f"{n}: {v}" for n, v in kept]
    lines.append("Connection: close")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


def _safe_target(head: bytes) -> str:
    """Host and port of the request for logs (never paths or queries)."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
    parts = line.split(" ")
    target = parts[1] if len(parts) > 1 else ""
    if target.lower().startswith("http://"):
        target = target[7:].split("/", 1)[0]
    return target[:270]


# -- entry point ----------------------------------------------------------------------


def _csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def build(argv: Iterable[str] | None = None) -> tuple[EgressProxy, str, int]:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--listen", default=os.environ.get("EGRESS_LISTEN", "0.0.0.0:3128"))
    parser.add_argument("--ports", default=os.environ.get("EGRESS_ALLOWED_PORTS", "80,443"))
    parser.add_argument(
        "--allow-private-hosts", default=os.environ.get("EGRESS_ALLOW_PRIVATE_HOSTS", "")
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    host, _, port = str(args.listen).rpartition(":")
    policy = Policy(
        allowed_ports=frozenset(int(p) for p in _csv(args.ports)),
        allow_private_hosts=frozenset(h.lower() for h in _csv(args.allow_private_hosts)),
    )
    return EgressProxy(policy=policy), host or "0.0.0.0", int(port)  # noqa: S104


async def _main() -> None:
    proxy, host, port = build()
    server = await proxy.serve(host, port)
    proxy.log(
        {
            "event": "listening",
            "host": host,
            "port": port,
            "ports": sorted(proxy.policy.allowed_ports),
        }
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
