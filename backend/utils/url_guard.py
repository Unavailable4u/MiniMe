"""
utils/url_guard.py — Patches SX-04 and SX-05 (SSRF guard: decide whether a URL may be fetched, and fetch it safely).

WHAT THIS FILE DOES
-------------------
One place that answers "may the server fetch this URL?" (SX-04) and, when it may, fetches it so that the address
that was checked is the address that is connected to (SX-05).  The checks make NO network call themselves: DNS goes
through an injectable resolver (``GuardPolicy.resolver``), so tests use a fake one.  Nothing calls this module yet;
the later patches wire it in (SX-06/07/08 use it).

    check_url_syntax(url)       text rules only: scheme, userinfo, port, host spelling; IP-literal hosts are
                                also judged against the address blocklist.  No DNS.
    validate_url(url)           check_url_syntax, then resolve the name and require EVERY answer to pass the
                                blocklist (one bad answer rejects the whole name).
    validate_youtube_url(url)   check_url_syntax, then the host must be a YouTube host.  No DNS (yt-dlp does its
                                own networking).
    blocked_address(ip)         True when the server must never connect to ``ip``.
    safe_stream(url, ...)       context manager: fetch ``url`` and give a response to read in chunks.
    safe_get(url, ...)          fetch ``url`` and return ``(final_url, status, headers, body)``.

The address blocklist is a PORT of ``_blocked`` and its tables in m2c/src/m2c/sandbox/netproxy.py (copied, not
imported: m2c is a separate package), without that file's test-only loopback flag: tests replace
``GuardPolicy.blocked`` instead.

HOW THE SAFE FETCH WORKS (SX-05)
--------------------------------
It uses ``urllib3`` directly, one connection pool per hop, never ``requests``/``httpx`` redirect handling:

  1. every hop (the first request and each redirect) goes through ``validate_url`` first, for a readable error;
  2. the pool is created with the host NAME (so the ``Host`` header, SNI and certificate checks use the name), but
     its connection class overrides ``_new_conn`` so that name resolution, the blocklist check and the socket
     connect to the checked IP all happen in the same step.  Nothing can change the answer in between (DNS
     rebinding);
  3. HTTPS always verifies the certificate against ``certifi`` (or ``GuardPolicy.ca_certs``);
  4. redirects are followed by hand, at most ``max_redirects`` of them; environment proxy variables are ignored;
  5. the body is read with ``read1`` and counted AFTER decompression against ``max_bytes``; a total-time deadline
     is checked after every read (a single read can overshoot it by at most ``timeout``);
  6. the content type is checked before any body byte is read.

KNOWN LIMIT of the total deadline: it is checked between reads and between hops.  A server that drips the response
HEADERS, or a compressed body whose first bytes decode to nothing, can keep one call busy for longer than
``total_timeout`` (every single socket wait is still bounded by ``timeout``).  A hard limit would need a watchdog
that shuts the socket down from another thread; that is left for a later patch.

``_new_conn`` and ``ConnectionCls`` are not a public ``urllib3`` API.  tests/unit/test_url_guard_fetch.py is the
tripwire when ``urllib3`` is bumped (patch SX-13).

ERRORS
------
All of them are ``ValueError`` subclasses, so a caller keeps a single-exception contract.  Messages are fixed texts
that are safe to show a user: they never echo the URL, a host name or a resolved address.

    UnsafeURL        the URL (or a redirect target) must not be fetched.
    FetchError       connection, TLS, HTTP status, content type or redirect-count failure.
    ResponseTooLarge a FetchError: the body is over the byte cap.
    FetchTimeout     a FetchError that is ALSO a ``requests.exceptions.Timeout``, because component_spec_lookup
                     re-raises exactly that type to drive its retry loop.

Place this file at: backend/utils/url_guard.py
"""
from __future__ import annotations

import contextlib
import http.client
import ipaddress
import re
import socket
import ssl
import time
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping, Sequence
from urllib.parse import quote, urljoin

import certifi
import requests
import urllib3

