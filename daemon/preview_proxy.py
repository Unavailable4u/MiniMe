"""
daemon/preview_proxy.py — W7.4 (Build Workbench plan): the daemon's
preview reverse proxy.

WHAT IT IS. A small HTTP reverse proxy the daemon starts on
127.0.0.1:<MINIME_PREVIEW_PORT> in front of the person's own dev server
(MINIME_PREVIEW_TARGET, e.g. http://localhost:5173). Pasting the
proxy's address into the workbench's "Dev server URL" box (W7.3) then
gives them, with no change to their app:

  1. An embeddable page. Dev servers that send `X-Frame-Options` or a
     CSP `frame-ancestors` refuse to render inside the preview iframe;
     the proxy strips both (everything else in the CSP is kept).
  2. The inspector. The same one-line <script> W7.3 asks people to paste
     into their HTML (previewUrl.js's buildInspectorSnippet(), served by
     the frontend's /mm-inspector.js route) is injected into HTML
     *document* responses, so hover/click selection works. Mapping a
     clicked element to a source line still needs the build-time plugin
     (`data-locatorjs` / `data-mm`) -- the proxy can't invent that.
  3. Working live reload. WebSocket upgrades (Vite/webpack/Next HMR) are
     tunnelled byte-for-byte to the dev server.

HOW IT WORKS, AND WHY IT'S SHAPED THIS WAY.

  - Stdlib only (asyncio streams). The daemon's dependency set is
    deliberately tiny (requirements.txt), and a proxy that has to
    tunnel WebSockets and rewrite one kind of response is easier to
    reason about as ~one page of explicit HTTP/1.1 than as a framework
    plus plugins.
  - Only the *head* of each message is parsed. Bodies are copied with
    their original framing (Content-Length / chunked / until-close), so
    JS bundles, SSE streams and uploads pass through untouched. The one
    exception is an HTML document, which is buffered (capped), edited,
    and re-sent with a fresh Content-Length.
  - One request per connection (`Connection: close` both ways). Slower
    than keep-alive in theory, but loopback connects are microseconds
    and it removes a whole class of request-smuggling / framing bugs.
  - We ask the dev server for `Accept-Encoding: identity`, so HTML
    arrives uncompressed and can be edited without a decompressor.

SECURITY POSTURE. This is a listening socket on the user's machine that
forwards to another local service, so it is deliberately hard to turn
into anything else:

  - It binds 127.0.0.1 only and forwards to ONE fixed upstream taken
    from config -- never from the request. The target must be a
    loopback address (same rule as the frontend's normalizePreviewUrl)
    and can't be MiniMe itself or the proxy's own port.
  - Requests must be origin-form (`GET /path`); absolute-form targets,
    CONNECT and `*` are refused, so it is not an open proxy.
  - The `Host` header must be 127.0.0.1:<port> or localhost:<port>.
    That is the DNS-rebinding defence: a hostile site whose DNS flips to
    127.0.0.1 sends `Host: evil.example`, and is refused instead of being
    handed the person's dev app.
  - Origin/Referer are only rewritten when they name the proxy itself.
    An unrelated Origin (another site's) is forwarded as-is, so the dev
    server's own CORS / origin checks still apply to it.
  - Requests with ambiguous framing (Content-Length + Transfer-Encoding,
    conflicting Content-Lengths, bare CR/LF or obsolete folding in a
    header) are rejected rather than forwarded.

KNOWN LIMITS (documented in daemon/README.md too): a CSP delivered via
<meta http-equiv> is not touched; an app that sends
`Cross-Origin-Embedder-Policy: require-corp` will block the injected
script (the frontend route sends no CORP header); cookies with a
`Domain=` attribute aren't rewritten; HTTP/1.1 only.

Place this file at: daemon/preview_proxy.py
"""
from __future__ import annotations

import argparse
import asyncio
import html
import ipaddress
import logging
import re
import ssl
import sys
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger("minime_daemon")

# Stable on purpose: the workbench remembers the last URL typed per
# workspace (previewUrl.js's savePreviewPrefs), so a port that changes
# every launch would make that memory useless. Not a port any common
# dev server defaults to (3000, 4200, 5173, 8000, 8080 ...).
DEFAULT_PORT = 5199

