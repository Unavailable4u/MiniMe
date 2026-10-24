"""
m2c/tests/auth/mock_server.py -- Patch C03: a scripted stand-in for the S03
device endpoints, plus the helpers the auth tests share.

The server is a real ``http.server`` on 127.0.0.1 (random port), so the tests
drive the genuine ``urllib`` code path in m2c.auth.login: sockets, headers,
redirects, dropped connections and all.  Time is the only thing faked (see
``FakeTime``): the server answers instantly, and the client's sleeps and
deadline run on a clock the test controls, so a "10 minute" login takes
milliseconds.

Scripting.  ``server.script("pair/poll", reply1, reply2, ...)`` queues the
replies for one path; each request pops the next.  When a script runs dry the
request is recorded in ``server.unscripted`` and answered with a 500, and the
``server`` fixture fails the test -- an unexpected extra call is a bug in
either the test or the client.  ``repeat_last=True`` keeps answering with the
final reply (for "pending forever").

The secret constants below are what the mock server hands out.  Each is
distinctive so a leak is unmistakable, and each satisfies the real S03 shapes
(token_urlsafe(32)-like for the codes, three dot-separated parts for the JWT,
a UUID for the device id).
"""
from __future__ import annotations

import json
import socket
import threading
import traceback
import types
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
from typing import Any, Callable

# --------------------------------------------------------------------------
# The three secrets in flight (build plan S03), plus the non-secret values.
# --------------------------------------------------------------------------
DEVICE_CODE = "DEVCODE-Zk3fQ9vL1mT7xR2wY8nB5cHj0sUaPdE4gI6o"
CREDENTIAL = "CRED-Qw8rT2yU5iO9pA3sD7fG1hJ4kL6zX0cV2bN7mQ"
JWT = ".".join(
    base64.urlsafe_b64encode(json.dumps(part, separators=(",", ":")).encode()).rstrip(b"=").decode()
    for part in ({"alg": "none", "typ": "JWT"}, {"sub": "test-user", "exp": 4102444800})
) + ".fake-signature"
SECRETS = (DEVICE_CODE, CREDENTIAL, JWT)

DEVICE_ID = "5b1f2c3e-8a47-4d09-9c61-2f7e0a1b3c4d"
USER_CODE = "BCDF-GHJK"
VERIFICATION_URL = "https://app.example.test/settings/devices"

START_PATH = "v1/code/devices/pair/start"
POLL_PATH = "v1/code/devices/pair/poll"
TOKEN_PATH = "v1/code/devices/token"


# --------------------------------------------------------------------------
# Replies
# --------------------------------------------------------------------------
@dataclass
class Reply:
    status: int = 200
    body: Any = None  # dict/list -> JSON; str -> text; bytes -> raw
    headers: dict[str, str] = field(default_factory=dict)


class _Drop:
    """Close the connection without answering (a network failure)."""

    def __repr__(self) -> str:
        return "DROP"


DROP = _Drop()


def ok(body: Any) -> Reply:
    return Reply(200, body)


def error(status: int, body: Any = None, **headers: str) -> Reply:
    return Reply(status, {"detail": "error"} if body is None else body, dict(headers))


def redirect(location: str, status: int = 307) -> Reply:
    return Reply(status, b"", {"Location": location})


def start_reply(**overrides: Any) -> Reply:
    body = {
        "device_code": DEVICE_CODE,
        "user_code": USER_CODE,
        "verification_url": VERIFICATION_URL,
        "interval": 5,
        "expires_in": 600,
    }
    body.update(overrides)
    return ok({k: v for k, v in body.items() if v is not None})


def pending() -> Reply:
    return ok({"status": "pending"})


def approved(**overrides: Any) -> Reply:
    body = {"status": "approved", "device_credential": CREDENTIAL, "device_id": DEVICE_ID}
    body.update(overrides)
    return ok({k: v for k, v in body.items() if v is not None})


def denied() -> Reply:
    return ok({"status": "denied"})


def expired() -> Reply:
    return ok({"status": "expired"})


def token_reply(expires_in: Any = 900, jwt: Any = JWT) -> Reply:
    return ok({"jwt": jwt, "expires_in": expires_in})