__all__ = [
    "DEFAULT_POLICY", "FetchError", "FetchTimeout", "GuardPolicy", "ParsedURL", "ResponseTooLarge", "SafeResponse",
    "UnsafeURL", "YOUTUBE_HOSTS", "blocked_address", "check_url_syntax", "default_resolver", "safe_get",
    "safe_stream", "validate_url", "validate_youtube_url",
]

MAX_URL_LENGTH = 2048
YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be",
    "youtube-nocookie.com", "www.youtube-nocookie.com",
})


class UnsafeURL(ValueError):
    """The URL must not be fetched.  The message is fixed text and never contains the URL, a host or an address."""


# ==========================================================================
# which addresses are never reachable (ported from m2c netproxy.py)
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


def _blocked(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(addr, ipaddress.IPv4Address):
        return any(addr in net for net in _V4_BLOCKED)
    mapped = addr.ipv4_mapped
    if mapped is not None:                         # ::ffff:a.b.c.d is judged as a.b.c.d
        return _blocked(mapped)
    if addr in _NAT64:                             # 64:ff9b::a.b.c.d is judged as a.b.c.d
        return _blocked(ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF))
    if addr not in _V6_GLOBAL:                     # ::/8 (unspecified, loopback, v4-compatible), fc00::/7, fe80::/10,
        return True                                # ff00::/8 ... all of it
    if addr.sixtofour is not None:                 # 2002:a.b.c.d::/48 is judged as a.b.c.d
        return _blocked(addr.sixtofour)
    return any(addr in net for net in _V6_BLOCKED)


def blocked_address(ip: object) -> bool:
    """True when the server must never connect to ``ip``.  Anything that is not a plain IP address is blocked."""
    if not isinstance(ip, str) or "%" in ip:       # a %scope is never accepted
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return _blocked(addr)


# ==========================================================================
# the policy (everything a test may want to replace)
# ==========================================================================
Resolver = Callable[[str, int], Sequence[str]]


