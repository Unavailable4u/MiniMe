"""
daemon/tests/test_preview_proxy.py — W7.4: the preview reverse proxy.

Two layers, like the rest of this repo's tests:

  - pure functions (config validation, CSP / header rewriting, HTML
    injection, the inject-or-not decision) tested directly;
  - the real server on real loopback sockets, in front of a tiny fake
    "dev server", driven with hand-written raw HTTP so framing, header
    handling and the refusal paths are asserted byte-for-byte rather
    than through a client library that would tidy them up.

No pytest-asyncio (not a daemon dependency): each scenario is an async
function run with asyncio.run().

Run: pytest daemon/tests/test_preview_proxy.py -v
"""
from __future__ import annotations

import asyncio
import os
import shutil
import ssl
import subprocess
from contextlib import asynccontextmanager

import pytest

from daemon.preview_proxy import (
    PreviewConfigError,
    PreviewProxy,
    PreviewProxyConfig,
    build_inspector_tag,
    build_preview_config,
    inject_inspector,
    rewrite_csp,
    rewrite_request_headers,
    rewrite_response_headers,
    should_inject,
)

FRONTEND = "http://localhost:3000"
TAG = build_inspector_tag(FRONTEND)


# ---------------------------------------------------------------------
# build_preview_config
# ---------------------------------------------------------------------


def test_config_normalises_a_loopback_target():
    cfg = build_preview_config("localhost:5173", "http://localhost:3000/", "5199")
    assert (cfg.target_scheme, cfg.target_host, cfg.target_port) == ("http", "localhost", 5173)
    assert cfg.target_origin == "http://localhost:5173"
    assert cfg.frontend_origin == "http://localhost:3000"  # trailing slash dropped
    assert cfg.port == 5199


def test_config_default_port_and_default_scheme_ports():
    assert build_preview_config("http://127.0.0.1:5173", FRONTEND).port == 5199
    cfg = build_preview_config("https://localhost", FRONTEND)
    assert cfg.target_port == 443 and cfg.target_origin == "https://localhost"  # default port omitted from the origin


def test_config_accepts_ipv6_and_dot_localhost():
    assert build_preview_config("http://[::1]:8080", FRONTEND).target_origin == "http://[::1]:8080"
    assert build_preview_config("http://app.localhost:3001", FRONTEND).target_host == "app.localhost"


@pytest.mark.parametrize(
    "target",
    [
        "http://example.com:3000",  # not local
        "http://192.168.1.20:3000",  # LAN, not loopback
        "http://0.0.0.0:3000",
        "ftp://localhost:21",
        "http://user:pw@localhost:3000",
        "http://localhost:3000/app",  # an origin, not a URL with a path
        "http://localhost:3000?x=1",
        "http://localhost:notaport",
        "",
    ],
)
def test_config_rejects_unsafe_or_malformed_targets(target):
    with pytest.raises(PreviewConfigError):
        build_preview_config(target, FRONTEND)


def test_config_requires_the_frontend_origin():
    with pytest.raises(PreviewConfigError, match="MINIME_FRONTEND_ORIGIN is not set"):
        build_preview_config("http://localhost:5173", "")


def test_config_refuses_to_proxy_minime_to_itself():
    with pytest.raises(PreviewConfigError, match="MiniMe itself"):
        build_preview_config("http://127.0.0.1:3000", "http://localhost:3000")  # alias of the same loopback port


def test_config_refuses_a_proxy_that_would_forward_to_itself_or_collide_with_minime():
    with pytest.raises(PreviewConfigError, match="forward to itself"):
        build_preview_config("http://localhost:5199", FRONTEND, "5199")
    with pytest.raises(PreviewConfigError, match="collides"):
        build_preview_config("http://localhost:5173", FRONTEND, "3000")


@pytest.mark.parametrize("port", ["abc", "-1", "70000"])
def test_config_rejects_bad_listen_ports(port):
    with pytest.raises(PreviewConfigError, match="MINIME_PREVIEW_PORT"):
        build_preview_config("http://localhost:5173", FRONTEND, port)


# ---------------------------------------------------------------------
# inspector tag + injection
# ---------------------------------------------------------------------


