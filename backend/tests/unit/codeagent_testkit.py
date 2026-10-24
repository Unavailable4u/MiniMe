"""Shared helpers for the S04 device-socket tests (not collected: no test_ prefix).

Builds on S03's devices_testkit (Clock, FakeStore, SECRET, ...) rather than
re-implementing a device registry:

  WsFakeStore   devices_testkit.FakeStore plus the one store method S04 adds to
                eo.devices.PgStore (credential_is_revoked), call counters, and
                switches that make a store call fail. A subclass, not an edit of
                the shared kit, so this patch cannot conflict with another patch
                touching devices_testkit.py.
  Rig           one FastAPI app with the S04 router behind a TestClient, a fresh
                connection registry, a fresh handshake limiter on a fake clock,
                and injectable socket settings -- everything monkeypatched, so
                no state leaks between tests.
  FakeDevice    a device that knows how to speak the protocol to the Rig:
                connect, hello, send an envelope, read one.
  ScriptedHandler
                a stand-in for the run manager (S06): answers each client
                message with the server lines a golden transcript has next.

Nothing here imports jsonschema at module level: the server loads the protocol
validator lazily, and these helpers must stay importable where it is absent.
"""
import dataclasses
import json
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import anyio
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from codeagent import connections, ws as ws_module
from eo import devices
from tests.unit.devices_testkit import Clock, FakeStore, sha256

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "protocol" / "fixtures"

# protocol/fixtures/*.jsonl all say hello with this credential (they start with
# "m2c-fixture-" and are not secrets). It is 33 characters of [A-Za-z0-9-], so it
# passes eo.devices' credential shape check and can be registered as-is.
FIXTURE_CREDENTIAL = "m2c-fixture-credential-0000000000"

CLIENT_TYPES = {"hello", "run.start", "user_message", "tool_result", "tool_stream",
                "approval", "cancel", "run.resume", "ping"}
SOCKET_PATH = "/v1/code/device"


def load_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()
            if line.strip()]


def now_ts():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def envelope(t, payload=None, *, run_id=None, msg_id=None):
    """A well-formed client envelope (the schema's own rules: lower-case uuid,
    RFC 3339 ts with an offset, run_id only where the type is run-scoped)."""
    msg = {"v": 1, "t": t, "id": msg_id or str(uuid.uuid4()), "ts": now_ts(),
           "payload": {} if payload is None else payload}
    if run_id is not None:
        msg["run_id"] = run_id
    return msg


def hello(credential, *, versions=(1,), client_version="0.0.1-test", capabilities=("sandbox",)):
    return envelope("hello", {"protocol_versions": list(versions),
                              "device_credential": credential,
                              "client_version": client_version,
                              "capabilities": list(capabilities)})


def ping(nonce=None):
    return envelope("ping", {} if nonce is None else {"nonce": nonce})


def user_message(text="hello", run_id="run_testkit_0001"):
    return envelope("user_message", {"text": text}, run_id=run_id)


class WsFakeStore(FakeStore):
    """FakeStore + what S04 needs. Counters let a test prove a call was NOT made
    (a malformed credential never reaches the store; a rate-limited address
    never costs a lookup)."""

    def __init__(self, clock):
        super().__init__(clock)
        self.touch_calls = 0
        self.active_calls = 0
        self.fail_touch = None          # an exception instance to raise
        self.fail_is_active = None
        self.fail_revoked_lookup = None

    def touch_active_by_credential(self, *, credential_hash):
        self.touch_calls += 1
        if self.fail_touch is not None:
            raise self.fail_touch
        return super().touch_active_by_credential(credential_hash=credential_hash)

    def is_active(self, *, user_id, device_id):
        self.active_calls += 1
        if self.fail_is_active is not None:
            raise self.fail_is_active
        return super().is_active(user_id=user_id, device_id=device_id)

    def credential_is_revoked(self, *, credential_hash):
        if self.fail_revoked_lookup is not None:
            raise self.fail_revoked_lookup
        with self.lock:
            return any(d["credential_hash"] == credential_hash and d["revoked_at"] is not None
                       for d in self.devices.values())

    def add_device(self, user_id="user-a", credential=None, name="test-laptop"):
        """Insert a paired device directly (the pairing flow has its own tests).
        Returns (credential, device_id)."""
        credential = credential or ("cred-" + uuid.uuid4().hex + uuid.uuid4().hex[:8])
        device_id = str(uuid.uuid4())
        with self.lock:
            self.devices[device_id] = {
                "id": device_id, "user_id": user_id, "device_name": name, "os": "Ubuntu 24.04",
                "client_version": "0.0.1", "credential_hash": sha256(credential),
                "created_at": self.clock.t, "last_seen_at": None, "revoked_at": None,
            }
        return credential, device_id