def default_resolver(host: str, port: int) -> list[str]:
    """The host's resolver: ``socket.getaddrinfo``, reduced to the list of address strings."""
    return [str(info[4][0]) for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


@dataclass(frozen=True)
class GuardPolicy:
    """``ports=None`` means any port (the MCP rule in SX-24).  ``ca_certs`` is a path to a CA bundle for the safe
    fetch's HTTPS checks; ``None`` means the ``certifi`` bundle."""

    resolver: Resolver = default_resolver
    blocked: Callable[[str], bool] = blocked_address
    ports: frozenset[int] | None = frozenset({80, 443})
    ca_certs: str | None = None


DEFAULT_POLICY = GuardPolicy()


# ==========================================================================
# syntax checks (no network)
# ==========================================================================
@dataclass(frozen=True)
class ParsedURL:
    """What a URL that passed ``check_url_syntax`` is made of.  ``host`` has no brackets; ``target`` is never empty."""

    scheme: str
    host: str
    port: int
    target: str
    is_ip: bool


_SCHEME = re.compile(r"([A-Za-z][A-Za-z0-9+.\-]*):(.*)", re.DOTALL)
_PORT = re.compile(r"[0-9]{1,5}")
_LABEL = re.compile(r"[a-z0-9_](?:[a-z0-9_\-]{0,61}[a-z0-9_])?")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_PATH_SAFE = "/%:@!$&'()*+,;=-._~"
_QUERY_SAFE = _PATH_SAFE + "?"


def _reject(why: str) -> UnsafeURL:
    return UnsafeURL(why)


def _split_authority(authority: str) -> tuple[str, str | None, bool]:
    """(host text, port text or None, is_bracketed) from the part between ``//`` and the first ``/ ? #``."""
    if authority.startswith("["):
        end = authority.find("]")
        if end == -1:
            raise _reject("URL host is not valid")
        rest = authority[end + 1:]
        if rest and not rest.startswith(":"):
            raise _reject("URL host is not valid")
        return authority[1:end], (rest[1:] if rest else None), True
    host, colon, port = authority.partition(":")
    if ":" in port:                                # an IPv6 address without brackets, or two ports
        raise _reject("URL host is not valid")
    return host, (port if colon else None), False


def _judge_host(raw: str, bracketed: bool, policy: GuardPolicy) -> tuple[str, bool]:
    """Normalise and judge a host.  Returns (host, is_ip) or raises UnsafeURL."""
    if "%" in raw:
        raise _reject("URL host is not valid")
    if bracketed:                                  # must be an IPv6 address
        try:
            ip6 = ipaddress.IPv6Address(raw)
        except ValueError:
            raise _reject("URL host is not valid") from None
        host = str(ip6)
        if policy.blocked(host):
            raise _reject("URL destination is not allowed")
        return host, True
    try:                                           # IDNA first: look-alike dots and full-width digits become ASCII
        host = raw.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise _reject("URL host is not valid") from None
    if host.endswith("."):
        host = host[:-1]
    if not host:
        raise _reject("URL host is not valid")
    if _NUMERIC_LABEL.fullmatch(host.rsplit(".", 1)[-1]):   # looks like a number: only a strict dotted quad is allowed
        try:
            ip4 = ipaddress.IPv4Address(host)
        except ValueError:
            raise _reject("URL host is ambiguous") from None
        if policy.blocked(str(ip4)):
            raise _reject("URL destination is not allowed")
        return str(ip4), True
    if len(host) > 253 or not all(_LABEL.fullmatch(label) for label in host.split(".")):
        raise _reject("URL host is not valid")
    if host == "localhost" or host.endswith(".localhost"):
        raise _reject("URL destination is not allowed")
    return host, False


def check_url_syntax(url: object, policy: GuardPolicy = DEFAULT_POLICY) -> ParsedURL:
    """Text rules only; never touches the network.  Raises UnsafeURL, returns a ParsedURL."""
    if not isinstance(url, str) or not url:
        raise _reject("URL must be non-empty text")
    if len(url) > MAX_URL_LENGTH:
        raise _reject("URL is too long")
    if "\\" in url or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in url):
        raise _reject("URL contains characters that are not allowed")
    try:
        url.encode("utf-8")
    except UnicodeEncodeError:
        raise _reject("URL contains characters that are not allowed") from None
    match = _SCHEME.fullmatch(url)
    if match is None or match.group(1).lower() not in ("http", "https"):
        raise _reject("URL must start with http:// or https://")
    scheme, rest = match.group(1).lower(), match.group(2)
    if not rest.startswith("//"):
        raise _reject("URL must start with http:// or https://")
    rest = rest[2:].split("#", 1)[0]               # the fragment is never sent
    cut = min((i for i in (rest.find("/"), rest.find("?")) if i != -1), default=len(rest))
    authority, remainder = rest[:cut], rest[cut:]
    if not authority:
        raise _reject("URL has no host")
    if "@" in authority:
        raise _reject("URL must not contain a user name or password")
    host_text, port_text, bracketed = _split_authority(authority)
    if port_text is None:
        port = 443 if scheme == "https" else 80
    elif _PORT.fullmatch(port_text) and 0 < int(port_text) <= 65535:
        port = int(port_text)
    else:
        raise _reject("URL port is not valid")
    if policy.ports is not None and port not in policy.ports:
        raise _reject("URL port is not allowed")
    host, is_ip = _judge_host(host_text, bracketed, policy)
    path, question, query = remainder.partition("?")
    target = quote(path or "/", safe=_PATH_SAFE) + (("?" + quote(query, safe=_QUERY_SAFE)) if question else "")
    return ParsedURL(scheme=scheme, host=host, port=port, target=target, is_ip=is_ip)


# ==========================================================================
# the two public checks
# ==========================================================================
def validate_url(url: object, policy: GuardPolicy = DEFAULT_POLICY) -> ParsedURL:
    """May the server fetch ``url``?  Syntax first, then resolve the name; EVERY answer must pass the blocklist."""
    parsed = check_url_syntax(url, policy)
    if parsed.is_ip:                               # an IP literal was already judged by the syntax check
        return parsed
    try:
        addresses = list(policy.resolver(parsed.host, parsed.port))
    except Exception:
        raise _reject("URL host could not be resolved") from None
    if not addresses:
        raise _reject("URL host could not be resolved")
    if any(policy.blocked(address) for address in addresses):
        raise _reject("URL destination is not allowed")
    return parsed