@dataclass
class Recorded:
    path: str
    headers: dict[str, str]
    raw: bytes
    json: Any


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------
class MockServer:
    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        self.unscripted: list[str] = []
        self._scripts: dict[str, list[Any]] = {}
        self._repeat: dict[str, bool] = {}
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:  # silence
                pass

            def do_POST(self) -> None:  # noqa: N802 (http.server's naming)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                owner._handle(self, raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        # poll_interval: shutdown() waits for the serve loop to notice, so the default
        # 0.5 s would add half a second to every test that starts a server.
        self._thread = threading.Thread(
            target=lambda: self.httpd.serve_forever(poll_interval=0.01), daemon=True
        )

    # -- lifecycle -------------------------------------------------------
    def start(self) -> "MockServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    # -- scripting -------------------------------------------------------
    def script(self, path: str, *replies: Any, repeat_last: bool = False) -> None:
        with self._lock:
            self._scripts.setdefault(path.strip("/"), []).extend(replies)
            self._repeat[path.strip("/")] = repeat_last

    def count(self, path: str) -> int:
        return sum(1 for r in self.requests if r.path == path.strip("/"))

    def bodies(self, path: str) -> list[Any]:
        return [r.json for r in self.requests if r.path == path.strip("/")]

    # -- handling --------------------------------------------------------
    def _next(self, path: str, recorded: Recorded) -> Any:
        with self._lock:
            queue = self._scripts.get(path, [])
            if not queue:
                self.unscripted.append(path)
                return Reply(500, {"detail": "unscripted"})
            item = queue[0] if (len(queue) == 1 and self._repeat.get(path)) else queue.pop(0)
        return item(recorded) if callable(item) else item

    def _handle(self, handler: BaseHTTPRequestHandler, raw: bytes) -> None:
        path = handler.path.strip("/")
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        recorded = Recorded(path, {k.lower(): v for k, v in handler.headers.items()}, raw, parsed)
        with self._lock:
            self.requests.append(recorded)
        item = self._next(path, recorded)

        if item is DROP:
            handler.close_connection = True
            try:
                handler.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            return

        if isinstance(item.body, (dict, list)):
            payload, ctype = json.dumps(item.body).encode(), "application/json"
        elif isinstance(item.body, str):
            payload, ctype = item.body.encode(), "text/plain"
        else:
            payload, ctype = (item.body or b""), "application/octet-stream"
        handler.send_response(item.status)
        handler.send_header("Content-Type", ctype)
        handler.send_header("Content-Length", str(len(payload)))
        for key, value in item.headers.items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(payload)


# --------------------------------------------------------------------------
# Fake time
# --------------------------------------------------------------------------
class FakeTime:
    """A monotonic clock that only moves when the code under test sleeps."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeKeyring:
    """Just enough of the ``keyring`` module's surface for credentials.py.

    ``priority`` mimics the real backends: viable ones report > 0, keyring's
    "fail" backend (what WSL2 usually ends up with) reports 0.
    """

    def __init__(self, priority: int = 5) -> None:
        self.store: dict[tuple[str, str], str] = {}
        self.calls: list[tuple[str, str, str]] = []
        self.fail_get: BaseException | None = None
        self.fail_set: BaseException | None = None
        self.fail_delete: BaseException | None = None
        self._backend = types.SimpleNamespace(priority=priority)

    def get_keyring(self):
        return self._backend

    def set_password(self, service: str, username: str, password: str) -> None:
        self.calls.append(("set", service, username))
        if self.fail_set:
            raise self.fail_set
        self.store[(service, username)] = password

    def get_password(self, service: str, username: str):
        self.calls.append(("get", service, username))
        if self.fail_get:
            raise self.fail_get
        return self.store.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self.calls.append(("delete", service, username))
        if self.fail_delete:
            raise self.fail_delete
        self.store.pop((service, username), None)


def closed_port_url() -> str:
    """A loopback URL nothing listens on: connections are refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


# --------------------------------------------------------------------------
# Leak checking
# --------------------------------------------------------------------------
def _is_code_under_test(filename: str) -> bool:
    """Frames of the m2c package itself (not tests, not the standard library)."""
    norm = filename.replace("\\", "/")
    return "/m2c/" in norm and "/tests/" not in norm


def exception_texts(exc: BaseException) -> list[str]:
    """Every way an exception can be turned into text: str, repr, the plain
    traceback, the instance's attributes, and the traceback WITH local
    variables (what error trackers and ``pytest --showlocals`` print).

    The locals-capturing rendering is restricted to frames of the m2c package
    (and followed down the __cause__/__context__ chain).  The test function
    that catches the exception is also on the traceback, and its own locals
    (the secrets a test deliberately plants) are not what is being checked.
    """
    texts = [str(exc), repr(exc), repr(vars(exc)), repr(exc.args)]
    texts.append("".join(traceback.format_exception(exc)))

    pending = [traceback.TracebackException.from_exception(exc, capture_locals=True)]
    while pending:
        current = pending.pop()
        frames = [f for f in current.stack if _is_code_under_test(f.filename)]
        for frame in frames:
            texts.append(f"{frame.filename}:{frame.lineno} {frame.name} locals={frame.locals!r}")
        for chained in (current.__cause__, current.__context__):
            if chained is not None:
                pending.append(chained)
    return texts


def assert_no_secrets(*texts: str, secrets: tuple[str, ...] = SECRETS) -> None:
    for text in texts:
        for secret in secrets:
            assert secret not in text, f"secret leaked into: {text[:300]!r}"


def log_texts(caplog) -> list[str]:
    """Everything in the captured log records, in every form a handler could
    read it: the formatted text, the message, the args, the raw record dict
    (which is where ``extra=`` fields live) and any attached exception."""
    texts = [caplog.text]
    for record in caplog.records:
        texts.append(record.getMessage())
        texts.append(repr(record.args))
        texts.append(repr(vars(record)))
        if record.exc_info:
            texts.append("".join(traceback.format_exception(*record.exc_info)))
    return texts


Check = Callable[[], None]
