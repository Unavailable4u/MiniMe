"""S04: codeagent/connections.py -- the registry of connected devices, the
failed-handshake limiter and the per-connection send path. No sockets here: a fake
websocket stands in, so the interfaces S06/S09/S11 will build on are pinned directly.
"""
import asyncio
import uuid

import pytest

from codeagent import connections
from codeagent.connections import (
    ConnectionClosed,
    ConnectionRegistry,
    DeviceConnection,
    HandshakeLimiter,
    InMemoryRegistry,
)
from tests.unit.devices_testkit import Clock


class FakeSocket:
    """The slice of starlette's WebSocket that DeviceConnection uses."""

    def __init__(self, *, slow=False):
        self.sent, self.closed_with, self.slow = [], None, slow
        self.in_flight = self.max_in_flight = 0
        self.fail_next = None

    async def send_text(self, text):
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        if self.slow:
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.sent.append(text)
        self.in_flight -= 1

    async def close(self, code=1000, reason=None):
        self.closed_with = (code, reason)


def make_conn(user="user-a", device=None, sock=None):
    return DeviceConnection(sock or FakeSocket(), user_id=user, device_id=device or str(uuid.uuid4()),
                            protocol=1, client_version="0.0.1", capabilities=("sandbox",))


# ------------------------------------------------------------------ registry

def test_registry_interface_is_abstract():
    with pytest.raises(TypeError):
        ConnectionRegistry()


def test_add_get_remove_and_count():
    reg = InMemoryRegistry()
    a, b = make_conn("u1"), make_conn("u2")
    assert reg.add(a) is None and reg.add(b) is None
    assert reg.count() == 2
    assert reg.get("u1", a.device_id) is a and reg.get("u2", a.device_id) is None
    assert reg.is_online("u1", a.device_id) and not reg.is_online("u1", b.device_id)
    reg.remove(a)
    assert reg.count() == 1 and reg.get("u1", a.device_id) is None


def test_for_user_returns_only_that_users_devices():
    reg = InMemoryRegistry()
    mine = [make_conn("u1"), make_conn("u1")]
    for c in mine + [make_conn("u2")]:
        reg.add(c)
    assert set(reg.for_user("u1")) == set(mine)
    assert reg.for_user("nobody") == []
    snapshot = reg.for_user("u1")
    reg.remove(mine[0])
    assert len(snapshot) == 2                              # a snapshot, not a live view


def test_adding_the_same_device_again_returns_the_connection_it_replaced():
    reg = InMemoryRegistry()
    old = make_conn("u1", "dev-1")
    new = make_conn("u1", "dev-1")
    assert reg.add(old) is None
    assert reg.add(new) is old
    assert reg.get("u1", "dev-1") is new and reg.count() == 1


def test_a_superseded_connection_removing_itself_does_not_remove_its_replacement():
    reg = InMemoryRegistry()
    old, new = make_conn("u1", "dev-1"), make_conn("u1", "dev-1")
    reg.add(old)
    reg.add(new)
    reg.remove(old)
    assert reg.get("u1", "dev-1") is new
    reg.remove(old)                                        # and removing twice is harmless
    reg.remove(new)
    assert reg.count() == 0 and reg.for_user("u1") == []   # no empty user bucket left behind


def test_module_accessors_hand_out_the_current_registry_and_limiter(monkeypatch):
    reg, lim = InMemoryRegistry(), HandshakeLimiter()
    monkeypatch.setattr(connections, "_REGISTRY", reg)
    monkeypatch.setattr(connections, "_LIMITER", lim)
    assert connections.get_registry() is reg and connections.get_handshake_limiter() is lim
    other = InMemoryRegistry()
    connections.set_registry(other)
    assert connections.get_registry() is other


# ------------------------------------------------------------------ limiter

def make_limiter(limit=3, window=60.0, **kw):
    clock = Clock(1000.0)
    return HandshakeLimiter(limit=limit, window_s=window, clock=lambda: clock.t, **kw), clock


def test_limiter_allows_until_the_limit_then_reports_retry_after():
    lim, clock = make_limiter(limit=3, window=60)
    for _ in range(2):
        lim.record_failure("1.2.3.4")
        assert lim.retry_after("1.2.3.4") == 0
    lim.record_failure("1.2.3.4")
    assert lim.retry_after("1.2.3.4") == 60
    clock.advance(10.2)
    assert lim.retry_after("1.2.3.4") == 50                # rounded UP: never tells a client "0" too early
    clock.advance(49.0)
    assert lim.retry_after("1.2.3.4") == 1                 # 0.8 s left
    clock.advance(1.0)
    assert lim.retry_after("1.2.3.4") == 0


def test_limiter_counts_a_sliding_window_not_a_fixed_bucket():
    lim, clock = make_limiter(limit=2, window=60)
    lim.record_failure("a")
    clock.advance(40)
    lim.record_failure("a")                                # 2 within 60s -> limited
    assert lim.retry_after("a") == 20                      # until the OLDEST ages out
    clock.advance(21)
    assert lim.retry_after("a") == 0
    lim.record_failure("a")                                # the old one expired; this is 2 in window again
    assert lim.retry_after("a") > 0