def test_inspector_tag_matches_the_snippet_the_frontend_shows():
    # frontend/app/lib/preview/previewUrl.js buildInspectorSnippet(origin)
    assert TAG == (
        '<script src="http://localhost:3000/mm-inspector.js" '
        'data-mm-origin="http://localhost:3000" defer></script>'
    )


def test_inspector_tag_carries_a_nonce_and_escapes_attributes():
    assert build_inspector_tag(FRONTEND, "abc123").endswith(' defer nonce="abc123"></script>')
    hostile = build_inspector_tag('http://x"onload="y')
    assert "&quot;" in hostile and 'x"onload' not in hostile


def test_inject_goes_before_head_close_case_insensitively():
    doc = b"<html><HEAD><title>t</title></HEAD ><body>hi</body></html>"
    out = inject_inspector(doc, TAG)
    assert out == b"<html><HEAD><title>t</title>" + TAG.encode() + b"</HEAD ><body>hi</body></html>"


def test_inject_falls_back_to_body_then_end():
    assert inject_inspector(b"<body>x</body>", TAG) == TAG.encode() + b"<body>x</body>"
    assert inject_inspector(b"<p>fragment</p>", TAG) == b"<p>fragment</p>" + TAG.encode()
    assert inject_inspector(b"<p>x</p></body>", TAG) == b"<p>x</p>" + TAG.encode() + b"</body>"


def test_inject_leaves_a_page_that_already_has_the_snippet_alone():
    doc = b'<head><script src="http://localhost:3000/mm-inspector.js"></script></head>'
    assert inject_inspector(doc, TAG) == doc


# ---------------------------------------------------------------------
# CSP
# ---------------------------------------------------------------------


def test_csp_drops_frame_ancestors_and_keeps_the_rest():
    value, nonce = rewrite_csp("default-src 'self'; frame-ancestors 'none'; img-src *", FRONTEND)
    assert "frame-ancestors" not in value
    assert "img-src *" in value and "default-src 'self'" in value
    assert nonce is None


def test_csp_only_frame_ancestors_means_no_header_at_all():
    assert rewrite_csp("frame-ancestors 'self'", FRONTEND) == (None, None)


def test_csp_script_src_gets_the_inspector_origin():
    value, _ = rewrite_csp("script-src 'self'; object-src 'none'", FRONTEND)
    assert "script-src 'self' http://localhost:3000" in value
    value, _ = rewrite_csp("script-src 'none'", FRONTEND)
    assert value == "script-src http://localhost:3000"  # 'none' can't be combined with a source


def test_csp_default_src_only_gets_a_script_src_elem_so_other_fetches_are_unchanged():
    value, _ = rewrite_csp("default-src 'self'", FRONTEND)
    assert value == "default-src 'self'; script-src-elem 'self' http://localhost:3000"


def test_csp_nonce_is_returned_for_the_injected_tag():
    _, nonce = rewrite_csp("script-src 'nonce-AbC+/=_-' 'strict-dynamic'", FRONTEND)
    assert nonce == "AbC+/=_-"
    _, nonce = rewrite_csp("script-src 'nonce-\"><x>'", FRONTEND)
    assert nonce is None  # not a legal nonce: never put it in an attribute


def test_csp_multiple_policies_each_rewritten():
    value, _ = rewrite_csp("script-src 'self', frame-ancestors 'none'; img-src data:", FRONTEND)
    assert value == "script-src 'self' http://localhost:3000, img-src data:"


# ---------------------------------------------------------------------
# header rewriting
# ---------------------------------------------------------------------

CFG = PreviewProxyConfig("http", "localhost", 5173, FRONTEND, port=5199)
PROXY_ORIGINS = {"http://127.0.0.1:5199", "http://localhost:5199"}


def _as_dict(headers):
    return {k.lower(): v for k, v in headers}