class ScriptedHandler(ws_module.MessageHandler):
    """Plays recorded server lines: `replies` maps a client message id to the
    server envelopes to send when that message arrives. Records everything it
    was given, in order."""

    def __init__(self, replies=None, *, fail_on=None):
        self.replies = replies or {}
        self.fail_on = fail_on          # message type that makes on_message raise
        self.received = []
        self.disconnected = []

    async def on_message(self, conn, msg):
        self.received.append(msg)
        if self.fail_on and msg["t"] == self.fail_on:
            raise RuntimeError("CANARY-handler-exploded-with-content")
        for env in self.replies.get(msg["id"], []):
            await conn.send_json(env)

    async def on_disconnect(self, conn):
        self.disconnected.append((conn.user_id, conn.device_id))


class Rig:
    """Everything one socket test needs, all monkeypatched onto fresh state."""

    def __init__(self, monkeypatch, *, client_addr=("203.0.113.7", 40000), limiter=None, **settings):
        self.monkeypatch = monkeypatch
        self.store_clock = Clock()
        self.store = WsFakeStore(self.store_clock)
        monkeypatch.setattr(devices, "_STORE", self.store)

        self.registry = connections.InMemoryRegistry()
        monkeypatch.setattr(connections, "_REGISTRY", self.registry)

        self.limiter_clock = Clock(1000.0)
        self.limiter = limiter or connections.HandshakeLimiter(
            limit=10, window_s=60.0, clock=lambda: self.limiter_clock.t)
        monkeypatch.setattr(connections, "_LIMITER", self.limiter)

        self.deadline_s = 15.0
        self.check = None               # optional callable run on every frame the device receives
        self.configure(**settings)
        monkeypatch.setattr(ws_module, "_HANDLER", ws_module.UnavailableHandler())

        # The real endpoint function, wrapped only to count invocations: Starlette's
        # TestClient cancels the app task the moment the client leaves, which would
        # abort teardown that takes a few event-loop turns. connect() waits for the
        # endpoint to finish, as a real ASGI server (which never cancels it) would let it.
        self.started = 0
        self.finished = set()

        async def endpoint(websocket: WebSocket):
            self.started += 1
            index = self.started
            try:
                await ws_module.device_socket(websocket)
            finally:
                self.finished.add(index)

        app = FastAPI()
        app.add_api_websocket_route(SOCKET_PATH, endpoint)
        self.app = app
        self.client = TestClient(app, client=client_addr)

    def configure(self, **settings):
        self.monkeypatch.setattr(ws_module, "SETTINGS",
                                 dataclasses.replace(ws_module.SETTINGS, **settings))

    def use_handler(self, handler):
        self.monkeypatch.setattr(ws_module, "_HANDLER", handler)
        return handler

    def client_from(self, addr):
        return TestClient(self.app, client=addr)

    @contextmanager
    def connect(self, client=None):
        with (client or self.client).websocket_connect(SOCKET_PATH) as sock:
            mine = self.started                    # the endpoint call that just accepted us
            try:
                yield FakeDevice(sock, self.check, self.deadline_s)
            finally:
                sock.close(1000)                   # the client leaves ...
                deadline = time.monotonic() + 10
                while mine not in self.finished and time.monotonic() < deadline:
                    time.sleep(0.005)              # ... and the server finishes its cleanup
                assert mine in self.finished, "the endpoint did not finish after the client left"

    @contextmanager
    def session(self, credential, client=None, **hello_kwargs):
        """Connect and complete the handshake; yields (device, welcome)."""
        with self.connect(client) as device:
            device.send(hello(credential, **hello_kwargs))
            welcome = device.recv()
            assert welcome["t"] == "welcome", welcome
            yield device, welcome


class FakeDevice:
    """The client end of one socket. Every receive has a deadline, so a server that stops
    answering FAILS the test (with a message saying so) instead of hanging the run."""

    def __init__(self, sock, check=None, timeout=15.0):
        self.sock = sock
        self.check = check
        self.timeout = timeout

    def send(self, msg):
        self.sock.send_text(json.dumps(msg))

    def send_text(self, text):
        self.sock.send_text(text)

    def send_bytes(self, data):
        self.sock.send_bytes(data)

    def _receive(self):
        """The next raw ASGI message, via the test session's own portal and stream (what
        WebSocketTestSession.receive does), with a deadline added."""
        async def with_deadline():
            with anyio.fail_after(self.timeout):
                return await self.sock._send_rx.receive()
        try:
            return self.sock.portal.call(with_deadline)
        except TimeoutError:
            raise AssertionError(
                f"the server sent nothing for {self.timeout:.0f}s (it should have answered or closed)"
            ) from None

    def recv(self):
        message = self._receive()
        if message["type"] == "websocket.close":
            raise WebSocketDisconnect(code=message.get("code", 1000), reason=message.get("reason", ""))
        msg = json.loads(message["text"])
        if self.check is not None:
            self.check(msg)
        return msg

    def recv_error(self):
        msg = self.recv()
        assert msg["t"] == "error", msg
        return msg["payload"]

    def expect_close(self):
        """The close code the server ends the connection with."""
        message = self._receive()
        assert message["type"] == "websocket.close", f"expected a close frame, got {message['type']}"
        return message.get("code", 1000)

    def ping_pong(self, nonce="alive"):
        self.send(ping(nonce))
        reply = self.recv()
        assert reply["t"] == "pong" and reply["payload"] == {"nonce": nonce}, reply