def test_limiter_is_per_address_and_none_is_one_shared_address():
    lim, _ = make_limiter(limit=1)
    lim.record_failure("a")
    assert lim.retry_after("a") > 0 and lim.retry_after("b") == 0
    lim.record_failure(None)
    assert lim.retry_after(None) > 0 and lim.retry_after("unknown") > 0


def test_checking_does_not_count_and_a_limited_address_cannot_extend_its_own_penalty():
    lim, clock = make_limiter(limit=1, window=60)
    lim.record_failure("a")
    for _ in range(50):
        lim.retry_after("a")
    clock.advance(30)
    lim.record_failure("a")                                # still limited: recording more is ignored
    clock.advance(31)
    assert lim.retry_after("a") == 0                       # freed 60s after the FIRST failure


def test_limiter_memory_is_bounded():
    lim, clock = make_limiter(limit=5, window=60, max_tracked=100)
    for i in range(1000):
        lim.record_failure(f"10.0.{i // 250}.{i % 250}")
    assert lim.tracked() <= 100
    clock.advance(61)
    lim.record_failure("fresh")
    assert lim.tracked() <= 100
    lim.sweep()
    assert lim.tracked() == 1                              # expired entries are dropped


def test_limiter_defaults_are_a_modest_per_minute_budget():
    lim = HandshakeLimiter()
    assert 1 <= lim.limit <= 30 and 10 <= lim.window_s <= 600


# ------------------------------------------------------------------ DeviceConnection

def run(coro):
    return asyncio.run(coro)


def test_send_message_builds_a_protocol_envelope():
    sock = FakeSocket()
    conn = make_conn(sock=sock)

    async def go():
        a = await conn.send_message("pong", {"nonce": "x"})
        b = await conn.send_message("run_event", {"type": "text_done"}, run_id="run_abcdef01", seq=7)
        return a, b

    a, b = run(go())
    import json
    sent = [json.loads(t) for t in sock.sent]
    assert sent == [a, b]
    assert a["v"] == 1 and a["t"] == "pong" and a["payload"] == {"nonce": "x"}
    assert "run_id" not in a and "seq" not in a
    assert b["run_id"] == "run_abcdef01" and b["seq"] == 7
    assert uuid.UUID(a["id"]) and a["id"] == a["id"].lower() and a["id"] != b["id"]
    assert a["ts"].endswith("Z") and "T" in a["ts"]


def test_envelopes_validate_against_protocol_v1():
    jsonschema = pytest.importorskip("jsonschema")           # noqa: F841
    from codeagent import ws as ws_module
    protocol = ws_module.load_protocol()
    sock = FakeSocket()
    conn = make_conn(sock=sock)
    import json

    async def go():
        await conn.send_message("pong", {})
        await conn.send_error("rate_limited", "Too many attempts.", details={"retry_after_s": 5})
        await conn.send_error("invalid_message", "Bad message.", ref_id=str(uuid.uuid4()))
        await conn.send_message("run_event", {"type": "text_done"}, run_id="run_abcdef01", seq=1)

    run(go())
    for text in sock.sent:
        issues = protocol.validate_message(json.loads(text))
        assert issues == [], [str(i) for i in issues]


def test_send_error_omits_empty_optional_fields():
    sock = FakeSocket()
    conn = make_conn(sock=sock)
    import json
    run(conn.send_error("internal", "Server error."))
    payload = json.loads(sock.sent[0])["payload"]
    assert payload == {"code": "internal", "message": "Server error."}


def test_concurrent_sends_are_serialized():
    sock = FakeSocket(slow=True)
    conn = make_conn(sock=sock)

    async def go():
        await asyncio.gather(*[conn.send_message("pong", {}) for _ in range(10)])

    run(go())
    assert len(sock.sent) == 10 and sock.max_in_flight == 1


def test_close_is_idempotent_and_later_sends_raise_connection_closed():
    sock = FakeSocket()
    conn = make_conn(sock=sock)

    async def go():
        assert not conn.closed
        await conn.close(4409, "superseded")
        await conn.close(1000, "again")
        assert conn.closed
        with pytest.raises(ConnectionClosed):
            await conn.send_message("pong", {})

    run(go())
    assert sock.closed_with == (4409, "superseded")        # the first close wins


def test_a_transport_error_on_send_marks_the_connection_closed():
    sock = FakeSocket()
    conn = make_conn(sock=sock)
    sock.fail_next = RuntimeError("Cannot call send once a close message has been sent")

    async def go():
        with pytest.raises(ConnectionClosed):
            await conn.send_message("pong", {})
        assert conn.closed

    run(go())


def test_closed_event_wakes_a_waiter_when_someone_else_closes_the_connection():
    conn = make_conn()

    async def go():
        waiter = asyncio.create_task(conn.wait_closed())
        await asyncio.sleep(0)
        assert not waiter.done()
        await conn.close(4409, "superseded")
        await asyncio.wait_for(waiter, 1)

    run(go())