# Must stay in step with the frontend's INSPECTOR_SCRIPT_PATH
# (frontend/app/lib/preview/previewUrl.js).
INSPECTOR_SCRIPT_PATH = "/mm-inspector.js"

_MAX_HEAD_BYTES = 64 * 1024
_MAX_HTML_BYTES = 16 * 1024 * 1024  # HTML documents we're willing to buffer and edit
_CHUNK = 64 * 1024
_HEAD_TIMEOUT_SECONDS = 30.0  # an idle/speculative browser connection that never sends a request
_CONNECT_TIMEOUT_SECONDS = 5.0
# Generous: a framework's first request can sit in a compile step for a while.
_RESPONSE_HEAD_TIMEOUT_SECONDS = 120.0

_TOKEN_RE = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9+/_=-]+$")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_HEAD_CLOSE_RE = re.compile(rb"</head\s*>", re.IGNORECASE)
_BODY_OPEN_RE = re.compile(rb"<body[\s>]", re.IGNORECASE)
_BODY_CLOSE_RE = re.compile(rb"</body\s*>", re.IGNORECASE)

_DEFAULT_PORTS = {"http": 80, "https": 443}


# ---------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------


class PreviewConfigError(ValueError):
    """Raised for any bad preview-proxy setting. config.py re-raises it
    as ConfigError so the daemon fails at startup, same as every other
    bad .env value."""


@dataclass(frozen=True)
class PreviewProxyConfig:
    target_scheme: str  # "http" | "https"
    target_host: str  # hostname without IPv6 brackets, lowercase
    target_port: int
    frontend_origin: str  # MiniMe's own origin: where the inspector script is loaded from
    port: int = DEFAULT_PORT  # listen port on 127.0.0.1; 0 = pick a free one (tests)

    @property
    def target_origin(self) -> str:
        return _origin(self.target_scheme, self.target_host, self.target_port)

    @property
    def target_host_header(self) -> str:
        """What the dev server should see as `Host` (it may allow-list it)."""
        return _host_header(self.target_scheme, self.target_host, self.target_port)


def _host_header(scheme: str, host: str, port: int) -> str:
    shown = f"[{host}]" if ":" in host else host
    return shown if port == _DEFAULT_PORTS.get(scheme) else f"{shown}:{port}"


def _origin(scheme: str, host: str, port: int) -> str:
    return f"{scheme}://{_host_header(scheme, host, port)}"


def _is_loopback_host(hostname: str) -> bool:
    """Same rule as the frontend's isLoopbackHost(): localhost,
    *.localhost, 127.0.0.0/8, ::1."""
    h = hostname.lower().rstrip(".")
    if h == "localhost" or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _split_http_url(raw: str, label: str) -> tuple[str, str, int]:
    """Parse "scheme://host[:port]" (scheme optional, defaults to http)
    into (scheme, host, port); anything more than an origin is an error,
    so a pasted path or query can't silently be dropped."""
    text = (raw or "").strip()
    if not text:
        raise PreviewConfigError(f"{label} is empty")
    candidate = text if _SCHEME_RE.match(text) else f"http://{text}"
    try:
        parts = urlsplit(candidate)
        port = parts.port
    except ValueError as exc:
        raise PreviewConfigError(f"{label} is not a valid address: {text!r}") from exc
    if parts.scheme not in ("http", "https"):
        raise PreviewConfigError(f"{label} must be an http:// or https:// address (got: {text!r})")
    if parts.username or parts.password:
        raise PreviewConfigError(f"{label} must not contain a username or password")
    if not parts.hostname:
        raise PreviewConfigError(f"{label} has no host: {text!r}")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise PreviewConfigError(f"{label} must be just scheme://host:port, no path or query (got: {text!r})")
    return parts.scheme, parts.hostname, port or _DEFAULT_PORTS[parts.scheme]


def _parse_listen_port(value: object) -> int:
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_PORT
    try:
        port = int(str(value).strip())
    except ValueError as exc:
        raise PreviewConfigError(f"MINIME_PREVIEW_PORT must be a number (got: {value!r})") from exc
    if not 0 <= port <= 65535:
        raise PreviewConfigError(f"MINIME_PREVIEW_PORT must be between 0 and 65535 (got: {port})")
    return port