def test_request_headers_are_pointed_at_the_dev_server():
    out = rewrite_request_headers(
        [
            ("Host", "127.0.0.1:5199"),
            ("Origin", "http://127.0.0.1:5199"),
            ("Referer", "http://127.0.0.1:5199/page?x=1"),
            ("Accept-Encoding", "gzip, br"),
            ("Connection", "keep-alive, X-Secret"),
            ("X-Secret", "dropped because Connection names it"),
            ("Keep-Alive", "timeout=5"),
            ("Cookie", "a=b"),
        ],
        CFG, PROXY_ORIGINS, upgrade=False,
    )
    d = _as_dict(out)
    assert d["host"] == "localhost:5173"
    assert d["origin"] == "http://localhost:5173"
    assert d["referer"] == "http://localhost:5173/page?x=1"
    assert d["accept-encoding"] == "identity"
    assert d["connection"] == "close"
    assert d["cookie"] == "a=b"
    assert "x-secret" not in d and "keep-alive" not in d


def test_a_foreign_origin_is_not_rewritten_so_the_dev_servers_cors_still_applies():
    out = rewrite_request_headers(
        [("Host", "127.0.0.1:5199"), ("Origin", "https://evil.example")], CFG, PROXY_ORIGINS, upgrade=False
    )
    assert _as_dict(out)["origin"] == "https://evil.example"


def test_websocket_handshake_headers_survive():
    out = rewrite_request_headers(
        [
            ("Host", "127.0.0.1:5199"),
            ("Connection", "Upgrade"),
            ("Upgrade", "websocket"),
            ("Sec-WebSocket-Key", "k"),
            ("Sec-WebSocket-Protocol", "vite-hmr"),
        ],
        CFG, PROXY_ORIGINS, upgrade=True,
    )
    d = _as_dict(out)
    assert d["connection"] == "Upgrade" and d["upgrade"] == "websocket"
    assert d["sec-websocket-protocol"] == "vite-hmr"


def test_response_headers_lose_embed_blockers_and_get_mapped_back():
    out, nonce = rewrite_response_headers(
        [
            ("X-Frame-Options", "DENY"),
            ("Content-Security-Policy", "frame-ancestors 'none'; img-src *"),
            ("Location", "http://localhost:5173/login?next=/"),
            ("Access-Control-Allow-Origin", "http://localhost:5173"),
            ("Connection", "keep-alive"),
            ("Set-Cookie", "sid=1; Path=/"),
        ],
        CFG, "http://127.0.0.1:5199",
    )
    d = _as_dict(out)
    assert "x-frame-options" not in d
    assert d["content-security-policy"] == "img-src *"
    assert d["location"] == "http://127.0.0.1:5199/login?next=/"
    assert d["access-control-allow-origin"] == "http://127.0.0.1:5199"
    assert d["connection"] == "close"
    assert d["set-cookie"] == "sid=1; Path=/"
    assert nonce is None


# ---------------------------------------------------------------------
# should_inject
# ---------------------------------------------------------------------

HTML = [("Content-Type", "text/html; charset=utf-8")]


def _inject(req, resp=HTML, **kw):
    args = {"method": "GET", "status": 200, "body_kind": "length", "body_length": 10}
    args.update(kw)
    return should_inject(req, resp, **args)


def test_inject_only_for_html_documents():
    assert _inject([("Sec-Fetch-Dest", "iframe")])
    assert _inject([("Sec-Fetch-Dest", "document")])
    assert not _inject([("Sec-Fetch-Dest", "empty")])  # fetch()/XHR of an HTML fragment
    assert not _inject([("Sec-Fetch-Dest", "script")])
    assert not _inject([("Sec-Fetch-Dest", "iframe")], [("Content-Type", "application/json")])
    assert not _inject([("Sec-Fetch-Dest", "iframe")], HTML, method="HEAD")
    assert not _inject([("Sec-Fetch-Dest", "iframe")], HTML, status=304)
    assert not _inject([("Sec-Fetch-Dest", "iframe")], HTML, body_kind="none")


def test_inject_falls_back_to_accept_when_a_client_sends_no_fetch_metadata():
    assert _inject([("Accept", "text/html,application/xhtml+xml")])
    assert not _inject([("Accept", "application/json")])
    assert _inject([])


def test_inject_skips_what_it_cannot_safely_edit():
    req = [("Sec-Fetch-Dest", "iframe")]
    assert not _inject(req, [("Content-Type", "text/html"), ("Content-Encoding", "br")])
    assert not _inject(req, [("Content-Type", "text/html; charset=utf-16")])
    assert not _inject(req, HTML, body_length=64 * 1024 * 1024)


