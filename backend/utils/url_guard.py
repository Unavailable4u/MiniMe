"""
utils/url_guard.py — Patch SX-04 (SSRF guard, part 1: decide whether a URL may be fetched by the server).

WHAT THIS FILE DOES
-------------------
One place that answers "may the server fetch this URL?".  It makes NO network call itself: DNS goes through an
injectable resolver (``GuardPolicy.resolver``), so tests use a fake one.  Nothing calls this module yet; the
later patches wire it in (SX-05 adds the safe fetch, SX-06/07/08 use it).

    check_url_syntax(url)       text rules only: scheme, userinfo, port, host spelling; IP-literal hosts are
                                also judged against the address blocklist.  No DNS.
    validate_url(url)           check_url_syntax, then resolve the name and require EVERY answer to pass the
                                blocklist (one bad answer rejects the whole name).
    validate_youtube_url(url)   check_url_syntax, then the host must be a YouTube host.  No DNS (yt-dlp does its
                                own networking).
    blocked_address(ip)         True when the server must never connect to ``ip``.

The address blocklist is a PORT of ``_blocked`` and its tables in m2c/src/m2c/sandbox/netproxy.py (copied, not
imported: m2c is a separate package), without that file's test-only loopback flag: tests replace
``GuardPolicy.blocked`` instead.

ERRORS
------
``UnsafeURL`` (a ``ValueError``).  Its messages are fixed texts that are safe to show a user: they never echo the
URL, a host name or a resolved address.

Standard library only.

Place this file at: backend/utils/url_guard.py
"""
from __future__ import annotations

import ipaddress
import re
import socket
import unicodedata
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.parse import quote

__all__ = [
    "DEFAULT_POLICY", "GuardPolicy", "ParsedURL", "UnsafeURL", "YOUTUBE_HOSTS",
    "blocked_address", "check_url_syntax", "default_resolver", "validate_url", "validate_youtube_url",
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
    """``ports=None`` means any port (the MCP rule in SX-24).  ``ca_certs`` is for SX-05 (TLS); unused here."""

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