def build_preview_config(target: str, frontend_origin: str, port: object = None) -> PreviewProxyConfig:
    """Validate and normalise the three MINIME_PREVIEW_* / MINIME_FRONTEND_ORIGIN
    settings. Raises PreviewConfigError on anything unsafe or meaningless."""
    scheme, host, target_port = _split_http_url(target, "MINIME_PREVIEW_TARGET")
    if not _is_loopback_host(host):
        raise PreviewConfigError(
            "MINIME_PREVIEW_TARGET must be a local address (localhost, 127.0.0.1 or [::1]) -- "
            f"the preview proxy only fronts dev servers on this machine (got host: {host!r})"
        )

    if not (frontend_origin or "").strip():
        raise PreviewConfigError(
            "MINIME_FRONTEND_ORIGIN is not set in daemon/.env -- it's where MiniMe's web app "
            "lives (e.g. http://localhost:3000), which the injected inspector script is loaded from"
        )
    f_scheme, f_host, f_port = _split_http_url(frontend_origin, "MINIME_FRONTEND_ORIGIN")

    listen = _parse_listen_port(port)

    # Same guard as the frontend's normalizePreviewUrl(): never proxy
    # MiniMe to itself (a typo for "localhost:3000" is easy to make).
    if _is_loopback_host(f_host) and f_scheme == scheme and f_port == target_port:
        raise PreviewConfigError(
            "MINIME_PREVIEW_TARGET is the address MiniMe itself runs on -- point it at your own app's dev server"
        )
    if listen and target_port == listen:
        raise PreviewConfigError(
            f"MINIME_PREVIEW_PORT ({listen}) is the same as the target's port -- the proxy would forward to itself"
        )
    if listen and _is_loopback_host(f_host) and f_port == listen:
        raise PreviewConfigError(f"MINIME_PREVIEW_PORT ({listen}) collides with MiniMe's own port")

    return PreviewProxyConfig(
        target_scheme=scheme,
        target_host=host,
        target_port=target_port,
        frontend_origin=_origin(f_scheme, f_host, f_port),
        port=listen,
    )


# ---------------------------------------------------------------------
# Pure helpers (header + HTML rewriting) -- no IO, unit-tested directly
# ---------------------------------------------------------------------

Headers = list[tuple[str, str]]


def _header(headers: Headers, name: str) -> str | None:
    lname = name.lower()
    for key, value in headers:
        if key.lower() == lname:
            return value
    return None


def _tokens(value: str | None) -> list[str]:
    return [t.strip().lower() for t in (value or "").split(",") if t.strip()]


def _connection_tokens(headers: Headers) -> set[str]:
    out: set[str] = set()
    for key, value in headers:
        if key.lower() == "connection":
            out.update(_tokens(value))
    return out


def build_inspector_tag(origin: str, nonce: str | None = None) -> str:
    """The <script> tag to inject. Identical in shape to the frontend's
    buildInspectorSnippet() (the thing W7.3 asks people to paste), plus
    an optional CSP nonce."""
    o = html.escape(origin.rstrip("/"), quote=True)
    nonce_attr = f' nonce="{html.escape(nonce, quote=True)}"' if nonce else ""
    return f'<script src="{o}{INSPECTOR_SCRIPT_PATH}" data-mm-origin="{o}" defer{nonce_attr}></script>'


def inject_inspector(document: bytes, tag: str) -> bytes:
    """Insert `tag` into an HTML document: before </head> if there is
    one, else before <body>, else before </body>, else at the end. A
    document that already loads mm-inspector.js (someone who installed the
    W7.3 snippet by hand) is returned unchanged."""
    if INSPECTOR_SCRIPT_PATH.encode() in document:
        return document
    payload = tag.encode("utf-8")
    for pattern in (_HEAD_CLOSE_RE, _BODY_OPEN_RE, _BODY_CLOSE_RE):
        match = pattern.search(document)
        if match:
            at = match.start()
            return document[:at] + payload + document[at:]
    return document + payload


def _find_nonce(tokens: list[str]) -> str | None:
    for token in tokens:
        if token.lower().startswith("'nonce-") and token.endswith("'"):
            candidate = token[7:-1]
            if candidate and _NONCE_RE.match(candidate):
                return candidate
    return None


def _allow_origin(tokens: list[str], origin: str) -> list[str]:
    if [t.lower() for t in tokens] == ["'none'"]:
        return [origin]  # 'none' can't be combined with anything else
    if origin in tokens:
        return tokens
    return [*tokens, origin]