# ---------------------------------------------------------------------
# The server, end to end on real sockets
# ---------------------------------------------------------------------


def http_response(status=200, headers=(), body=b"", chunked=False):
    reason = {200: "OK", 301: "Moved Permanently", 404: "Not Found"}.get(status, "X")
    lines = [f"HTTP/1.1 {status} {reason}", *(f"{k}: {v}" for k, v in headers), "Connection: close"]
    if chunked:
        lines.append("Transfer-Encoding: chunked")
        mid = max(1, len(body) // 2)
        pieces = [body[:mid], body[mid:]]
        payload = b"".join(f"{len(p):x}\r\n".encode() + p + b"\r\n" for p in pieces if p) + b"0\r\n\r\n"
    else:
        lines.append(f"Content-Length: {len(body)}")
        payload = body
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + payload


class FakeUpstream:
    """A stand-in dev server. `handler(request, reader, writer)` gets the
    parsed request and either returns raw response bytes or writes to the
    socket itself (returning None)."""

    def __init__(self, handler, ssl_context=None):
        self.handler = handler
        self.ssl_context = ssl_context
        self.requests: list[dict] = []
        self._writers: set[asyncio.StreamWriter] = set()

    async def start(self):
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0, ssl=self.ssl_context)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self):
        self._server.close()
        for w in list(self._writers):
            w.close()
        await asyncio.wait_for(self._server.wait_closed(), 2)

    async def _serve(self, reader, writer):
        self._writers.add(writer)
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = {}
            for line in lines[1:]:
                if line:
                    k, v = line.split(":", 1)
                    headers[k.lower()] = v.strip()
            body = b""
            if "content-length" in headers:
                body = await reader.readexactly(int(headers["content-length"]))
            elif headers.get("transfer-encoding") == "chunked":
                while True:
                    size = int((await reader.readuntil(b"\r\n")).strip() or b"0", 16)
                    if size == 0:
                        await reader.readuntil(b"\r\n")
                        break
                    body += await reader.readexactly(size)
                    await reader.readexactly(2)
            request = {"method": method, "target": target, "headers": headers, "body": body}
            self.requests.append(request)
            out = await self.handler(request, reader, writer)
            if out is not None:
                writer.write(out)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


@asynccontextmanager
async def running(handler, frontend=FRONTEND, ssl_context=None):
    upstream = FakeUpstream(handler, ssl_context)
    await upstream.start()
    scheme = "https" if ssl_context else "http"
    proxy = PreviewProxy(PreviewProxyConfig(scheme, "127.0.0.1", upstream.port, frontend, port=0))
    await proxy.start()
    try:
        yield proxy, upstream
    finally:
        await proxy.stop()
        await upstream.stop()


def build_request(proxy, path="/", method="GET", headers=(), body=b"", host=None):
    sent = {k.lower() for k, _ in headers}
    lines = [f"{method} {path} HTTP/1.1"]
    if "host" not in sent:
        lines.append(f"Host: {host or f'127.0.0.1:{proxy.port}'}")
    lines += [f"{k}: {v}" for k, v in headers]
    if body and not ({"content-length", "transfer-encoding"} & sent):
        lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body


async def exchange(proxy, raw: bytes):
    """Send raw bytes, read until the proxy closes; return (status, headers, body)."""
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
    writer.write(raw)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(-1), 5)
    writer.close()
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    headers = [tuple(line.split(": ", 1)) for line in lines[1:]]
    return int(lines[0].split(" ")[1]), headers, body


