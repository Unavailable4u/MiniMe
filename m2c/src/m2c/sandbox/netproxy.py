"""
m2c/src/m2c/sandbox/netproxy.py -- Patch C11b: the host-side proxy that decides where a sandboxed command may connect.

THE DESIGN
----------
The sandbox keeps NO direct network (``--unshare-net`` stays).  On the host an asyncio proxy listens on a UNIX socket
inside a private temporary directory (mode 0700, owned by the user).  That directory is bind-mounted READ-ONLY into
the sandbox together with ``bridge.py``; inside, the bridge relays 127.0.0.1:3128 to the socket.  A program that
ignores the proxy settings cannot connect at all: fail closed by construction.  Everything that matters is decided
HERE, on the host, where the command cannot reach the code or its memory.

WHAT THE PROXY DOES, per connection
-----------------------------------
1. Reads the request head (request line and headers up to the blank line) under a total deadline and size caps
   (slowloris and oversized-header protection).  It looks at the request line only; headers are skipped, never
   parsed, stored or logged, and the payload that follows is never looked at.
2. Accepts ``CONNECT host:port`` only.  Plain ``http://`` requests get a fixed 403 (cleartext is OFF by default;
   ``ProxyConfig.allow_cleartext`` turns it on, and then the proxy reads the host out of the request line, nothing
   else, and tunnels the rest blindly).  Every other method gets a fixed 405.
3. Asks the policy (netpolicy.py): allowlist, ports (443 only by default), IP literals, the person's prompt.
4. RESOLVES the name once, on the host, and connects to the resolved ADDRESS (pinned): the name is never resolved
   again, so DNS rebinding cannot swap the destination between the check and the connect.
5. REFUSES the whole request if ANY resolved address is loopback, private (RFC 1918), link-local (the cloud metadata
   addresses among them), carrier-grade NAT, unique-local, multicast, unspecified, reserved or documentation space,
   including IPv4-mapped / NAT64 / 6to4 forms of those -- EVEN when the name is on the allowlist.  Refusing the whole
   answer (not skipping the bad address) means a hostile DNS server cannot mix a private address into a public set.
6. Tries the addresses in order with a connect timeout, then relays bytes with an idle timeout.

Limits: connections overall and per host, head size, connect/idle/header timeouts.  Shutdown closes everything and
removes the socket directory.

HONEST LIMITS (also in the patch description)
---------------------------------------------
* A proxy controls WHERE a command connects, not WHAT it sends.  Data can still leave to a domain the person
  allowed (a code-hosting site, say).
* The proxy sees only the CONNECT host.  Domain fronting through an allowed CDN is not detectable.
* C15's redaction does not apply to bytes a command sends over the network.
* Tools that ignore HTTP(S)_PROXY / ALL_PROXY cannot connect, by design.  A command's own ``localhost`` test server
  is reached through the proxy too (NO_PROXY is empty) and the proxy refuses loopback.
* Names are resolved by the host's resolver; a hostile resolver is outside this threat model.

THE TEST-ONLY FLAG
------------------
``_test_allow_loopback`` lets the proxy connect to 127.0.0.0/8 and ::1 so that tests and the probe can use a local
fake upstream.  It is a keyword argument of the constructors in this file and of nothing else: no setting, no
environment variable and no ShellConfig field reaches it (tests assert that), and it relaxes loopback only.

Standard library only (asyncio).

Place this file at: m2c/src/m2c/sandbox/netproxy.py
"""
from __future__ import annotations

import asyncio
import atexit
import ipaddress
import logging
import os
import re
import shutil
import socket
import stat
import tempfile
import threading
import weakref
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from ..logging_setup import log_event
from . import bridge as _bridge
from .bridge import NetworkMount
from .netpolicy import ALLOW, NetworkPolicy, normalize_host

__all__ = [
    "NetworkError", "NetworkService", "ProxyConfig", "ProxyServer", "RESPONSES", "blocked_address", "clear_label",
    "close_network",
    "default_connector", "default_resolver", "make_private_dir", "make_service", "service_for",
]

logger = logging.getLogger("m2c.sandbox.netproxy")

Resolver = Callable[[str, int], Awaitable[Sequence[str]]]
Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