def rewrite_csp(value: str, script_origin: str) -> tuple[str | None, str | None]:
    """Make a Content-Security-Policy header value safe to embed and
    able to run the inspector script.

      - `frame-ancestors` is dropped (it's what forbids the preview
        iframe); every other directive is kept.
      - `script-src` / `script-src-elem` (or, if neither exists, a new
        `script-src-elem` copied from `default-src`) get `script_origin`
        added, so a `script-src 'self'` app still loads the inspector.
      - A `'nonce-…'` found in those directives is returned so the
        injected tag can carry it (needed under 'strict-dynamic', which
        ignores host allow-lists).

    A header value may hold several comma-separated policies; each is
    rewritten on its own. Returns (new_value_or_None_if_nothing_left,
    nonce_or_None)."""
    nonce: str | None = None
    policies: list[str] = []
    for policy in value.split(","):
        out: list[str] = []
        script_directive_seen = False
        default_tokens: list[str] | None = None
        for raw_directive in (d.strip() for d in policy.split(";")):
            if not raw_directive:
                continue
            name, *tokens = raw_directive.split()
            lname = name.lower()
            if lname == "frame-ancestors":
                continue
            if lname in ("script-src", "script-src-elem"):
                script_directive_seen = True
                nonce = nonce or _find_nonce(tokens)
                out.append(" ".join([name, *_allow_origin(tokens, script_origin)]))
                continue
            if lname == "default-src":
                default_tokens = tokens
                nonce = nonce or _find_nonce(tokens)
            out.append(raw_directive)
        if default_tokens is not None and not script_directive_seen:
            out.append(" ".join(["script-src-elem", *_allow_origin(default_tokens, script_origin)]))
        if out:
            policies.append("; ".join(out))
    return (", ".join(policies) or None), nonce


def rewrite_request_headers(
    headers: Headers,
    cfg: PreviewProxyConfig,
    proxy_origins: set[str],
    *,
    upgrade: bool,
) -> Headers:
    """Headers to send upstream. Host becomes the dev server's own (it
    may allow-list it); Origin/Referer that name the proxy are mapped to
    the dev server's origin; hop-by-hop headers are dropped."""
    target_origin = cfg.target_origin
    named_by_connection = _connection_tokens(headers)
    out: Headers = []
    for key, value in headers:
        lkey = key.lower()
        if lkey in ("proxy-authorization", "proxy-connection", "keep-alive", "te", "expect", "accept-encoding"):
            continue
        if lkey in named_by_connection and lkey not in ("upgrade", "connection"):
            continue
        if lkey in ("connection", "upgrade"):
            if upgrade:
                out.append((key, value))  # a WebSocket handshake needs these as sent
            continue
        if lkey == "host":
            out.append((key, cfg.target_host_header))
        elif lkey == "origin":
            out.append((key, target_origin if value in proxy_origins else value))
        elif lkey == "referer":
            out.append((key, _swap_origin_prefix(value, proxy_origins, target_origin)))
        else:
            out.append((key, value))
    out.append(("Accept-Encoding", "identity"))
    if not upgrade:
        out.append(("Connection", "close"))
    return out


def _swap_origin_prefix(url: str, old_origins: set[str], new_origin: str) -> str:
    for old in old_origins:
        if url == old or url.startswith((old + "/", old + "?", old + "#")):
            return new_origin + url[len(old):]
    return url


def rewrite_response_headers(
    headers: Headers,
    cfg: PreviewProxyConfig,
    proxy_origin: str,
) -> tuple[Headers, str | None]:
    """Headers to send the browser for a normal (non-101) response.
    Returns (headers, csp_nonce_for_the_injected_script)."""
    target_origin = cfg.target_origin
    named_by_connection = _connection_tokens(headers)
    out: Headers = []
    nonce: str | None = None
    for key, value in headers:
        lkey = key.lower()
        if lkey in ("x-frame-options", "connection", "keep-alive", "proxy-connection") or lkey in named_by_connection:
            continue
        if lkey == "content-security-policy":
            rewritten, found = rewrite_csp(value, cfg.frontend_origin)
            nonce = nonce or found
            if rewritten:
                out.append((key, rewritten))
            continue
        if lkey in ("location", "content-location"):
            value = _swap_origin_prefix(value, {target_origin}, proxy_origin)
        elif lkey == "access-control-allow-origin" and value == target_origin:
            value = proxy_origin
        out.append((key, value))
    out.append(("Connection", "close"))
    return out, nonce