def validate_youtube_url(url: object, policy: GuardPolicy = DEFAULT_POLICY) -> str:
    """Syntax rules, then the host must be a YouTube host.  No DNS.  Returns ``url`` unchanged."""
    parsed = check_url_syntax(url, policy)
    if parsed.host not in YOUTUBE_HOSTS:
        raise _reject("URL is not a YouTube address")
    return url  # type: ignore[return-value]  # check_url_syntax proved it is a str


# ==========================================================================
# the safe fetch (patch SX-05)
# ==========================================================================
class FetchError(ValueError):
    """The fetch failed: connection, TLS, HTTP status, content type or redirect count.  Fixed text, never the URL."""


class ResponseTooLarge(FetchError):
    """The body is larger than the byte cap."""


class FetchTimeout(FetchError, requests.exceptions.Timeout):
    """A time limit ran out.  Also a ``requests`` Timeout: component_spec_lookup re-raises exactly that type to
    drive its retry loop, so this one must keep working there."""

    def __init__(self, message: str = "The request took too long") -> None:
        # Both bases define __init__; ValueError's comes first in the MRO and would skip the one that gives a
        # requests exception its ``response`` and ``request`` attributes.
        requests.exceptions.Timeout.__init__(self, message)


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_CHUNK = 64 * 1024
_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; MiniMe/1.0)",
    "Accept": "*/*",
    "Accept-Encoding": "gzip, deflate",      # only what urllib3 decodes without extra packages
}
_CREDENTIAL_HEADERS = frozenset({"authorization", "cookie", "proxy-authorization"})
_CONTENT_LENGTH = re.compile(r"[0-9]{1,18}")
_UNSET = object()


# --------------------------------------------------------------------------
# the connection: resolve + check + connect in ONE step
# --------------------------------------------------------------------------
def _connect_checked(conn: "_GuardMixin") -> socket.socket:
    """Resolve the connection's host name, require EVERY answer to pass the blocklist, then connect to one of the
    checked addresses.  The name is looked up here, at connect time, so an answer that changed after the early
    ``validate_url`` of this hop (DNS rebinding) is judged too."""
    policy, deadline = conn._guard_policy, conn._guard_deadline
    host = conn.host
    try:
        ipaddress.ip_address(host)
        addresses = [host]                            # an IP literal needs no lookup (it was judged by the syntax check)
    except ValueError:
        try:
            addresses = [str(a) for a in policy.resolver(host, conn.port)]
        except Exception:
            raise UnsafeURL("URL host could not be resolved") from None
    if not addresses or any(policy.blocked(address) for address in addresses):
        raise UnsafeURL("URL destination is not allowed")
    timed_out = False
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FetchTimeout()
        sock = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.settimeout(min(float(conn.timeout), remaining))
            for option in conn.socket_options or ():
                sock.setsockopt(*option)
            if conn.source_address:
                sock.bind(conn.source_address)
            sock.connect((address, conn.port))
            return sock
        except OSError as exc:
            sock.close()
            timed_out = isinstance(exc, TimeoutError)
    if timed_out:
        raise FetchTimeout("The server did not answer in time")
    raise FetchError("Could not connect to the server")