class NetworkError(Exception):
    """The proxy could not be started safely.  ``code`` is a short identifier; the text never holds a path."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# ==========================================================================
# which addresses are never reachable
# ==========================================================================
_V4_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8",          # "this network", unspecified
    "10.0.0.0/8",         # RFC 1918
    "100.64.0.0/10",      # carrier-grade NAT (also Alibaba's metadata 100.100.100.200)
    "127.0.0.0/8",        # loopback
    "169.254.0.0/16",     # link-local: AWS/GCP/Azure/DO metadata 169.254.169.254, ECS 169.254.170.2
    "172.16.0.0/12",      # RFC 1918
    "192.0.0.0/24",       # IETF protocol assignments (Oracle metadata 192.0.0.192)
    "192.0.2.0/24",       # documentation
    "192.88.99.0/24",     # 6to4 relay anycast (deprecated)
    "192.168.0.0/16",     # RFC 1918
    "198.18.0.0/15",      # benchmarking
    "198.51.100.0/24",    # documentation
    "203.0.113.0/24",     # documentation
    "224.0.0.0/4",        # multicast
    "240.0.0.0/4",        # reserved, and the broadcast address
    "168.63.129.16/32",   # Azure's virtual public address (wire server / metadata helper)
))
_V6_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "2001::/23",          # IETF protocol assignments, Teredo
    "2001:db8::/32",      # documentation
    "3fff::/20",          # documentation
    "fd00:ec2::/32",      # AWS instance metadata over IPv6 (inside unique-local space)
))
_V6_GLOBAL = ipaddress.ip_network("2000::/3")  # the only IPv6 space that is public unicast
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LOOPBACK_V4 = ipaddress.ip_network("127.0.0.0/8")


def _blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address, allow_loopback: bool) -> bool:
    if isinstance(addr, ipaddress.IPv4Address):
        if allow_loopback and addr in _LOOPBACK_V4:
            return False
        return any(addr in net for net in _V4_BLOCKED)
    mapped = addr.ipv4_mapped
    if mapped is not None:                         # ::ffff:a.b.c.d is judged as a.b.c.d
        return _blocked(mapped, allow_loopback)
    if addr == ipaddress.IPv6Address("::1"):
        return not allow_loopback
    if addr in _NAT64:                             # 64:ff9b::a.b.c.d is judged as a.b.c.d
        return _blocked(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF), allow_loopback)
    if addr not in _V6_GLOBAL:                     # ::/8 (unspecified, loopback, v4-compatible), fc00::/7, fe80::/10,
        return True                                # ff00::/8, 64:ff9b:1::/48, 100::/64 ... all of it
    if addr.sixtofour is not None:                 # 2002:a.b.c.d::/48 is judged as a.b.c.d
        return _blocked(addr.sixtofour, allow_loopback)
    return any(addr in net for net in _V6_BLOCKED)


def blocked_address(ip: object, *, allow_loopback: bool = False) -> bool:
    """True when the proxy must never connect to ``ip``.  Anything that is not an IP address is blocked."""
    if not isinstance(ip, str):
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return _blocked(addr, allow_loopback)


# ==========================================================================
# the fixed answers
# ==========================================================================
RESPONSES: dict[str, tuple[int, str, str]] = {
    "denied": (403, "Forbidden", "m2c network: access to this host is not allowed"),
    "destination": (403, "Forbidden", "m2c network: this destination is not reachable from the sandbox"),
    "cleartext": (403, "Forbidden", "m2c network: plain http is not allowed; use https"),
    "method": (405, "Method Not Allowed", "m2c network: only CONNECT is supported"),
    "bad": (400, "Bad Request", "m2c network: malformed request"),
    "too_large": (431, "Request Header Fields Too Large", "m2c network: request too large"),
    "timeout": (408, "Request Timeout", "m2c network: request timed out"),
    "busy": (503, "Service Unavailable", "m2c network: too many connections"),
    "unreachable": (502, "Bad Gateway", "m2c network: could not connect"),
}


def _response_bytes(key: str) -> bytes:
    code, reason, text = RESPONSES[key]
    body = (text + "\n").encode("ascii")
    head = (
        "HTTP/1.1 %d %s\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: %d\r\n"
        "Connection: close\r\nX-M2C-Proxy: refused\r\n\r\n" % (code, reason, len(body))
    ).encode("ascii")
    return head + body


_CONNECTED = b"HTTP/1.1 200 Connection Established\r\n\r\n"


class _Refuse(Exception):
    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


# ==========================================================================
# configuration, resolver, connector
# ==========================================================================
@dataclass(frozen=True)
class ProxyConfig:
    """The proxy's limits.  The ports live in the policy (``NetworkPolicy.ports``)."""

    allow_cleartext: bool = False
    max_request_line: int = 2048
    max_header_bytes: int = 8192
    header_timeout_s: float = 10.0       # the WHOLE request head must arrive in this time (slowloris)
    resolve_timeout_s: float = 8.0
    connect_timeout_s: float = 10.0
    idle_timeout_s: float = 300.0
    max_connections: int = 64
    max_per_host: int = 16
    max_addresses: int = 4               # resolved addresses tried per request
    max_resolved: int = 16               # more than this many answers is refused as hostile