def should_inject(
    request_headers: Headers,
    response_headers: Headers,
    *,
    method: str,
    status: int,
    body_kind: str,
    body_length: int,
) -> bool:
    """Only HTML *documents* get the inspector -- not an HTML fragment
    fetch()ed by the app (htmx, partials), not a download."""
    if method == "HEAD" or status < 200 or status in (204, 304) or body_kind == "none":
        return False
    media = (_header(response_headers, "content-type") or "").split(";")[0].strip().lower()
    if media not in ("text/html", "application/xhtml+xml"):
        return False
    content_type = (_header(response_headers, "content-type") or "").lower()
    if "charset=utf-16" in content_type or "charset=utf-32" in content_type:
        return False  # not ASCII-compatible: splicing ASCII bytes would corrupt it
    encoding = (_header(response_headers, "content-encoding") or "identity").strip().lower()
    if encoding not in ("", "identity"):
        return False
    if body_kind == "length" and body_length > _MAX_HTML_BYTES:
        return False
    dest = _header(request_headers, "sec-fetch-dest")
    if dest is not None:
        return dest.strip().lower() in ("document", "iframe", "frame")
    accept = _header(request_headers, "accept")  # older clients send no Sec-Fetch-*
    return accept is None or "text/html" in accept.lower() or "*/*" in accept


# ---------------------------------------------------------------------
# HTTP/1.1 head + body plumbing
# ---------------------------------------------------------------------