def get(headers, name):
    values = [v for k, v in headers if k.lower() == name.lower()]
    return values[0] if values else None


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def test_html_gets_the_inspector_and_loses_its_embed_blockers():
    html_doc = b"<!doctype html><html><head><title>app</title></head><body><div id=root></div></body></html>"

    async def handler(req, r, w):
        return http_response(
            200,
            [
                ("Content-Type", "text/html; charset=utf-8"),
                ("X-Frame-Options", "SAMEORIGIN"),
                ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'self'"),
                ("ETag", '"abc"'),
            ],
            html_doc,
        )

    async def scenario():
        async with running(handler) as (proxy, upstream):
            raw = build_request(proxy, "/", headers=[("Sec-Fetch-Dest", "iframe"), ("Accept-Encoding", "gzip, br")])
            status, headers, body = await exchange(proxy, raw)
            seen = upstream.requests[0]
            return status, headers, body, seen, upstream.port

    status, headers, body, seen, up_port = run(scenario())
    assert status == 200
    assert TAG.encode() in body and body.index(TAG.encode()) < body.index(b"</head>")
    assert get(headers, "x-frame-options") is None
    csp = get(headers, "content-security-policy")
    assert "frame-ancestors" not in csp and "script-src-elem 'self' http://localhost:3000" in csp
    assert int(get(headers, "content-length")) == len(body)
    assert get(headers, "etag") is None and get(headers, "cache-control") == "no-store"
    # What the dev server saw:
    assert seen["headers"]["host"] == f"127.0.0.1:{up_port}"
    assert seen["headers"]["accept-encoding"] == "identity"
    assert seen["headers"]["connection"] == "close"


def test_a_csp_nonce_is_copied_onto_the_injected_script():
    async def handler(req, r, w):
        return http_response(
            200,
            [("Content-Type", "text/html"), ("Content-Security-Policy", "script-src 'nonce-XyZ9' 'strict-dynamic'")],
            b"<head></head>",
        )

    async def scenario():
        async with running(handler) as (proxy, _):
            return await exchange(proxy, build_request(proxy, headers=[("Sec-Fetch-Dest", "iframe")]))

    _, _, body = run(scenario())
    assert b'defer nonce="XyZ9"></script>' in body


def test_non_html_passes_through_byte_for_byte_with_its_framing():
    blob = os.urandom(200_000)

    async def handler(req, r, w):
        if req["target"] == "/chunked.js":
            return http_response(200, [("Content-Type", "application/javascript")], b"console.log('hi'); // chunked", chunked=True)
        return http_response(200, [("Content-Type", "application/octet-stream")], blob)

    async def scenario():
        async with running(handler) as (proxy, _):
            a = await exchange(proxy, build_request(proxy, "/blob.bin"))
            b = await exchange(proxy, build_request(proxy, "/chunked.js"))
            return a, b

    (s1, h1, b1), (s2, h2, b2) = run(scenario())
    assert s1 == 200 and b1 == blob
    assert s2 == 200 and get(h2, "transfer-encoding") == "chunked"
    assert b2.endswith(b"0\r\n\r\n") and b"console.log" in b2  # chunk framing preserved


def test_an_html_fragment_fetched_by_the_app_is_not_modified():
    fragment = b"<li>row</li></head>"

    async def handler(req, r, w):
        return http_response(200, [("Content-Type", "text/html")], fragment)

    async def scenario():
        async with running(handler) as (proxy, _):
            return await exchange(proxy, build_request(proxy, "/partial", headers=[("Sec-Fetch-Dest", "empty")]))

    _, _, body = run(scenario())
    assert body == fragment


def test_a_chunked_html_document_is_reassembled_and_injected():
    async def handler(req, r, w):
        return http_response(200, [("Content-Type", "text/html")], b"<html><head></head><body>streamed</body></html>", chunked=True)

    async def scenario():
        async with running(handler) as (proxy, _):
            return await exchange(proxy, build_request(proxy, headers=[("Sec-Fetch-Dest", "iframe")]))

    _, headers, body = run(scenario())
    assert get(headers, "transfer-encoding") is None
    assert int(get(headers, "content-length")) == len(body)
    assert body.startswith(b"<html><head>" + TAG.encode() + b"</head>") and body.endswith(b"streamed</body></html>")


def test_redirects_are_pointed_back_at_the_proxy():
    async def handler(req, r, w):
        return http_response(301, [("Location", f"http://127.0.0.1:{upstream_port[0]}/login")], b"")

    upstream_port = [0]

    async def scenario():
        async with running(handler) as (proxy, upstream):
            upstream_port[0] = upstream.port
            status, headers, _ = await exchange(proxy, build_request(proxy, "/"))
            return status, headers, proxy.port

    status, headers, proxy_port = run(scenario())
    assert status == 301
    assert get(headers, "location") == f"http://127.0.0.1:{proxy_port}/login"


