"""
tests/unit/test_url_guard_fetch.py — Patch SX-05.

Covers the safe fetch in utils/url_guard.py: safe_stream and safe_get, and the errors they raise.

No test here uses the internet.  Every request goes to a throw-away server on 127.0.0.1 (HTTP, or HTTPS with a
certificate from a throw-away CA made with the ``cryptography`` package).  Names such as ``web.test`` are mapped to
127.0.0.1 by a fake resolver, and a policy that lets the loopback address through (every other address stays
blocked).  An autouse fixture makes the real ``socket.getaddrinfo`` raise, which proves that the fetch never does a
name lookup of its own: names go through ``GuardPolicy.resolver`` only.

If a urllib3 bump (SX-13) breaks the pinned-address connect, these tests are the tripwire.
Nothing in this file skips.
"""
import datetime
import gzip
import http.server
import socket
import socketserver
import ssl
import threading
import time

import pytest
import requests

from utils.url_guard import (
    FetchError,
    FetchTimeout,
    GuardPolicy,
    ResponseTooLarge,
    UnsafeURL,
    blocked_address,
    safe_get,
    safe_stream,
)


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real DNS resolver")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


class FakeResolver:
    """Maps a host name to the list of addresses a lookup should answer; counts and records the calls."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        if host not in self.table:
            raise OSError("no such host")
        return list(self.table[host])


def loopback_policy(table=None, **overrides):
    """A policy that lets 127.0.0.1 through and keeps every other address blocked, with any port allowed."""
    resolver = FakeResolver(table if table is not None else {"web.test": ["127.0.0.1"]})

    def blocked(ip):
        return False if ip == "127.0.0.1" else blocked_address(ip)

    options = dict(resolver=resolver, blocked=blocked, ports=None)
    options.update(overrides)
    return resolver, GuardPolicy(**options)


# --------------------------------------------------------------------------
# a local server whose answers each test describes
# --------------------------------------------------------------------------
class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):                 # HTTPServer.server_bind does a reverse lookup; skip it
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request, client_address):   # a client that hangs up or refuses a certificate is expected
        pass

    @property
    def port(self):
        return self.server_address[1]


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        route = self.server.routes.get(self.path) or self.server.fallback
        if route is None:
            reply(self, 404, b"not found")
        else:
            route(self)

    def log_message(self, *args):
        pass


def reply(handler, status=200, body=b"", headers=(), length="auto"):
    """Send an answer.  ``length``: "auto" (len(body)), None (no Content-Length header) or an int to declare."""
    handler.send_response(status)
    for name, value in headers:
        handler.send_header(name, value)
    if length == "auto":
        handler.send_header("Content-Length", str(len(body)))
    elif length is not None:
        handler.send_header("Content-Length", str(length))
    handler.end_headers()
    handler.wfile.write(body)


def redirect(location, status=302):
    return lambda handler: reply(handler, status, headers=[("Location", location)])


@pytest.fixture
def start_server():
    started = []

    def start(routes=None, fallback=None, tls=None):
        server = _Server(("127.0.0.1", 0), _Handler)
        server.routes, server.fallback, server.seen = dict(routes or {}), fallback, []
        server.release = threading.Event()          # lets a "hanging" route finish when the test ends
        if tls:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(*tls)
            server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        started.append((server, thread))
        return server

    yield start
    for server, thread in started:
        server.release.set()
        server.shutdown()
        server.server_close()
        thread.join(5)


def ok_page(handler):
    reply(handler, 200, b"<html>hello</html>", headers=[("Content-Type", "text/html; charset=utf-8")])


# --------------------------------------------------------------------------
# a throw-away CA and a server certificate for example.test
# --------------------------------------------------------------------------
@pytest.fixture
def tls_files(tmp_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.datetime.now(datetime.timezone.utc)
    window = dict(not_valid_before=now - datetime.timedelta(days=1), not_valid_after=now + datetime.timedelta(days=1))

    def builder(subject, issuer, public_key):
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(issuer).public_key(public_key)
                .serial_number(x509.random_serial_number())
                .not_valid_before(window["not_valid_before"]).not_valid_after(window["not_valid_after"]))

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "SX-05 throw-away test CA")])
    ca_cert = (builder(ca_name, ca_name, ca_key.public_key())
               .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
               .sign(ca_key, hashes.SHA256()))

    key = ec.generate_private_key(ec.SECP256R1())
    cert = (builder(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.test")]), ca_name, key.public_key())
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("example.test")]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))

    pem = serialization.Encoding.PEM
    ca_path, cert_path, key_path = tmp_path / "ca.pem", tmp_path / "server.pem", tmp_path / "server.key"
    ca_path.write_bytes(ca_cert.public_bytes(pem))
    cert_path.write_bytes(cert.public_bytes(pem))
    key_path.write_bytes(key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return str(ca_path), str(cert_path), str(key_path)


# --------------------------------------------------------------------------
# 1. a plain GET
# --------------------------------------------------------------------------
def test_plain_get_returns_final_url_status_headers_and_body(start_server):
    server = start_server({"/page?x=1": ok_page})
    resolver, policy = loopback_policy()
    url = f"http://web.test:{server.port}/page?x=1"
    final_url, status, headers, body = safe_get(url, policy=policy)
    assert (final_url, status, body) == (url, 200, b"<html>hello</html>")
    assert headers["content-type"] == "text/html; charset=utf-8"        # headers are case-insensitive
    # the request carries the NAME in its Host header although the socket went to the checked address
    assert server.seen[0][1]["host"] == f"web.test:{server.port}"
    assert server.seen[0][1]["accept-encoding"] == "gzip, deflate"


def test_ip_literal_url_needs_no_lookup(start_server):
    server = start_server({"/ok": ok_page})
    resolver, policy = loopback_policy({})
    assert safe_get(f"http://127.0.0.1:{server.port}/ok", policy=policy)[3] == b"<html>hello</html>"
    assert resolver.calls == []


def test_safe_stream_reads_in_chunks_and_closes(start_server):
    body = b"x" * 200_000
    server = start_server({"/big": lambda h: reply(h, 200, body)})
    _, policy = loopback_policy()
    with safe_stream(f"http://web.test:{server.port}/big", max_bytes=1_000_000, policy=policy) as response:
        chunks = list(response.iter_bytes())
    assert len(chunks) > 1 and b"".join(chunks) == body


def test_the_default_policy_refuses_a_private_literal_and_localhost_without_opening_a_socket(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a socket was opened")
    monkeypatch.setattr(socket, "socket", refuse)
    for url in ("http://10.0.0.1/", "http://169.254.169.254/latest/meta-data/", "http://localhost/",
                "file:///etc/passwd", "http://example.com:8080/"):
        with pytest.raises(UnsafeURL):
            safe_get(url)


# --------------------------------------------------------------------------
# 2-5. redirects
# --------------------------------------------------------------------------
def test_redirect_to_the_metadata_address_is_refused(start_server):
    server = start_server({"/go": redirect("http://169.254.169.254/latest/meta-data/")})
    _, policy = loopback_policy()
    with pytest.raises(UnsafeURL):
        safe_get(f"http://web.test:{server.port}/go", policy=policy)
    assert [path for path, _ in server.seen] == ["/go"]


def test_redirect_to_a_name_that_resolves_to_a_private_address_is_refused(start_server):
    server = start_server({"/go": lambda h: reply(h, 302, headers=[("Location", f"http://internal.test:{server.port}/")])})
    resolver, policy = loopback_policy({"web.test": ["127.0.0.1"], "internal.test": ["10.0.0.5"]})
    with pytest.raises(UnsafeURL):
        safe_get(f"http://web.test:{server.port}/go", policy=policy)
    assert [path for path, _ in server.seen] == ["/go"]
    assert ("internal.test", server.port) in resolver.calls


@pytest.mark.parametrize("location", ["file:///etc/passwd", "gopher://127.0.0.1/", "ftp://example.com/",
                                      "http://user@example.com/", "//169.254.169.254/", "http://10.0.0.1/", "http://[::ffff:10.0.0.1]/"])
def test_redirect_to_a_forbidden_target_is_refused(start_server, location):
    server = start_server({"/go": redirect(location)})
    _, policy = loopback_policy()
    with pytest.raises(UnsafeURL):
        safe_get(f"http://web.test:{server.port}/go", policy=policy)


def test_relative_redirects_are_followed_and_the_final_url_is_reported(start_server):
    server = start_server({"/a": redirect("/b/c"), "/b/c": redirect("../d?q=1", 301), "/d?q=1": ok_page})
    _, policy = loopback_policy()
    final_url, status, _, body = safe_get(f"http://web.test:{server.port}/a", policy=policy)
    assert final_url == f"http://web.test:{server.port}/d?q=1" and status == 200 and body == b"<html>hello</html>"


def chain(total):
    """Route for /hop/N: redirect to /hop/N+1 until N == total, then answer."""
    def route(handler):
        n = int(handler.path.rsplit("/", 1)[1])
        if n < total:
            redirect(f"/hop/{n + 1}")(handler)
        else:
            ok_page(handler)
    return route


def test_a_redirect_chain_longer_than_the_limit_fails(start_server):
    server = start_server(fallback=chain(total=6))
    _, policy = loopback_policy()
    with pytest.raises(FetchError, match="too many"):
        safe_get(f"http://web.test:{server.port}/hop/0", max_redirects=2, policy=policy)
    assert len(server.seen) == 3                       # the first request and two redirects, no more


def test_a_chain_of_exactly_the_limit_succeeds(start_server):
    server = start_server(fallback=chain(total=2))
    _, policy = loopback_policy()
    assert safe_get(f"http://web.test:{server.port}/hop/0", max_redirects=2, policy=policy)[1] == 200


def test_a_redirect_without_a_location_fails(start_server):
    server = start_server({"/go": lambda h: reply(h, 302)})
    _, policy = loopback_policy()
    with pytest.raises(FetchError):
        safe_get(f"http://web.test:{server.port}/go", policy=policy)


def test_credentials_are_dropped_when_a_redirect_changes_host_and_kept_when_it_does_not(start_server):
    server = start_server({"/same": redirect("/final"), "/other": None, "/final": ok_page})
    server.routes["/other"] = lambda h: reply(h, 302, headers=[("Location", f"http://elsewhere.test:{server.port}/final")])
    _, policy = loopback_policy({"web.test": ["127.0.0.1"], "elsewhere.test": ["127.0.0.1"]})
    headers = {"Authorization": "Bearer secret", "Cookie": "a=b", "X-Keep": "1"}

    safe_get(f"http://web.test:{server.port}/same", headers=headers, policy=policy)
    assert server.seen[-1][1]["authorization"] == "Bearer secret" and server.seen[-1][1]["cookie"] == "a=b"

    safe_get(f"http://web.test:{server.port}/other", headers=headers, policy=policy)
    final = server.seen[-1][1]
    assert final["host"] == f"elsewhere.test:{server.port}"
    assert "authorization" not in final and "cookie" not in final and final["x-keep"] == "1"


# --------------------------------------------------------------------------
# 6. DNS rebinding: the answer is judged again at connect time
# --------------------------------------------------------------------------
def test_dns_rebinding_is_refused_at_connect_time(start_server):
    server = start_server({"/ok": ok_page})
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])      # public for the early check, loopback for the connect
    calls = []

    def resolver(host, port):
        calls.append(host)
        return next(answers)

    policy = GuardPolicy(resolver=resolver, ports=None)      # the default blocklist: loopback is blocked
    with pytest.raises(UnsafeURL):
        safe_get(f"http://rebind.test:{server.port}/ok", policy=policy)
    assert calls == ["rebind.test", "rebind.test"]           # looked up at the early check AND at connect time
    assert server.seen == []                                 # nothing reached the loopback server


def test_one_bad_answer_among_good_ones_rejects_the_name(start_server):
    server = start_server({"/ok": ok_page})
    resolver, policy = loopback_policy({"mixed.test": ["127.0.0.1", "10.0.0.5"]})
    with pytest.raises(UnsafeURL):
        safe_get(f"http://mixed.test:{server.port}/ok", policy=policy)
    assert server.seen == []


def test_a_name_that_does_not_resolve_is_a_refusal(start_server):
    _, policy = loopback_policy({})
    with pytest.raises(UnsafeURL):
        safe_get("http://nowhere.test/", policy=policy)


# --------------------------------------------------------------------------
# 7. size
# --------------------------------------------------------------------------
def test_content_length_over_the_cap_is_refused_before_the_body_is_read(start_server):
    server = start_server({"/big": lambda h: reply(h, 200, b"x" * 10, length=10_000_000)})
    _, policy = loopback_policy()
    with pytest.raises(ResponseTooLarge):
        safe_get(f"http://web.test:{server.port}/big", max_bytes=1000, policy=policy)


def test_a_body_over_the_cap_without_content_length_is_refused(start_server):
    server = start_server({"/big": lambda h: reply(h, 200, b"x" * 5000, length=None)})
    _, policy = loopback_policy()
    with pytest.raises(ResponseTooLarge):
        safe_get(f"http://web.test:{server.port}/big", max_bytes=1000, policy=policy)


def test_a_body_exactly_at_the_cap_is_accepted_and_one_byte_more_is_not(start_server):
    server = start_server({"/a": lambda h: reply(h, 200, b"x" * 1000, length=None),
                           "/b": lambda h: reply(h, 200, b"x" * 1001, length=None)})
    _, policy = loopback_policy()
    assert len(safe_get(f"http://web.test:{server.port}/a", max_bytes=1000, policy=policy)[3]) == 1000
    with pytest.raises(ResponseTooLarge):
        safe_get(f"http://web.test:{server.port}/b", max_bytes=1000, policy=policy)


def test_the_read_argument_overrides_the_stream_cap(start_server):
    server = start_server({"/a": lambda h: reply(h, 200, b"x" * 5000)})
    _, policy = loopback_policy()
    with safe_stream(f"http://web.test:{server.port}/a", max_bytes=100_000, policy=policy) as response:
        with pytest.raises(ResponseTooLarge):
            response.read(1000)


def test_the_cap_counts_decompressed_bytes(start_server):
    bomb = gzip.compress(b"\0" * 20_000_000)                 # about 20 KB on the wire, 20 MB decoded
    assert len(bomb) < 100_000
    server = start_server({"/bomb": lambda h: reply(h, 200, bomb, headers=[("Content-Encoding", "gzip")])})
    _, policy = loopback_policy()
    with pytest.raises(ResponseTooLarge):
        safe_get(f"http://web.test:{server.port}/bomb", max_bytes=1_000_000, policy=policy)


def test_a_large_gzip_page_is_returned_in_full(start_server):
    page = b"".join(b"line %d of a page that compresses well\n" % n for n in range(60_000))     # about 2.4 MB
    packed = gzip.compress(page)
    server = start_server({"/z": lambda h: reply(h, 200, packed, headers=[("Content-Encoding", "gzip")])})
    _, policy = loopback_policy()
    body = safe_get(f"http://web.test:{server.port}/z", max_bytes=5_000_000, policy=policy)[3]
    assert body == page


def test_a_chunked_body_is_read_in_full(start_server):
    parts = [b"a" * 70_000, b"b" * 10, b"c" * 5000]

    def chunked(handler):
        handler.protocol_version = "HTTP/1.1"
        handler.send_response(200)
        handler.send_header("Transfer-Encoding", "chunked")
        handler.send_header("Connection", "close")
        handler.end_headers()
        for part in parts:
            handler.wfile.write(b"%x\r\n" % len(part) + part + b"\r\n")
        handler.wfile.write(b"0\r\n\r\n")

    server = start_server({"/c": chunked})
    _, policy = loopback_policy()
    assert safe_get(f"http://web.test:{server.port}/c", policy=policy)[3] == b"".join(parts)


def test_a_truncated_body_is_a_fetch_error(start_server):
    server = start_server({"/short": lambda h: reply(h, 200, b"x" * 10, length=100)})
    _, policy = loopback_policy()
    with pytest.raises(FetchError):
        safe_get(f"http://web.test:{server.port}/short", policy=policy)


def test_a_gzip_page_under_the_cap_is_decoded(start_server):
    packed = gzip.compress(b"<html>hello</html>")
    server = start_server({"/z": lambda h: reply(h, 200, packed, headers=[("Content-Encoding", "gzip")])})
    _, policy = loopback_policy()
    assert safe_get(f"http://web.test:{server.port}/z", policy=policy)[3] == b"<html>hello</html>"


# --------------------------------------------------------------------------
# 8. content type and status
# --------------------------------------------------------------------------
def test_a_disallowed_content_type_is_refused(start_server):
    server = start_server({"/img": lambda h: reply(h, 200, b"\x89PNG", headers=[("Content-Type", "image/png")])})
    _, policy = loopback_policy()
    with pytest.raises(FetchError, match="content type"):
        safe_get(f"http://web.test:{server.port}/img", allowed_content_types=("text/html",), policy=policy)


def test_an_allowed_content_type_passes_with_parameters_and_in_any_case(start_server):
    server = start_server({"/ok": lambda h: reply(h, 200, b"hi", headers=[("Content-Type", "Text/HTML; charset=UTF-8")])})
    _, policy = loopback_policy()
    assert safe_get(f"http://web.test:{server.port}/ok", allowed_content_types=("text/html",), policy=policy)[3] == b"hi"


def test_a_missing_content_type_is_allowed(start_server):
    server = start_server({"/ok": lambda h: reply(h, 200, b"hi")})
    _, policy = loopback_policy()
    assert safe_get(f"http://web.test:{server.port}/ok", allowed_content_types=("text/html",), policy=policy)[3] == b"hi"


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
def test_an_error_status_is_a_fetch_error(start_server, status):
    server = start_server({"/x": lambda h: reply(h, status, b"nope")})
    _, policy = loopback_policy()
    with pytest.raises(FetchError):
        safe_get(f"http://web.test:{server.port}/x", policy=policy)


# --------------------------------------------------------------------------
# 9. time
# --------------------------------------------------------------------------
def test_a_server_that_never_answers_is_a_fetch_timeout_and_a_requests_timeout(start_server):
    server = start_server({"/hang": lambda h: h.server.release.wait(10)})
    _, policy = loopback_policy()
    started = time.monotonic()
    with pytest.raises(FetchTimeout) as caught:
        safe_get(f"http://web.test:{server.port}/hang", timeout=0.3, policy=policy)
    assert time.monotonic() - started < 5
    error = caught.value
    assert isinstance(error, requests.exceptions.Timeout)
    assert isinstance(error, FetchError) and isinstance(error, ValueError)
    assert error.response is None and error.request is None          # a real requests exception, fully initialised


def test_the_total_deadline_stops_a_server_that_drips_bytes(start_server):
    def drip(handler):
        handler.send_response(200)
        handler.end_headers()
        for _ in range(200):
            if handler.server.release.wait(0.05):
                return
            handler.wfile.write(b"x")
            handler.wfile.flush()

    server = start_server({"/drip": drip})
    _, policy = loopback_policy()
    started = time.monotonic()
    with pytest.raises(FetchTimeout):
        safe_get(f"http://web.test:{server.port}/drip", timeout=5, total_timeout=0.5, policy=policy)
    assert time.monotonic() - started < 4


def test_a_total_timeout_that_is_already_over_fails_before_any_connection(start_server):
    server = start_server({"/ok": ok_page})
    _, policy = loopback_policy()
    with pytest.raises(FetchTimeout):
        safe_get(f"http://web.test:{server.port}/ok", total_timeout=0, policy=policy)
    assert server.seen == []


# --------------------------------------------------------------------------
# connection failures and messages
# --------------------------------------------------------------------------
def test_a_refused_connection_is_a_fetch_error_and_the_message_hides_the_address():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]                         # nothing listens here once the socket is closed
    _, policy = loopback_policy()
    with pytest.raises(FetchError) as caught:
        safe_get(f"http://web.test:{port}/", policy=policy)
    text = str(caught.value)
    assert not isinstance(caught.value, (UnsafeURL, FetchTimeout))
    assert "127.0.0.1" not in text and "web.test" not in text and str(port) not in text


def test_error_messages_never_echo_the_url_a_host_or_an_address(start_server):
    server = start_server({"/go": redirect("http://169.254.169.254/secret-path"), "/404": None})
    _, policy = loopback_policy({"web.test": ["127.0.0.1"], "internal.test": ["10.0.0.5"]})
    cases = [f"http://web.test:{server.port}/go", f"http://web.test:{server.port}/404",
             f"http://internal.test:{server.port}/secret-path", "http://10.0.0.1/secret-path"]
    for url in cases:
        with pytest.raises(ValueError) as caught:
            safe_get(url, policy=policy)
        text = str(caught.value)
        for leaked in ("web.test", "internal.test", "10.0.0", "169.254", "127.0.0.1", "secret-path"):
            assert leaked not in text


def test_environment_proxy_variables_are_ignored(start_server, monkeypatch):
    server = start_server({"/ok": ok_page})
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:1")      # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    _, policy = loopback_policy()
    assert safe_get(f"http://web.test:{server.port}/ok", policy=policy)[1] == 200


@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"host": "x"}, {"X-A": "b\r\nX-B: c"}, {"X-A\n": "b"},
                                     {"X-A": "b\0"}, {"": "b"}, {"X-A": 1}])
def test_unsafe_request_headers_are_refused(headers):
    with pytest.raises(ValueError, match="invalid request header"):
        safe_get("http://example.com/", headers=headers)


# --------------------------------------------------------------------------
# 10. TLS
# --------------------------------------------------------------------------
def test_https_succeeds_when_the_certificate_chains_to_the_given_ca(start_server, tls_files):
    ca, cert, key = tls_files
    server = start_server({"/ok": ok_page}, tls=(cert, key))
    _, policy = loopback_policy({"example.test": ["127.0.0.1"]}, ca_certs=ca)
    final_url, status, _, body = safe_get(f"https://example.test:{server.port}/ok", policy=policy)
    assert status == 200 and body == b"<html>hello</html>" and final_url.startswith("https://example.test:")
    assert server.seen[0][1]["host"] == f"example.test:{server.port}"


def test_https_fails_when_the_host_name_does_not_match_the_certificate(start_server, tls_files):
    ca, cert, key = tls_files
    server = start_server({"/ok": ok_page}, tls=(cert, key))
    _, policy = loopback_policy({"other.test": ["127.0.0.1"]}, ca_certs=ca)
    with pytest.raises(FetchError, match="secure connection"):
        safe_get(f"https://other.test:{server.port}/ok", policy=policy)
    assert server.seen == []                                 # no request was sent over the unverified connection


def test_https_fails_with_the_default_certificate_store(start_server, tls_files):
    ca, cert, key = tls_files
    server = start_server({"/ok": ok_page}, tls=(cert, key))
    _, policy = loopback_policy({"example.test": ["127.0.0.1"]})          # ca_certs=None: the certifi bundle
    with pytest.raises(FetchError, match="secure connection"):
        safe_get(f"https://example.test:{server.port}/ok", policy=policy)
    assert server.seen == []


def test_an_https_redirect_to_a_forbidden_address_is_refused(start_server, tls_files):
    ca, cert, key = tls_files
    server = start_server({"/go": redirect("https://[::1]/")}, tls=(cert, key))
    _, policy = loopback_policy({"example.test": ["127.0.0.1"]}, ca_certs=ca)
    with pytest.raises(UnsafeURL):
        safe_get(f"https://example.test:{server.port}/go", policy=policy)