class _ProxyError(Exception):
    """A failure to report to the browser as a small error page. Only
    raised before any response bytes have been sent."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _parse_head(raw: bytes, *, bad_status: int) -> tuple[str, Headers]:
    """Split a message head into (start line, headers). Strict where
    leniency would enable request smuggling: bare CR/LF/NUL inside a
    line and obsolete line folding are refused."""
    text = raw.lstrip(b"\r\n")[:-4].decode("latin-1") if raw.endswith(b"\r\n\r\n") else ""
    if not text:
        raise _ProxyError(bad_status, "Malformed HTTP message.")
    lines = text.split("\r\n")
    if any(ch in line for line in lines for ch in ("\r", "\n", "\x00")):
        raise _ProxyError(bad_status, "Malformed HTTP message.")
    headers: Headers = []
    for line in lines[1:]:
        if line[:1] in (" ", "\t"):
            raise _ProxyError(bad_status, "Obsolete header folding is not supported.")
        name, sep, value = line.partition(":")
        if not sep or not _TOKEN_RE.match(name):
            raise _ProxyError(bad_status, "Malformed header line.")
        headers.append((name, value.strip(" \t")))
    return lines[0], headers


def _parse_request_line(line: str) -> tuple[str, str]:
    parts = line.split(" ")
    if len(parts) != 3 or not _TOKEN_RE.match(parts[0]) or parts[2] not in ("HTTP/1.0", "HTTP/1.1"):
        raise _ProxyError(400, "Malformed request line.")
    method, target = parts[0], parts[1]
    if not target.startswith("/"):
        # CONNECT host:port, absolute-form http://..., and `*`: all things
        # only a general-purpose proxy would serve. This one isn't.
        raise _ProxyError(400, "This proxy only serves requests addressed to it directly.")
    return method, target


def _parse_status_line(line: str) -> int:
    parts = line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/1.") or not parts[1].isdigit():
        raise _ProxyError(502, "Your dev server sent a response the preview proxy couldn't read.")
    return int(parts[1])


def _framing(headers: Headers, *, response: bool, status: int = 0, method: str = "GET") -> tuple[str, int]:
    """How a message body is delimited: ("none"|"length"|"chunked"|"eof", n)."""
    if response and (method == "HEAD" or status < 200 or status in (204, 304)):
        return "none", 0
    bad = 502 if response else 400
    transfer = [t for key, value in headers if key.lower() == "transfer-encoding" for t in _tokens(value)]
    lengths = [v for key, value in headers if key.lower() == "content-length" for v in _tokens(value)]
    if transfer:
        if transfer[-1] != "chunked":
            raise _ProxyError(bad, "Unsupported Transfer-Encoding.")
        if lengths and not response:
            raise _ProxyError(400, "Ambiguous message framing.")  # classic request-smuggling shape
        return "chunked", 0
    if lengths:
        if len(set(lengths)) != 1 or not lengths[0].isdigit():
            raise _ProxyError(bad, "Invalid Content-Length.")
        return "length", int(lengths[0])
    return ("eof", 0) if response else ("none", 0)


def _chunk_size(line: bytes) -> int:
    return int(line[:-2].split(b";")[0].strip(), 16)  # ValueError on garbage -> connection is dropped


async def _copy_body(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, framing: tuple[str, int]) -> None:
    """Copy one message body with its framing intact."""
    kind, size = framing
    if kind == "length":
        remaining = size
        while remaining:
            chunk = await reader.read(min(_CHUNK, remaining))
            if not chunk:
                raise asyncio.IncompleteReadError(b"", remaining)
            writer.write(chunk)
            await writer.drain()
            remaining -= len(chunk)
    elif kind == "chunked":
        while True:
            line = await reader.readuntil(b"\r\n")
            writer.write(line)
            n = _chunk_size(line)
            if n == 0:
                while True:  # trailer section, ends at a blank line
                    trailer = await reader.readuntil(b"\r\n")
                    writer.write(trailer)
                    if trailer == b"\r\n":
                        break
                await writer.drain()
                return
            writer.write(await reader.readexactly(n + 2))  # data + its CRLF
            await writer.drain()
    elif kind == "eof":
        while True:
            chunk = await reader.read(_CHUNK)
            if not chunk:
                return
            writer.write(chunk)
            await writer.drain()


async def _read_body(reader: asyncio.StreamReader, framing: tuple[str, int], limit: int) -> bytes:
    """Read one message body fully and decoded (de-chunked), or raise
    _ProxyError if it's bigger than `limit`."""
    kind, size = framing
    too_big = _ProxyError(502, "That page is too large for the preview proxy to add the inspector to.")
    if kind == "length":
        if size > limit:
            raise too_big
        return await reader.readexactly(size) if size else b""
    parts: list[bytes] = []
    total = 0
    if kind == "chunked":
        while True:
            n = _chunk_size(await reader.readuntil(b"\r\n"))
            if n == 0:
                while (await reader.readuntil(b"\r\n")) != b"\r\n":
                    pass
                return b"".join(parts)
            total += n
            if total > limit:
                raise too_big
            parts.append(await reader.readexactly(n))
            await reader.readexactly(2)
    while True:  # until close
        chunk = await reader.read(_CHUNK)
        if not chunk:
            return b"".join(parts)
        total += len(chunk)
        if total > limit:
            raise too_big
        parts.append(chunk)


def _format_head(start_line: str, headers: Headers) -> bytes:
    lines = [start_line, *(f"{key}: {value}" for key, value in headers)]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(_CHUNK)
            if not data:
                return
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        return