class _GuardMixin:
    """Gives a urllib3 connection the policy and the deadline, and replaces its ``_new_conn`` (not a public urllib3
    API; see the module docstring)."""

    def __init__(self, *args, guard_policy: GuardPolicy, guard_deadline: float, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._guard_policy = guard_policy
        self._guard_deadline = guard_deadline

    def _new_conn(self) -> socket.socket:
        return _connect_checked(self)


class _GuardedHTTPConnection(_GuardMixin, urllib3.connection.HTTPConnection):
    pass


class _GuardedHTTPSConnection(_GuardMixin, urllib3.connection.HTTPSConnection):
    pass


class _GuardedHTTPPool(urllib3.HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(urllib3.HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _request_headers(extra: Mapping[str, str] | None) -> dict[str, str]:
    """The default headers, overridden (case-insensitively) by the caller's.  Refuses what could break the request."""
    merged = {name.lower(): (name, value) for name, value in _DEFAULT_HEADERS.items()}
    for name, value in (extra or {}).items():
        if not isinstance(name, str) or not isinstance(value, str) or not name or name.lower() == "host":
            raise ValueError("invalid request header")
        if any(ch in name + value for ch in "\r\n\0"):
            raise ValueError("invalid request header")
        merged[name.lower()] = (name, value)
    return dict(merged.values())


def _without_credentials(headers: Mapping[str, str]) -> dict[str, str]:
    return {name: value for name, value in headers.items() if name.lower() not in _CREDENTIAL_HEADERS}


def _translate(exc: BaseException) -> Exception | None:
    """Our error for an error raised by urllib3 / ssl / socket, or ``None`` when it is not one of those (a bug:
    let it surface unchanged)."""
    if isinstance(exc, (UnsafeURL, FetchError)):
        return exc
    if isinstance(exc, urllib3.exceptions.MaxRetryError) and exc.reason is not None:
        return _translate(exc.reason)
    if isinstance(exc, (urllib3.exceptions.TimeoutError, TimeoutError)):
        return FetchTimeout("The server did not answer in time")
    if isinstance(exc, (urllib3.exceptions.SSLError, ssl.SSLError, ssl.CertificateError)):
        return FetchError("The secure connection to the server could not be verified")
    if isinstance(exc, (urllib3.exceptions.HTTPError, http.client.HTTPException, OSError)):
        return FetchError("The connection to the server failed")
    return None


def _close(pool: urllib3.HTTPConnectionPool, raw: urllib3.BaseHTTPResponse | None) -> None:
    """Close the response (its connection may hold unread body) and the pool.  Never raises."""
    if raw is not None:
        with contextlib.suppress(Exception):
            raw.close()
    with contextlib.suppress(Exception):
        pool.close()


def _open_hop(parsed: ParsedURL, headers: Mapping[str, str], policy: GuardPolicy, timeout: float,
              deadline: float) -> tuple[urllib3.HTTPConnectionPool, urllib3.BaseHTTPResponse]:
    """One request, GET, no redirect handling, no retries, body NOT read.  The pool is made for the host NAME; the
    connection class connects to the checked address."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchTimeout()
    step = max(0.001, min(float(timeout), remaining))
    options = dict(timeout=urllib3.Timeout(connect=step, read=step), guard_policy=policy, guard_deadline=deadline)
    if parsed.scheme == "https":                      # certificates are ALWAYS verified
        pool: urllib3.HTTPConnectionPool = _GuardedHTTPSPool(
            parsed.host, parsed.port, cert_reqs="CERT_REQUIRED", ca_certs=policy.ca_certs or certifi.where(),
            **options)
    else:
        pool = _GuardedHTTPPool(parsed.host, parsed.port, **options)
    try:
        raw = pool.urlopen("GET", parsed.target, headers=dict(headers), redirect=False, retries=False,
                           assert_same_host=False, preload_content=False)
    except Exception as exc:
        _close(pool, None)
        ours = _translate(exc)
        if ours is None or ours is exc:
            raise
        raise ours from exc
    return pool, raw


# --------------------------------------------------------------------------
# the response and the two public functions
# --------------------------------------------------------------------------
class SafeResponse:
    """What ``safe_stream`` yields.  ``url`` is the final URL (after redirects), ``headers`` is case-insensitive.
    ``max_bytes`` of ``iter_bytes`` / ``read`` overrides the cap given to ``safe_stream``; ``None`` means NO cap (the
    caller must then cap by other means).  The cap counts bytes AFTER decompression."""

    def __init__(self, url: str, raw: urllib3.BaseHTTPResponse, max_bytes: int | None, deadline: float) -> None:
        self.url = url
        self.status: int = raw.status
        self.headers = raw.headers
        self._raw = raw
        self._max_bytes = max_bytes
        self._deadline = deadline

    def iter_bytes(self, max_bytes: int | None | object = _UNSET) -> Iterator[bytes]:
        cap = self._max_bytes if max_bytes is _UNSET else max_bytes
        total = 0
        try:
            while True:
                # read1 returns what ONE socket read delivered; read(amt) and stream(amt) wait until ``amt`` bytes
                # have arrived, so a server dripping one byte at a time would never let the deadline be checked.
                chunk = self._raw.read1(_CHUNK, decode_content=True)
                if not chunk:
                    return
                if time.monotonic() > self._deadline:
                    raise FetchTimeout()
                total += len(chunk)
                if cap is not None and total > cap:   # type: ignore[operator]
                    raise ResponseTooLarge("The response is too large")
                yield chunk
        except Exception as exc:
            ours = _translate(exc)
            if ours is None or ours is exc:
                raise
            raise ours from exc

    def read(self, max_bytes: int | None | object = _UNSET) -> bytes:
        return b"".join(self.iter_bytes(max_bytes))


@contextlib.contextmanager
def safe_stream(
    url: str,
    *,
    max_bytes: int | None = None,
    max_redirects: int = 4,
    timeout: float = 10.0,
    total_timeout: float = 30.0,
    allowed_content_types: Sequence[str] | None = None,
    headers: Mapping[str, str] | None = None,
    policy: GuardPolicy = DEFAULT_POLICY,
) -> Iterator[SafeResponse]:
    """Fetch ``url`` (GET) and yield a ``SafeResponse``; the connection is closed when the ``with`` block ends.

    ``timeout`` limits every connect / read; ``total_timeout`` limits the whole call, checked between chunks (one
    read can overshoot it by at most ``timeout``).  ``max_bytes`` is the default body cap (``None``: no cap) and a
    ``Content-Length`` over it is refused before any body byte is read.  A content type outside
    ``allowed_content_types`` is refused before the body is read (a missing header is allowed).  Credential headers
    (``Authorization``, ``Cookie``, ``Proxy-Authorization``) are dropped when a redirect changes scheme, host or
    port.  Raises UnsafeURL, FetchError, ResponseTooLarge or FetchTimeout (all ``ValueError``)."""
    deadline = time.monotonic() + total_timeout
    allowed = frozenset(t.strip().lower() for t in allowed_content_types) if allowed_content_types else None
    request_headers = _request_headers(headers)
    current, redirects = url, 0
    pool = raw = None
    try:
        while True:
            parsed = validate_url(current, policy)     # early check, for a readable error
            pool, raw = _open_hop(parsed, request_headers, policy, timeout, deadline)
            if raw.status not in _REDIRECT_STATUSES:
                break
            location = raw.headers.get("Location")
            _close(pool, raw)
            pool = raw = None
            if not location:
                raise FetchError("The server sent an invalid redirect")
            if redirects >= max_redirects:
                raise FetchError("The page redirected too many times")
            redirects += 1
            target = urljoin(current, location)
            if _origin(check_url_syntax(target, policy)) != _origin(parsed):
                request_headers = _without_credentials(request_headers)
            current = target
        if not 200 <= raw.status < 300:
            raise FetchError("The server did not return the page")
        if allowed is not None:
            content_type = raw.headers.get("Content-Type")
            if content_type is not None and content_type.split(";", 1)[0].strip().lower() not in allowed:
                raise FetchError("The content type is not allowed")
        length = raw.headers.get("Content-Length")
        if max_bytes is not None and length is not None and _CONTENT_LENGTH.fullmatch(length.strip()):
            if int(length) > max_bytes:
                raise ResponseTooLarge("The response is too large")
        yield SafeResponse(current, raw, max_bytes, deadline)
    finally:
        if pool is not None:
            _close(pool, raw)


def _origin(parsed: ParsedURL) -> tuple[str, str, int]:
    return parsed.scheme, parsed.host, parsed.port


def safe_get(url: str, *, max_bytes: int | None = 5_000_000, **options) -> tuple[str, int, Mapping[str, str], bytes]:
    """``safe_stream`` + read the whole body.  Returns ``(final_url, status, headers, body)``.  ``options`` are the
    other ``safe_stream`` keywords."""
    with safe_stream(url, max_bytes=max_bytes, **options) as response:
        body = response.read()
        return response.url, response.status, response.headers, body
