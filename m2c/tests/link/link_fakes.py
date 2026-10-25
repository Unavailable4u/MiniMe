"""
m2c/tests/link/link_fakes.py -- Patch C04: a fake device server and the
helpers the link tests share.

Why this is a plain module and not a conftest.py: tests/test_doctor.py does
``from conftest import ...``, which resolves by module name, and a second
conftest.py in tests/link would shadow the real one (the same reason
tests/auth/auth_fixtures.py exists).  Each test file imports what it needs
from here; the autouse fixtures below are re-exported with ``import *`` and so
apply only to the link tests.

The fake server
---------------
``FakeServer`` is a real ``websockets`` server on 127.0.0.1 with an ephemeral
port, so the client under test runs its genuine network code: handshake,
frames, abrupt connection loss, close codes.  Each connection is handled by a
*script* -- an ``async def script(conn)`` -- chosen by connection number, so a
test says exactly what the server does on the first connection, the second,
and so on.  Ready-made scripts:

    fixture_script(lines)   replays one connection of a P00 golden transcript:
                            for every client line it waits for the client's
                            message and compares it (type, run_id, payload;
                            ids and timestamps are not compared); for every
                            server line it sends it verbatim.
    ReplayRun(events)       a tiny model of the server's resume rule
                            (protocol/README.md section 5): it holds a run's
                            whole event stream and, on run.start or
                            run.resume{last_seq}, delivers the events with
                            seq > last_seq verbatim.  It can kill the
                            connection mid-stream, redeliver old events
                            (duplicates) and withhold one (a gap).
    handshake_ok / reject / ...   the handshake variants.

Every client message the server receives is recorded, and -- when
``jsonschema`` is installed -- validated against the schemas in the CLIENT'S
OWN COPY (m2c/src/m2c/protocol/v1).  ``server.assert_clean()`` fails the test
on any script error, mismatch against a golden line, or schema problem.  CI's
m2c job installs only ``.[dev]``, so without jsonschema that last check is
skipped; the dedicated schema test in test_link_requests.py uses
``pytest.importorskip`` so the skip is visible.

Fake secrets are built at run time (this repo's gitleaks scan flags anything
shaped like a key): see ``make_credential``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

import m2c
from m2c.auth.credentials import Credential, Secret
from m2c.link import DeviceLink, LinkCallbacks
from m2c.store import create_session, open_db, read_events

PKG_DIR = Path(m2c.__file__).resolve().parent
SCHEMA_COPY = PKG_DIR / "protocol" / "v1"
REPO_ROOT = Path(__file__).resolve().parents[3]          # m2c/tests/link/<this file>
FIXTURES_DIR = REPO_ROOT / "protocol" / "fixtures"

CLIENT_TYPES = frozenset(
    {"hello", "run.start", "user_message", "tool_result", "tool_stream", "approval", "cancel", "run.resume", "ping"}
)
STREAM_TYPES = frozenset({"run_event", "tool_proposed", "budget_warning"})
PROJECT = "/home/user/code/app"


# --------------------------------------------------------------------------
# Autouse fixtures (re-exported into each test module with ``import *``)
# --------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _link_loopback_is_never_proxied(monkeypatch):
    """websockets honours https_proxy/no_proxy; CI sandboxes and corporate
    machines set them, and the fake server lives on 127.0.0.1."""
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy", "WSS_PROXY", "WS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("M2C_SERVER_URL", raising=False)      # a developer's own setting must not decide a test
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")


@pytest.fixture
def m2c_logs(caplog):
    """caplog attached directly to the ``m2c`` logger at DEBUG, so the tests
    read the RAW records (no ContentFreeFilter in the way): stricter than what a
    user's terminal would show."""
    logger = logging.getLogger("m2c")
    saved = (logger.level, logger.propagate)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(caplog.handler)
    caplog.set_level(logging.DEBUG, logger="m2c")
    try:
        yield caplog
    finally:
        logger.removeHandler(caplog.handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


# --------------------------------------------------------------------------
# Small builders
# --------------------------------------------------------------------------
def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def env(t: str, payload: dict, *, run_id: str | None = None, seq: int | None = None) -> dict:
    out: dict[str, Any] = {"v": 1, "t": t, "id": str(uuid.uuid4()), "ts": now_ts(), "payload": payload}
    if run_id is not None:
        out["run_id"] = run_id
    if seq is not None:
        out["seq"] = seq
    return out


def welcome_env(*, protocol: int = 1, min_protocol: int = 1, max_message_bytes: int | None = 1_048_576, **over) -> dict:
    limits: dict[str, Any] = {"max_parallel_runs": 3}
    if max_message_bytes is not None:
        limits["max_message_bytes"] = max_message_bytes
    payload = {"protocol": protocol, "min_protocol": min_protocol, "token_ttl_s": 900, "limits": limits,
               "server_version": "fake-1"}
    payload.update(over)
    return env("welcome", payload)


def error_env(code: str, *, message: str = "fixed text", run_id: str | None = None,
              ref_id: str | None = None, details: dict | None = None) -> dict:
    payload: dict[str, Any] = {"code": code, "message": message}
    if ref_id is not None:
        payload["ref_id"] = ref_id
    if details:
        payload["details"] = details
    return env("error", payload, run_id=run_id)


def make_credential(server_url: str, text: str | None = None) -> Credential:
    """A credential in the real shape, built at run time so no secret-looking
    literal sits in the source."""
    text = text or ("m2c-test-cred-" + "x" * 30)
    return Credential(device_credential=Secret(text), device_id=str(uuid.uuid4()), server_url=server_url)


def synthetic_events(run_id: str, n: int) -> list[dict]:
    """A valid server stream of exactly ``n`` events, seq 1..n: created, running,
    n-3 text deltas, completed."""
    assert n >= 4
    events: list[dict] = []

    def add(payload: dict) -> None:
        events.append(env("run_event", payload, run_id=run_id, seq=len(events) + 1))

    add({"type": "state_changed", "state": "created"})
    add({"type": "state_changed", "state": "running"})
    for i in range(n - 3):
        add({"type": "text_delta", "text": f"chunk {i} "})
    add({"type": "state_changed", "state": "completed"})
    assert len(events) == n
    return events


# --------------------------------------------------------------------------
# Golden fixtures
# --------------------------------------------------------------------------
def fixture_lines(name: str) -> list[dict]:
    path = FIXTURES_DIR / name
    if not path.is_file():
        pytest.skip(f"{path} not found (running from an sdist, outside the repository)")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def split_connections(lines: list[dict]) -> list[list[dict]]:
    """A transcript is the record of the messages delivered on a device's
    connections; a ``hello`` starts a new connection (protocol/README.md 9)."""
    groups: list[list[dict]] = []
    for line in lines:
        if line["t"] == "hello":
            groups.append([])
        groups[-1].append(line)
    return groups


def stream_lines(lines: list[dict]) -> list[dict]:
    """The server's replayable stream: the lines that carry a seq."""
    return [m for m in lines if "seq" in m]


def fixture_script(group: list[dict], *, kill_at_end: bool = False):
    async def script(conn: "FakeConn") -> None:
        for line in group:
            if line["t"] in CLIENT_TYPES:
                got = await conn.recv(timeout=8.0)
                conn.server.compare(line, got, conn.index)
            else:
                await conn.send(line)
        conn.script_done = True
        if kill_at_end:
            await conn.kill()

    return script


# --------------------------------------------------------------------------
# Schema validation with the client's own copy of the schemas
# --------------------------------------------------------------------------
def build_schema_validator() -> Callable[[dict], list[str]] | None:
    """A function returning the schema problems of one envelope, built from
    m2c/src/m2c/protocol/v1; None when jsonschema is not installed."""
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError:
        return None
    base = "https://m2c.invalid/protocol/v1/"
    registry = Registry()
    for path in sorted(SCHEMA_COPY.rglob("*.json")):
        rel = path.relative_to(SCHEMA_COPY).as_posix()
        doc = json.loads(path.read_text(encoding="utf-8"))
        registry = registry.with_resource(base + rel, Resource.from_contents(doc, default_specification=DRAFT202012))
    envelope = json.loads((SCHEMA_COPY / "envelope.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(envelope, registry=registry)
    return lambda msg: [f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message[:160]}"
                        for e in validator.iter_errors(msg)]


# --------------------------------------------------------------------------
# The server
# --------------------------------------------------------------------------
class FakeConn:
    def __init__(self, ws, index: int, server: "FakeServer") -> None:
        self.ws = ws
        self.index = index
        self.server = server
        self.received: list[dict] = []
        self.script_done = False
        self.close_code: int | None = None
        self.closed = asyncio.Event()

    async def recv(self, timeout: float | None = 5.0) -> dict:
        raw = await (self.ws.recv() if timeout is None else asyncio.wait_for(self.ws.recv(), timeout))
        msg = json.loads(raw)
        self.received.append(msg)
        self.server.client_messages.append((self.index, msg))
        if self.server.validator is not None:
            problems = self.server.validator(msg)
            if problems:
                self.server.schema_problems.append((self.index, msg.get("t"), problems))
        return msg

    async def send(self, message: dict | str) -> None:
        await self.ws.send(message if isinstance(message, str) else json.dumps(message, separators=(",", ":")))

    async def kill(self, settle_s: float = 0.03) -> None:
        """Drop the TCP connection with no close frame (a network failure).
        The short pause lets the client read what was already sent, so a kill
        after N events means the client really saw about N events."""
        await asyncio.sleep(settle_s)
        self.ws.transport.abort()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        await self.ws.close(code, reason)

    async def drain(self) -> None:
        try:
            while True:
                await self.recv(timeout=None)
        except ConnectionClosed:
            return


class FakeServer:
    """``async with FakeServer(script1, script2, ...) as srv:`` -- connection
    number i is handled by scripts[i]; ``repeat_last=True`` reuses the last
    script for every later connection, otherwise an extra connection is an
    error (an unexpected reconnect is a bug in the test or the client)."""

    def __init__(self, *scripts, repeat_last: bool = False, validate: bool = True) -> None:
        self._scripts = list(scripts)
        self._repeat_last = repeat_last
        self.validator = build_schema_validator() if validate else None
        self.connections: list[FakeConn] = []
        self.client_messages: list[tuple[int, dict]] = []
        self.errors: list[str] = []
        self.schema_problems: list[tuple[int, Any, list[str]]] = []
        self._server = None

    async def __aenter__(self) -> "FakeServer":
        self._server = await serve(self._handle, "127.0.0.1", 0, ping_interval=None, compression=None)
        return self

    async def __aexit__(self, *exc) -> None:
        self._server.close()
        await self._server.wait_closed()

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    @property
    def server_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def messages(self, conn_index: int | None = None, t: str | None = None) -> list[dict]:
        return [m for i, m in self.client_messages
                if (conn_index is None or i == conn_index) and (t is None or m["t"] == t)]

    def compare(self, expected: dict, got: dict, conn_index: int) -> None:
        where = f"connection {conn_index}, expected {expected['t']}"
        if got.get("t") != expected["t"]:
            self.errors.append(f"{where}: got {got.get('t')}")
        elif got.get("run_id") != expected.get("run_id"):
            self.errors.append(f"{where}: run_id {got.get('run_id')!r} != {expected.get('run_id')!r}")
        elif "seq" in got:
            self.errors.append(f"{where}: a client message must not carry seq")
        elif got.get("payload") != expected["payload"]:
            self.errors.append(f"{where}: payload {got.get('payload')!r} != {expected['payload']!r}")

    def assert_clean(self) -> None:
        assert self.errors == [], self.errors
        assert self.schema_problems == [], self.schema_problems

    async def _handle(self, ws) -> None:
        index = len(self.connections)
        conn = FakeConn(ws, index, self)
        self.connections.append(conn)
        if index < len(self._scripts):
            script = self._scripts[index]
        elif self._repeat_last and self._scripts:
            script = self._scripts[-1]
        else:
            self.errors.append(f"unscripted connection #{index}")
            await ws.close(1011)
            return
        try:
            await script(conn)
            await conn.drain()
        except ConnectionClosed:
            pass
        except asyncio.CancelledError:
            raise
        except BaseException as exc:  # noqa: BLE001 - recorded, asserted by assert_clean
            self.errors.append(f"connection {index}: {type(exc).__name__}: {exc}")
        finally:
            conn.close_code = ws.close_code
            conn.closed.set()


# --------------------------------------------------------------------------
# Handshake scripts
# --------------------------------------------------------------------------
async def do_handshake(conn: FakeConn, welcome: dict | None = None) -> dict:
    hello = await conn.recv()
    assert hello["t"] == "hello", hello["t"]
    await conn.send(welcome if welcome is not None else welcome_env())
    return hello


def idle_script(welcome: dict | None = None):
    """Hello, welcome, then answer pings and otherwise stay quiet."""

    async def script(conn: FakeConn) -> None:
        await do_handshake(conn, welcome)
        await answer_pings(conn)

    return script


async def answer_pings(conn: FakeConn) -> None:
    while True:
        msg = await conn.recv(timeout=None)
        if msg["t"] == "ping":
            await conn.send(env("pong", {} if "nonce" not in msg["payload"] else {"nonce": msg["payload"]["nonce"]}))


def reject_script(code: str, close_code: int | None, *, details: dict | None = None, message: str = "fixed text"):
    """Hello, then an ``error`` and the close frame (what S04 does)."""

    async def script(conn: FakeConn) -> None:
        await conn.recv()
        await conn.send(error_env(code, message=message, details=details))
        if close_code is not None:
            await conn.close(close_code)

    return script


def close_only_script(close_code: int):
    async def script(conn: FakeConn) -> None:
        await conn.recv()
        await conn.close(close_code)

    return script


def silent_script():
    async def script(conn: FakeConn) -> None:
        await conn.recv()
        await conn.ws.wait_closed()          # say nothing until the client gives up and closes

    return script


def raw_welcome_script(payload: Any):
    """Hello, then whatever ``payload`` is, sent as the welcome (for malformed ones)."""

    async def script(conn: FakeConn) -> None:
        await conn.recv()
        await conn.send(payload if isinstance(payload, str) else json.dumps(payload))
        await answer_pings(conn)

    return script


# --------------------------------------------------------------------------
# A model of the server's replay rule
# --------------------------------------------------------------------------
class ReplayRun:
    """One run's event stream, delivered the way protocol/README.md section 5
    says: run.start begins it, run.resume{last_seq} re-delivers every event
    with seq > last_seq verbatim.

    kill_after     per connection: how many events to send before killing it
                   (``None`` or past the end of the list: never)
    resume_overlap on a resume, start this many events EARLIER than the rule
                   says, i.e. redeliver duplicates
    drop_once      seqs withheld the first time they would be sent (a gap)
    delay_s        pause between events (lets a test kill a process mid-stream)
    """

    def __init__(self, events: list[dict], *, kill_after: list[int | None] | None = None,
                 resume_overlap: int = 0, drop_once: tuple[int, ...] = (), delay_s: float = 0.0,
                 welcome: dict | None = None) -> None:
        self.events = events
        self.kill_after = list(kill_after or [])
        self.resume_overlap = resume_overlap
        self.drop_once = set(drop_once)
        self.delay_s = delay_s
        self.welcome = welcome
        self.resume_requests: list[tuple[int, int]] = []     # (connection index, last_seq)
        self.start_requests = 0

    async def script(self, conn: FakeConn) -> None:
        await do_handshake(conn, self.welcome)
        kill_at = self.kill_after[conn.index] if conn.index < len(self.kill_after) else None
        sent = 0
        while True:
            msg = await conn.recv(timeout=None)
            if msg["t"] == "ping":
                await conn.send(env("pong", {} if "nonce" not in msg["payload"] else {"nonce": msg["payload"]["nonce"]}))
                continue
            if msg["t"] == "run.start":
                self.start_requests += 1
                begin = 0
            elif msg["t"] == "run.resume":
                last = msg["payload"]["last_seq"]
                self.resume_requests.append((conn.index, last))
                begin = max(0, last - self.resume_overlap)
            else:
                continue
            for event in self.events[begin:]:
                if event["seq"] in self.drop_once:
                    self.drop_once.discard(event["seq"])
                    continue
                await conn.send(event)
                sent += 1
                if self.delay_s:
                    await asyncio.sleep(self.delay_s)
                if kill_at is not None and sent >= kill_at:
                    conn.script_done = True
                    await conn.kill()
                    return


# --------------------------------------------------------------------------
# Time, callbacks, links, stores
# --------------------------------------------------------------------------
class FastTime:
    """An injected clock and sleep: sleeping records the delay and yields, so
    backoff tests take milliseconds; ``advance`` moves the clock."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []
        self.tool_proposed: list[Any] = []
        self.budget: list[Any] = []
        self.errors: list[Any] = []
        self.states: list[str] = []

    def callbacks(self) -> LinkCallbacks:
        return LinkCallbacks(on_event=self.events.append, on_tool_proposed=self.tool_proposed.append,
                             on_budget_warning=self.budget.append, on_error=self.errors.append,
                             on_state=self.states.append)

    def seqs(self, run_id: str) -> list[int]:
        return [e.seq for e in self.events if e.run_id == run_id]

    def completed(self, run_id: str) -> bool:
        return any(e.run_id == run_id and e.type == "run_event" and e.subtype == "state_changed"
                   and e.payload.get("state") == "completed" for e in self.events)

    def state_reached(self, run_id: str, state: str) -> bool:
        return any(e.run_id == run_id and e.subtype == "state_changed" and e.payload.get("state") == state
                   for e in self.events)


def make_link(store, server, *, rec: Recorder | None = None, fake_time: FastTime | None = None,
              credential: Credential | None = None, **kwargs) -> DeviceLink:
    url = server if isinstance(server, str) else server.server_url
    cred = credential or make_credential(url)
    fake_time = fake_time or FastTime()
    options: dict[str, Any] = dict(
        credential_loader=lambda: cred,
        callbacks=rec.callbacks() if rec is not None else None,
        clock=fake_time.clock,
        sleep=fake_time.sleep,
        rng=lambda: 0.5,
        keepalive_interval_s=None,      # keepalive tests turn it on; fake sleeps must not drive it
        handshake_timeout_s=3.0,
        request_timeout_s=5.0,
        close_timeout_s=1.0,
    )
    options.update(kwargs)
    link = DeviceLink(store, **options)
    link.test_time = fake_time           # handy for assertions
    return link


def new_run(store, run_id: str | None = None) -> str:
    return create_session(store, project_path=PROJECT, mode="ask", session_id=run_id).id


def server_events(store, run_id: str) -> list:
    """The stored events that came from the server, in local order."""
    return [e for e in read_events(store, run_id) if e.type.startswith("server.")]


def stored_server_seqs(store, run_id: str) -> list[int]:
    return [e.data["server_seq"] for e in server_events(store, run_id)]


async def until(predicate: Callable[[], Any], timeout: float = 10.0, what: str = "condition") -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError(f"timed out waiting for {what}")
        await asyncio.sleep(0.005)


def run_async(coro: Awaitable, timeout: float = 30.0):
    """Run a scenario under a hard timeout so a bug hangs one test, not CI."""

    async def guarded():
        return await asyncio.wait_for(coro, timeout)

    return asyncio.run(guarded())


def exception_texts(exc: BaseException) -> list[str]:
    """Every way an exception can be turned into text, including the
    traceback WITH local variables of m2c frames (what error trackers and
    ``pytest --showlocals`` print)."""
    import traceback

    texts = [str(exc), repr(exc), repr(vars(exc)), repr(exc.args), "".join(traceback.format_exception(exc))]
    pending = [traceback.TracebackException.from_exception(exc, capture_locals=True)]
    while pending:
        current = pending.pop()
        for frame in current.stack:
            if str(PKG_DIR) in str(frame.filename):
                texts.append(f"{frame.filename}:{frame.lineno} {frame.name} locals={frame.locals!r}")
        for chained in (current.__cause__, current.__context__):
            if chained is not None:
                pending.append(chained)
    return texts


def log_texts(caplog) -> list[str]:
    """Everything in the captured records, in every form a handler could read it."""
    texts = [caplog.text]
    for record in caplog.records:
        texts += [record.getMessage(), repr(record.args), repr(vars(record))]
        if record.exc_info:
            texts += exception_texts(record.exc_info[1])
    return texts


__all__ = [name for name in dir() if not name.startswith("__") and name not in {"annotations"}]