async def _tunnel(
    a_reader: asyncio.StreamReader,
    a_writer: asyncio.StreamWriter,
    b_reader: asyncio.StreamReader,
    b_writer: asyncio.StreamWriter,
) -> None:
    """Shuttle bytes both ways until either side closes (WebSocket)."""
    tasks = [asyncio.ensure_future(_pipe(a_reader, b_writer)), asyncio.ensure_future(_pipe(b_reader, a_writer))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


# ---------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------


class _ConnState:
    """Whether response bytes have gone out yet -- after that, an error
    page can no longer be sent and the connection is just closed."""

    started = False


class PreviewProxy:
    """start() binds 127.0.0.1:<config.port>; stop() tears down the
    listener and any open connections (a WebSocket would otherwise keep
    Server.wait_closed() waiting forever)."""

    def __init__(self, config: PreviewProxyConfig):
        self._cfg = config
        self._server: asyncio.AbstractServer | None = None
        self._port: int | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        self._upstream_ok: bool | None = None  # for log de-duplication only
        self._ssl: ssl.SSLContext | None = None
        if config.target_scheme == "https":
            # Verification is switched off ON PURPOSE: the target is
            # restricted to loopback (validated in build_preview_config),
            # where there is no network path to attack, and local dev
            # servers present self-signed certificates (Vite's basic-ssl,
            # mkcert without a trusted root, ...). Verifying would only
            # make https dev servers unusable.
            self._ssl = ssl.create_default_context()
            self._ssl.check_hostname = False  # nosemgrep
            self._ssl.verify_mode = ssl.CERT_NONE  # nosemgrep

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("preview proxy is not started")
        return self._port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, host="127.0.0.1", port=self._cfg.port, limit=_MAX_HEAD_BYTES
        )
        self._port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        for writer in list(self._writers):
            writer.close()
        try:
            await asyncio.wait_for(server.wait_closed(), timeout=2.0)
        except asyncio.TimeoutError:
            logger.debug("preview proxy: connections still closing after 2s, giving up waiting")

    # -- request handling ------------------------------------------------

    def _allowed_hosts(self) -> set[str]:
        hosts = {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}
        if self.port == 80:
            hosts |= {"127.0.0.1", "localhost"}
        return hosts

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        state = _ConnState()
        self._writers.add(writer)
        try:
            await self._serve(reader, writer, state)
        except _ProxyError as exc:
            if not state.started:
                await self._send_error(writer, exc.status, exc.message)
        except (ConnectionError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as exc:
            logger.debug("preview proxy: connection ended: %r", exc)
            if not state.started and not isinstance(exc, (ConnectionError, asyncio.IncompleteReadError)):
                await self._send_error(writer, 502, "The preview proxy couldn't complete that request.")
        except Exception:
            logger.exception("preview proxy: unexpected error")
            if not state.started:
                await self._send_error(writer, 500, "The preview proxy hit an internal error.")
        finally:
            self._writers.discard(writer)
            writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, state: _ConnState) -> None:
        cfg = self._cfg
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), _HEAD_TIMEOUT_SECONDS)
        except asyncio.LimitOverrunError:
            raise _ProxyError(431, "Request headers are too large.") from None
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return  # a speculative/idle connection that never sent a request

        start_line, headers = _parse_head(raw, bad_status=400)
        method, target = _parse_request_line(start_line)

        # DNS-rebinding defence -- see the module docstring.
        host = (_header(headers, "host") or "").lower()
        allowed = self._allowed_hosts()
        if host not in allowed:
            raise _ProxyError(403, "Blocked: this proxy only answers requests addressed to 127.0.0.1 or localhost.")
        proxy_origins = {f"http://{h}" for h in allowed}
        proxy_origin = f"http://{host}"

        upgrade = "upgrade" in _connection_tokens(headers) and "websocket" in _tokens(_header(headers, "upgrade"))
        framing = _framing(headers, response=False)
        if upgrade and framing[0] != "none":
            raise _ProxyError(400, "A WebSocket handshake can't carry a body.")
        expects_continue = "100-continue" in _tokens(_header(headers, "expect"))

        up_headers = rewrite_request_headers(headers, cfg, proxy_origins, upgrade=upgrade)
        up_reader, up_writer = await self._connect_upstream()
        try:
            up_writer.write(_format_head(f"{method} {target} HTTP/1.1", up_headers))
            if expects_continue:
                # We stripped `Expect` upstream, so answer it ourselves or
                # the client waits for a 100 that would never come.
                writer.write(b"HTTP/1.1 100 Continue\r\n\r\n")
                await writer.drain()
            await _copy_body(reader, up_writer, framing)
            await up_writer.drain()

            while True:  # skip interim responses (100 Continue, 103 Early Hints)
                try:
                    raw_resp = await asyncio.wait_for(up_reader.readuntil(b"\r\n\r\n"), _RESPONSE_HEAD_TIMEOUT_SECONDS)
                except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
                    raise _ProxyError(
                        502, f"Your dev server at {cfg.target_origin} closed the connection without answering."
                    ) from None
                status_line, resp_headers = _parse_head(raw_resp, bad_status=502)
                status = _parse_status_line(status_line)
                if status != 101 and 100 <= status < 200:
                    continue
                break

            if status == 101:
                if not upgrade:
                    raise _ProxyError(502, "Your dev server tried to switch protocols unprompted.")
                state.started = True
                writer.write(raw_resp)  # handshake reply verbatim; the tunnel is opaque after it
                await writer.drain()
                await _tunnel(reader, writer, up_reader, up_writer)
                return

            resp_framing = _framing(resp_headers, response=True, status=status, method=method)
            out_headers, nonce = rewrite_response_headers(resp_headers, cfg, proxy_origin)
            reason = status_line.split(" ", 2)[2] if status_line.count(" ") >= 2 else ""
            out_line = f"HTTP/1.1 {status} {reason}".rstrip()

            if should_inject(
                headers, resp_headers, method=method, status=status,
                body_kind=resp_framing[0], body_length=resp_framing[1],
            ):
                document = await _read_body(up_reader, resp_framing, _MAX_HTML_BYTES)
                edited = inject_inspector(document, build_inspector_tag(cfg.frontend_origin, nonce))
                # The edited copy is a different representation: drop the
                # headers that describe the original bytes, and stop the
                # browser caching a copy that embeds a script tag.
                stale = {
                    "content-length", "transfer-encoding", "etag", "last-modified", "cache-control",
                    "expires", "pragma", "content-md5", "digest", "content-digest", "repr-digest",
                }
                out_headers = [(k, v) for k, v in out_headers if k.lower() not in stale]
                out_headers += [("Content-Length", str(len(edited))), ("Cache-Control", "no-store")]
                state.started = True
                writer.write(_format_head(out_line, out_headers) + edited)
                await writer.drain()
            else:
                state.started = True
                writer.write(_format_head(out_line, out_headers))
                await _copy_body(up_reader, writer, resp_framing)
                await writer.drain()
        finally:
            up_writer.close()

    async def _connect_upstream(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        cfg = self._cfg
        try:
            pair = await asyncio.wait_for(
                asyncio.open_connection(cfg.target_host, cfg.target_port, ssl=self._ssl, limit=_MAX_HEAD_BYTES),
                _CONNECT_TIMEOUT_SECONDS,
            )
        except (OSError, asyncio.TimeoutError):
            if self._upstream_ok is not False:  # one line per outage, not one per asset request
                logger.warning("preview proxy: can't reach your dev server at %s -- is it running?", cfg.target_origin)
            self._upstream_ok = False
            raise _ProxyError(
                502, f"Couldn't reach your dev server at {cfg.target_origin}. Is it running? Reload once it is."
            ) from None
        if self._upstream_ok is False:
            logger.info("preview proxy: dev server at %s is reachable again", cfg.target_origin)
        self._upstream_ok = True
        return pair

    @staticmethod
    async def _send_error(writer: asyncio.StreamWriter, status: int, message: str) -> None:
        reasons = {400: "Bad Request", 403: "Forbidden", 431: "Request Header Fields Too Large",
                   500: "Internal Server Error", 502: "Bad Gateway"}
        body = (
            "<!doctype html><meta charset=utf-8><title>MiniMe preview proxy</title>"
            '<body style="font:14px system-ui,sans-serif;padding:24px;color:#444">'
            f"<h3>MiniMe preview proxy</h3><p>{html.escape(message)}</p></body>"
        ).encode("utf-8")
        head = _format_head(
            f"HTTP/1.1 {status} {reasons.get(status, 'Error')}",
            [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))),
             ("Cache-Control", "no-store"), ("Connection", "close")],
        )
        try:
            writer.write(head + body)
            await writer.drain()
        except (ConnectionError, OSError):
            pass


# ---------------------------------------------------------------------
# Standalone entry point: `python -m daemon.preview_proxy --target ...`
# Runs just the proxy, without pairing with the backend -- handy for
# trying it out, and it's what the integration tests exercise.
# ---------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m daemon.preview_proxy",
        description="Run only the MiniMe preview proxy (no backend pairing).",
    )
    parser.add_argument("--target", required=True, help="your dev server, e.g. http://localhost:5173")
    parser.add_argument("--frontend-origin", required=True, help="MiniMe's web app, e.g. http://localhost:3000")
    parser.add_argument("--port", default=None, help=f"listen port on 127.0.0.1 (default {DEFAULT_PORT})")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        config = build_preview_config(args.target, args.frontend_origin, args.port)
    except PreviewConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    async def _run() -> None:
        proxy = PreviewProxy(config)
        await proxy.start()
        logger.info("preview proxy: %s -> %s (Ctrl+C to stop)", proxy.url, config.target_origin)
        try:
            await asyncio.Event().wait()
        finally:
            await proxy.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