def test_requests_for_any_other_host_header_are_refused_before_reaching_the_dev_server():
    async def handler(req, r, w):
        return http_response(200, [], b"secret")

    async def scenario():
        async with running(handler) as (proxy, upstream):
            res = [
                await exchange(proxy, build_request(proxy, host="evil.example")),  # DNS rebinding
                await exchange(proxy, build_request(proxy, host=f"evil.example:{proxy.port}")),
                await exchange(proxy, build_request(proxy, host="127.0.0.1:1")),  # wrong port
            ]
            return res, len(upstream.requests)

    results, reached_upstream = run(scenario())
    assert [r[0] for r in results] == [403, 403, 403]
    assert all(b"secret" not in r[2] for r in results)
    assert reached_upstream == 0


def test_it_is_not_an_open_proxy():
    async def handler(req, r, w):
        return http_response(200, [], b"x")

    async def scenario():
        async with running(handler) as (proxy, upstream):
            res = [
                await exchange(proxy, build_request(proxy, "http://example.com/")),  # absolute-form
                await exchange(proxy, build_request(proxy, "example.com:443", method="CONNECT")),
                await exchange(proxy, build_request(proxy, "*", method="OPTIONS")),
            ]
            return res, len(upstream.requests)

    results, reached_upstream = run(scenario())
    assert [r[0] for r in results] == [400, 400, 400]
    assert reached_upstream == 0


def test_ambiguous_or_malformed_framing_is_refused_not_forwarded():
    async def handler(req, r, w):
        return http_response(200, [], b"x")

    async def scenario():
        async with running(handler) as (proxy, upstream):
            both = build_request(proxy, "/x", "POST", [("Content-Length", "3"), ("Transfer-Encoding", "chunked")], b"abc")
            two_lengths = build_request(proxy, "/x", "POST", [("Content-Length", "3"), ("Content-Length", "4")], b"abc")
            bare_lf = b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-A: 1\nX-B: 2\r\n\r\n" % proxy.port
            folded = b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-A: 1\r\n  continued\r\n\r\n" % proxy.port
            res = [await exchange(proxy, raw) for raw in (both, two_lengths, bare_lf, folded)]
            return res, len(upstream.requests)

    results, reached_upstream = run(scenario())
    assert [r[0] for r in results] == [400, 400, 400, 400]
    assert reached_upstream == 0


def test_request_bodies_reach_the_dev_server_intact_and_origin_is_mapped():
    async def handler(req, r, w):
        return http_response(200, [("Content-Type", "text/plain")], b"ok")

    chunked_body = b"4\r\nWiki\r\n5\r\npedia\r\n0\r\n\r\n"

    async def scenario():
        async with running(handler) as (proxy, upstream):
            origin = f"http://127.0.0.1:{proxy.port}"
            await exchange(proxy, build_request(proxy, "/api", "POST", [("Origin", origin)], b'{"a":1}'))
            await exchange(proxy, build_request(proxy, "/up", "PUT", [("Transfer-Encoding", "chunked")], chunked_body))
            return upstream.requests, upstream.port

    (post, put), up_port = run(scenario())
    assert post["body"] == b'{"a":1}' and post["headers"]["origin"] == f"http://127.0.0.1:{up_port}"
    assert put["body"] == b"Wikipedia"


def test_expect_continue_is_answered_by_the_proxy():
    async def handler(req, r, w):
        return http_response(200, [], req["body"])

    async def scenario():
        async with running(handler) as (proxy, upstream):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(build_request(proxy, "/big", "POST", [("Expect", "100-continue"), ("Content-Length", "5")]))
            await writer.drain()
            interim = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)  # would hang if we forwarded Expect
            writer.write(b"hello")
            await writer.drain()
            rest = await asyncio.wait_for(reader.read(-1), 5)
            writer.close()
            return interim, rest, upstream.requests[0]

    interim, rest, seen = run(scenario())
    assert interim.startswith(b"HTTP/1.1 100 Continue")
    assert rest.endswith(b"hello") and "expect" not in seen["headers"]