async def default_resolver(host: str, port: int) -> list[str]:
    """The host's resolver (``getaddrinfo`` in a thread).  Addresses only; scope ids are dropped by the caller."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


async def default_connector(ip: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to an ADDRESS.  ``ip`` must already be an address: nothing here resolves a name."""
    ipaddress.ip_address(ip)                       # raises ValueError for a name: fail closed, never resolve
    return await asyncio.open_connection(ip, port)


_AUTHORITY = re.compile(r"(\[[0-9A-Fa-f:.]+\]|[^:\[\]/@?#\s]+):([0-9]{1,5})")
_HTTP_URL = re.compile(r"http://(\[[0-9A-Fa-f:.]+\]|[^:\[\]/@?#\s]+)(?::([0-9]{1,5}))?(?:[/?#].*)?", re.IGNORECASE)


def make_private_dir() -> str:
    """A new directory only this user can enter (0700), short enough for a unix socket path.  Raises NetworkError."""
    candidates = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isabs(runtime) and os.path.isdir(runtime):
        candidates.append(runtime)
    candidates.append(tempfile.gettempdir())
    candidates.append("/tmp")
    for base in candidates:
        try:
            path = tempfile.mkdtemp(prefix="m2c-net-", dir=base)
        except OSError:
            continue
        if len(os.path.join(path, _bridge.SOCKET_NAME)) > 100:   # sun_path is 108 bytes
            shutil.rmtree(path, ignore_errors=True)
            continue
        try:
            os.chmod(path, 0o700)
            st = os.lstat(path)
            if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o077 or (hasattr(os, "geteuid") and st.st_uid != os.geteuid()):
                raise OSError
        except OSError:
            shutil.rmtree(path, ignore_errors=True)
            continue
        return path
    raise NetworkError("no_private_dir")


# ==========================================================================
# the server
# ==========================================================================
class ProxyServer:
    """The proxy on one event loop.  ``await start()`` then connect to ``socket_path``; ``await aclose()`` stops it."""

    def __init__(
        self,
        policy: NetworkPolicy,
        *,
        config: ProxyConfig | None = None,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
        label: Callable[[], str] = lambda: "",
        directory: str | None = None,
        _test_allow_loopback: bool = False,
    ) -> None:
        self.policy = policy
        self.config = config if config is not None else ProxyConfig()
        self._resolver = resolver if resolver is not None else default_resolver
        self._connector = connector if connector is not None else default_connector
        self._label = label
        self._allow_loopback = bool(_test_allow_loopback)
        self._given_dir = directory
        self.directory: str | None = None
        self.socket_path: str | None = None
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._active = 0
        self._closing = False
        self._per_host: dict[str, int] = {}
        # counts only: never a host, path or payload
        self.stats: dict[str, int] = {"accepted": 0, "tunnels": 0, "refused": 0, "busy": 0}

    # -- lifecycle -------------------------------------------------------------------------------------------------
    async def start(self) -> None:
        directory = self._given_dir if self._given_dir is not None else make_private_dir()
        self.directory = directory
        try:
            script = os.path.join(directory, _bridge.SCRIPT_NAME)
            with open(script, "w", encoding="utf-8") as handle:
                handle.write(_bridge.bridge_source())
            os.chmod(script, 0o444)
            path = os.path.join(directory, _bridge.SOCKET_NAME)
            self._server = await asyncio.start_unix_server(self._handle, path=path, backlog=128)
            os.chmod(path, 0o600)
            self.socket_path = path
        except BaseException:
            shutil.rmtree(directory, ignore_errors=True)
            self.directory = None
            raise

    async def aclose(self) -> None:
        """Stop accepting, close every connection, remove the directory.  Safe to call twice."""
        self._closing = True                       # a connection accepted from now on is dropped at once (see _handle)
        server, self._server = self._server, None
        # Keep accepting for a moment so that a connection that is already queued is accepted and dropped (closing
        # the listener first would leave it waiting, unanswered, in the accept queue), then end what is running.
        await asyncio.sleep(0.05)
        for _ in range(3):
            tasks = list(self._tasks)
            if not tasks:
                break
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if server is not None:
            server.close()
        for writer in list(self._writers):
            self._close(writer)
        if server is not None:
            try:
                await asyncio.wait_for(server.wait_closed(), 5)
            except (TimeoutError, asyncio.CancelledError):
                pass
        directory, self.directory, self.socket_path = self.directory, None, None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    @staticmethod
    def _close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass

    # -- one connection --------------------------------------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        self._active += 1
        self.stats["accepted"] += 1
        host_key: str | None = None
        upstream: asyncio.StreamWriter | None = None
        try:
            if self._closing:
                return                              # shutting down: nothing new is served (the finally closes it)
            if self._active > self.config.max_connections:
                self.stats["busy"] += 1
                raise _Refuse("busy")
            head, extra = await self._read_head(reader)
            method, target = self._parse_request_line(head)
            cleartext = False
            if method == "CONNECT":
                host, port = self._parse_authority(target)
            else:
                match = _HTTP_URL.fullmatch(target) if target[:7].lower() == "http://" else None
                if match is None:
                    raise _Refuse("method")
                if not self.config.allow_cleartext:
                    raise _Refuse("cleartext")
                cleartext = True
                host, port = match.group(1), int(match.group(2) or 80)
                if not 1 <= port <= 65535:
                    raise _Refuse("bad")
            verdict = await self.policy.decide(host, port, self._label())
            if verdict.action != ALLOW:
                raise _Refuse("denied")
            name = normalize_host(host)
            if name is None:
                raise _Refuse("denied")
            host_key = name
            if self._per_host.get(host_key, 0) >= self.config.max_per_host:
                self.stats["busy"] += 1
                host_key = None
                raise _Refuse("busy")
            self._per_host[host_key] = self._per_host.get(host_key, 0) + 1
            up_reader, upstream = await self._open_upstream(name, port)
            self._writers.add(upstream)
            self.stats["tunnels"] += 1
            if cleartext:
                upstream.write(head)                   # the request, unchanged (absolute-form is valid for origins)
            else:
                writer.write(_CONNECTED)
                await writer.drain()
            if extra:
                upstream.write(extra)
            await self._splice(reader, writer, up_reader, upstream)
        except _Refuse as refusal:
            self.stats["refused"] += 1
            try:
                writer.write(_response_bytes(refusal.key))
                await asyncio.wait_for(writer.drain(), 2)
            except Exception:  # noqa: BLE001 - the client may already be gone
                pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one bad connection never takes the proxy down
            log_event(logger, logging.DEBUG, "net.proxy", check="connection", status="error", code="handler_error")
        finally:
            if host_key is not None:
                left = self._per_host.get(host_key, 1) - 1
                if left > 0:
                    self._per_host[host_key] = left
                else:
                    self._per_host.pop(host_key, None)
            self._active -= 1
            for w in (writer, upstream):
                if w is not None:
                    self._writers.discard(w)
                    self._close(w)
            if task is not None:
                self._tasks.discard(task)

    async def _read_head(self, reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
        cfg = self.config
        buf = bytearray()
        try:
            async with asyncio.timeout(cfg.header_timeout_s):
                while True:
                    end = buf.find(b"\r\n\r\n")
                    line_end = buf.find(b"\n")
                    if (line_end < 0 and len(buf) > cfg.max_request_line) or line_end > cfg.max_request_line:
                        raise _Refuse("too_large")
                    if end >= 0:
                        if end + 4 > cfg.max_header_bytes:
                            raise _Refuse("too_large")
                        return bytes(buf[: end + 4]), bytes(buf[end + 4:])
                    if len(buf) > cfg.max_header_bytes:
                        raise _Refuse("too_large")
                    chunk = await reader.read(4096)
                    if not chunk:
                        raise _Refuse("bad")
                    buf += chunk
        except TimeoutError:
            raise _Refuse("timeout") from None

    @staticmethod
    def _parse_request_line(head: bytes) -> tuple[str, str]:
        try:
            line = head.split(b"\r\n", 1)[0].decode("ascii")
        except UnicodeDecodeError:
            raise _Refuse("bad") from None
        parts = line.split(" ")
        if len(parts) != 3 or not all(parts) or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
            raise _Refuse("bad")
        if any(ord(c) < 33 or ord(c) > 126 for c in parts[0] + parts[1]):
            raise _Refuse("bad")
        return parts[0], parts[1]

    @staticmethod
    def _parse_authority(target: str) -> tuple[str, int]:
        match = _AUTHORITY.fullmatch(target)
        if match is None:
            raise _Refuse("bad")
        port = int(match.group(2))
        if not 1 <= port <= 65535:
            raise _Refuse("bad")
        return match.group(1), port

    async def _open_upstream(self, name: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Resolve ONCE, refuse unsafe answers, connect to the pinned addresses in order."""
        cfg = self.config
        literal = None
        try:
            literal = str(ipaddress.ip_address(name))
        except ValueError:
            pass
        if literal is not None:
            addresses = [literal]
        else:
            try:
                async with asyncio.timeout(cfg.resolve_timeout_s):
                    found = await self._resolver(name, port)
            except Exception:  # noqa: BLE001 - no answer is no connection
                raise _Refuse("unreachable") from None
            addresses = []
            for item in found:
                text = str(item).split("%", 1)[0]
                if text not in addresses:
                    addresses.append(text)
            if not addresses:
                raise _Refuse("unreachable")
            if len(addresses) > cfg.max_resolved:
                self.policy.note(name, "deny", "address")
                raise _Refuse("destination")
        if any(blocked_address(a, allow_loopback=self._allow_loopback) for a in addresses):
            self.policy.note(name, "deny", "address")
            raise _Refuse("destination")
        for ip in addresses[: cfg.max_addresses]:
            try:
                async with asyncio.timeout(cfg.connect_timeout_s):
                    return await self._connector(ip, port)
            except (OSError, TimeoutError, ValueError):
                continue
        raise _Refuse("unreachable")

    async def _splice(self, c_reader: asyncio.StreamReader, c_writer: asyncio.StreamWriter,
                      u_reader: asyncio.StreamReader, u_writer: asyncio.StreamWriter) -> None:
        loop = asyncio.get_running_loop()
        idle = self.config.idle_timeout_s
        last = [loop.time()]

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    try:
                        data = await asyncio.wait_for(src.read(65536), idle)
                    except TimeoutError:
                        if loop.time() - last[0] < idle:       # the other direction is busy: not idle
                            continue
                        raise
                    if not data:
                        break
                    last[0] = loop.time()
                    dst.write(data)
                    await dst.drain()
            finally:
                try:
                    if dst.can_write_eof():
                        dst.write_eof()
                except Exception:  # noqa: BLE001
                    pass

        tasks = [asyncio.ensure_future(pipe(c_reader, u_writer)), asyncio.ensure_future(pipe(u_reader, c_writer))]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            if pending:
                failed = any(t.exception() is not None for t in done if not t.cancelled())
                if failed:
                    for t in pending:
                        t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


# ==========================================================================
# the service: the proxy on its own thread, with its own loop
# ==========================================================================
class NetworkService:
    """A ``ProxyServer`` (and its policy) running on a background thread, for callers that are not async or live on a
    different loop (the shell tool runs in worker threads; the terminal has its own loop).  ``start`` returns when
    the socket is listening; ``close`` stops everything and removes the directory."""

    def __init__(
        self,
        policy: NetworkPolicy,
        *,
        config: ProxyConfig | None = None,
        resolver: Resolver | None = None,
        connector: Connector | None = None,
        _test_allow_loopback: bool = False,
    ) -> None:
        self.policy = policy
        self._label = ""
        self._server = ProxyServer(
            policy, config=config, resolver=resolver, connector=connector, label=lambda: self._label,
            _test_allow_loopback=_test_allow_loopback)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._closed = False
        self.key: object = None            # what the owner built it from; compared to decide whether to rebuild

    # -- lifecycle -------------------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="m2c-net-proxy", daemon=True)
        self._thread.start()
        if not self._ready.wait(15) or self._error is not None:
            self.close()
            raise NetworkError("start_failed")

    def _run(self) -> None:
        try:
            asyncio.run(self._main())
        except BaseException as exc:  # noqa: BLE001
            self._error = exc
        finally:
            self._ready.set()

    async def _main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            await self._server.start()
        except BaseException as exc:
            self._error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            await self._stop.wait()
        finally:
            await self._server.aclose()
            await self.policy.aclose()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._closed and self._error is None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        loop, stop = self._loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(15)

    # -- what callers use ------------------------------------------------------------------------------------------
    @property
    def directory(self) -> str | None:
        return self._server.directory

    @property
    def socket_path(self) -> str | None:
        return self._server.socket_path

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._server.stats)

    @property
    def unprompted_denials(self) -> int:
        return self.policy.unprompted_denials

    def set_label(self, label: str) -> None:
        """The short label of the command that is running (shown to the person in a prompt, never logged)."""
        self._label = label if isinstance(label, str) else ""

    def mount(self, python: str) -> NetworkMount:
        """What the wrapper mounts for a command.  Raises NetworkError when the proxy is not running."""
        if not self.alive or self.directory is None:
            raise NetworkError("not_running")
        return NetworkMount(directory=self.directory, python=python)

    def run(self, coro: Awaitable[Any], timeout: float = 10.0) -> Any:
        """Run a coroutine on the proxy's loop and return its result (tests and the probe start fake upstreams so)."""
        if self._loop is None:
            raise NetworkError("not_running")

        async def wrapper() -> Any:
            return await coro

        return asyncio.run_coroutine_threadsafe(wrapper(), self._loop).result(timeout)


# ==========================================================================
# one service per tool context
# ==========================================================================
_LOCK = threading.Lock()
_SERVICES: "weakref.WeakKeyDictionary[object, NetworkService]" = weakref.WeakKeyDictionary()


def make_service(
    *,
    allow: Sequence[str] = (),
    ports: Sequence[int] = (443,),
    prompt: Any = None,
    record: Callable[[dict[str, str]], None] | None = None,
    allow_cleartext: bool = False,
) -> NetworkService:
    """The production service: the user's saved allowlist, the caller's list and prompt, the default resolver and
    connector.  There is no parameter for the test-only loopback flag, on purpose."""
    from .netpolicy import AllowFile

    policy = NetworkPolicy(allow=allow, ports=ports, store=AllowFile(), prompt=prompt, record=record)
    return NetworkService(policy, config=ProxyConfig(allow_cleartext=allow_cleartext))


def service_for(owner: object, key: object, build: Callable[[], NetworkService]) -> NetworkService:
    """The running service of ``owner`` (a tool context), started on first use.  A service that has died, or that
    was built from a different ``key`` (settings), is replaced.  Raises NetworkError when it cannot start."""
    with _LOCK:
        service = _SERVICES.get(owner)
        if service is not None and service.alive and service.key == key:
            return service
        if service is not None:
            service.close()
            _SERVICES.pop(owner, None)
        service = build()
        service.key = key
        service.start()
        _SERVICES[owner] = service
        weakref.finalize(owner, service.close)
        return service


def clear_label(owner: object) -> None:
    """Forget the command label of the owner's proxy (the command ended).  Never raises, never starts anything."""
    with _LOCK:
        service = _SERVICES.get(owner)
    if service is not None:
        try:
            service.set_label("")
        except Exception:  # noqa: BLE001
            pass


def close_network(owner: object) -> None:
    """Stop the owner's proxy, if any (the session is ending).  Never raises."""
    with _LOCK:
        service = _SERVICES.pop(owner, None)
    if service is not None:
        try:
            service.close()
        except Exception:  # noqa: BLE001
            pass


def _close_all() -> None:
    with _LOCK:
        services = list(_SERVICES.values())
        _SERVICES.clear()
    for service in services:
        try:
            service.close()
        except Exception:  # noqa: BLE001
            pass


atexit.register(_close_all)