def test_unreachable_dev_server_gets_a_readable_502_not_a_hang():
    async def scenario():
        # Bind then close a socket to learn a port nothing is listening on.
        probe = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        dead_port = probe.sockets[0].getsockname()[1]
        probe.close()
        await probe.wait_closed()
        proxy = PreviewProxy(PreviewProxyConfig("http", "127.0.0.1", dead_port, FRONTEND, port=0))
        await proxy.start()
        try:
            return await exchange(proxy, build_request(proxy)), dead_port
        finally:
            await proxy.stop()

    (status, headers, body), dead_port = run(scenario())
    assert status == 502
    assert f"http://127.0.0.1:{dead_port}".encode() in body and b"Is it running" in body
    assert get(headers, "x-frame-options") is None  # the error page itself must be embeddable too


def websocket_upgrade_handler(log):
    async def handler(req, reader, writer):
        log.append(req)
        writer.write(
            b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            b"Sec-WebSocket-Accept: abc\r\n\r\n"
        )
        await writer.drain()
        while data := await reader.read(1024):  # echo, shouted
            writer.write(data.upper())
            await writer.drain()
        return None

    return handler


def test_websocket_upgrades_are_tunnelled_so_hmr_keeps_working():
    log: list[dict] = []

    async def scenario():
        async with running(websocket_upgrade_handler(log)) as (proxy, upstream):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(
                build_request(
                    proxy, "/?token=t", headers=[
                        ("Connection", "Upgrade"), ("Upgrade", "websocket"),
                        ("Sec-WebSocket-Key", "k"), ("Sec-WebSocket-Protocol", "vite-hmr"),
                        ("Origin", f"http://127.0.0.1:{proxy.port}"),
                    ],
                )
            )
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            writer.write(b"ping")
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(4), 5)
            writer.close()
            return head, echoed, upstream.port

    head, echoed, up_port = run(scenario())
    assert head.startswith(b"HTTP/1.1 101") and b"Sec-WebSocket-Accept: abc" in head
    assert echoed == b"PING"
    seen = log[0]["headers"]
    assert seen["upgrade"] == "websocket" and "upgrade" in seen["connection"].lower()
    assert seen["sec-websocket-protocol"] == "vite-hmr"
    assert seen["origin"] == f"http://127.0.0.1:{up_port}"  # dev servers check this on the handshake


def test_stop_does_not_hang_on_an_open_websocket():
    async def scenario():
        async with running(websocket_upgrade_handler([])) as (proxy, _):
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            writer.write(build_request(proxy, "/", headers=[("Connection", "Upgrade"), ("Upgrade", "websocket")]))
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            await asyncio.wait_for(proxy.stop(), 5)  # the tunnel is still open
            writer.close()
        return True

    assert run(scenario())


def test_a_page_with_the_snippet_already_installed_is_not_double_injected():
    page = b'<head><script src="http://localhost:3000/mm-inspector.js" data-mm-origin="http://localhost:3000" defer></script></head>'

    async def handler(req, r, w):
        return http_response(200, [("Content-Type", "text/html")], page)

    async def scenario():
        async with running(handler) as (proxy, _):
            return await exchange(proxy, build_request(proxy, headers=[("Sec-Fetch-Dest", "iframe")]))

    _, _, body = run(scenario())
    assert body == page


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl CLI to mint a throwaway certificate")
def test_an_https_dev_server_with_a_self_signed_certificate_works(tmp_path):
    # Vite's basic-ssl, mkcert without a trusted root, `next --experimental-https`:
    # local https dev servers present certificates nothing trusts. The proxy
    # connects to loopback targets without verifying them (see PreviewProxy.__init__).
    key, cert = tmp_path / "key.pem", tmp_path / "cert.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
         "-subj", "/CN=localhost", "-days", "1", "-keyout", str(key), "-out", str(cert)],
        check=True, capture_output=True,
    )
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(cert, key)

    async def handler(req, r, w):
        return http_response(200, [("Content-Type", "text/html")], b"<head></head>secure")

    async def scenario():
        async with running(handler, ssl_context=server_ctx) as (proxy, upstream):
            result = await exchange(proxy, build_request(proxy, headers=[("Sec-Fetch-Dest", "iframe")]))
            return result, upstream.port

    (status, _, body), up_port = run(scenario())
    assert status == 200 and TAG.encode() in body and body.endswith(b"secure")
