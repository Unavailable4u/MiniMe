"""S14a: the message router (codeagent/router.py) -- the MessageHandler that S04's socket
hands every validated message to, and that dispatches it by type.

Two layers of tests:

  * plain unit tests with a stand-in connection (what the router does with a message
    and with its own mistakes: dispatch, the unregistered type, a handler that raises,
    the disconnect listeners, the registration rules, install);
  * socket tests through S04's real endpoint (tests/unit/codeagent_testkit.Rig and
    FakeDevice), so the claims "the socket stays alive", "the device gets `internal`
    with the id of its message" and "listeners run after the device left" are proven
    on the real transport and not only against a fake.

Maps to the work package ("What" 1 and "Content hygiene" 5):
  * a message type with no handler gets `internal`, with ref_id and a fixed text .. test_unregistered_*
  * a handler that raises is contained, the socket stays alive ................... test_a_handler_that_raises_*
  * register_disconnect takes several listeners, called in registration order .... test_disconnect_*
  * install() is idempotent, get_router() is the single instance ................. test_install_*, test_get_router_*
  * nothing a message carries, and no exception text, reaches the log ............ test_no_content_*

Skipped as a module when jsonschema is not installed, like the other socket tests.
"""
import asyncio
import logging
import uuid

import pytest

pytest.importorskip(
    "jsonschema",
    reason="jsonschema is not installed; install protocol/requirements.txt to run the "
           "device-socket tests (see codeagent/ws.py, 'Runtime needs')",
)

from codeagent import router as router_module, ws as ws_module            # noqa: E402
from codeagent.connections import ConnectionClosed                         # noqa: E402
from codeagent.router import MessageRouter                                 # noqa: E402
from tests.unit.codeagent_testkit import Rig, envelope, user_message       # noqa: E402
from tests.unit.devices_testkit import SECRET, SUPABASE_SECRET             # noqa: E402

CANARY = "CANARY-router-content-do-not-log-3c9e"
RUN = "run_router_test_01"


def run(coro):
    return asyncio.run(coro)


class StubConn:
    """Just enough of a DeviceConnection for the unit tests: who it is, and what the
    router sent to it. `closed` makes send_error raise like a connection that left."""

    def __init__(self, user_id="user-a", device_id="3f1c2d4e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"):
        self.user_id = user_id
        self.device_id = device_id
        self.errors = []
        self.closed = False

    async def send_error(self, code, message, *, ref_id=None, details=None):
        if self.closed:
            raise ConnectionClosed("connection closed")
        self.errors.append({"code": code, "message": message, "ref_id": ref_id, "details": details})


def msg(t="user_message", payload=None, msg_id=None):
    return {"v": 1, "t": t, "id": msg_id or str(uuid.uuid4()), "run_id": RUN,
            "ts": "2026-11-02T09:00:00.000Z", "payload": {"text": "hi"} if payload is None else payload}


# ===================================================================== dispatch

def test_a_message_goes_to_the_handler_registered_for_its_type():
    r, seen = MessageRouter(), []

    async def on_user_message(conn, m):
        seen.append(("user_message", conn, m))

    async def on_cancel(conn, m):
        seen.append(("cancel", conn, m))

    r.register("user_message", on_user_message)
    r.register("cancel", on_cancel)
    conn, first, second = StubConn(), msg("user_message"), msg("cancel", {})
    run(r.on_message(conn, first))
    run(r.on_message(conn, second))
    assert [name for name, _, _ in seen] == ["user_message", "cancel"]
    assert seen[0][1] is conn and seen[0][2] is first        # the same objects, not copies
    assert seen[1][2] is second
    assert conn.errors == []


def test_the_router_is_a_message_handler():
    assert isinstance(MessageRouter(), ws_module.MessageHandler)


def test_a_handler_may_answer_on_the_connection_itself():
    r = MessageRouter()

    async def reply(conn, m):
        await conn.send_error("rate_limited", "slow down", ref_id=m["id"])

    r.register("user_message", reply)
    conn, m = StubConn(), msg()
    run(r.on_message(conn, m))
    assert [e["code"] for e in conn.errors] == ["rate_limited"]      # exactly what the handler sent


def test_handlers_registered_lists_the_types_in_a_stable_order():
    r = MessageRouter()

    async def h(conn, m):
        return None

    r.register("run.start", h)
    r.register("cancel", h)
    assert r.registered_types() == ("cancel", "run.start")
    assert r.is_registered("cancel") and not r.is_registered("approval")


# ================================================================== unregistered

def test_unregistered_type_answers_internal_with_the_ref_id_and_a_fixed_text():
    r, conn = MessageRouter(), StubConn()
    m = msg("approval", {"call_id": "call_x", "decision": "allow", "scope": "once"})
    run(r.on_message(conn, m))
    assert len(conn.errors) == 1
    err = conn.errors[0]
    assert err["code"] == "internal"
    assert err["ref_id"] == m["id"]
    assert err["message"] == router_module.MSG_NO_HANDLER
    assert "call_x" not in err["message"] and "approval" not in err["message"]


def test_unregistered_type_text_never_echoes_what_the_client_sent():
    r, conn = MessageRouter(), StubConn()
    run(r.on_message(conn, msg("user_message", {"text": CANARY})))
    assert CANARY not in repr(conn.errors)


def test_a_message_without_a_well_formed_id_gets_internal_without_a_ref_id():
    r, conn = MessageRouter(), StubConn()
    run(r.on_message(conn, msg("cancel", {}, msg_id="NOT-A-LOWERCASE-UUID")))
    assert conn.errors[0]["code"] == "internal" and conn.errors[0]["ref_id"] is None


def test_the_ref_id_rule_is_the_sockets_own():
    """router._ref_id is a copy of ws._ref_id (that function is private to ws.py, which
    this patch may not edit); this pins that the two cannot drift apart."""
    good = str(uuid.uuid4())
    samples = [{"id": good}, {"id": good.upper()}, {"id": "x"}, {"id": 7}, {}, None, "str", [good]]
    for sample in samples:
        assert router_module._ref_id(sample) == ws_module._ref_id(sample), sample


def test_a_type_that_is_not_a_string_is_treated_as_unregistered():
    r, conn = MessageRouter(), StubConn()
    bad = msg()
    bad["t"] = ["run.start"]
    run(r.on_message(conn, bad))
    assert conn.errors[0]["code"] == "internal"


# ====================================================== a handler that goes wrong

def test_a_handler_that_raises_is_contained_and_answered_with_internal():
    r, conn = MessageRouter(), StubConn()

    async def boom(conn_, m):
        raise RuntimeError(CANARY)

    r.register("user_message", boom)
    m = msg()
    run(r.on_message(conn, m))                                 # does not raise
    assert conn.errors == [{"code": "internal", "message": ws_module.MSG_INTERNAL,
                            "ref_id": m["id"], "details": None}]
    assert CANARY not in repr(conn.errors)


def test_the_router_keeps_working_after_a_handler_failed():
    r, conn, seen = MessageRouter(), StubConn(), []

    async def boom(c, m):
        raise ValueError("x")

    async def fine(c, m):
        seen.append(m["id"])

    r.register("user_message", boom)
    r.register("cancel", fine)
    run(r.on_message(conn, msg("user_message")))
    ok = msg("cancel", {})
    run(r.on_message(conn, ok))
    assert seen == [ok["id"]]


def test_a_closed_connection_is_not_a_handler_failure():
    """ConnectionClosed means the peer left. ws.py's read loop ends on it; swallowing it
    here would keep a dead connection's loop alive, and answering it with `internal`
    would try to write to the closed socket."""
    r, conn = MessageRouter(), StubConn()

    async def peer_left(c, m):
        raise ConnectionClosed("connection closed")

    r.register("user_message", peer_left)
    with pytest.raises(ConnectionClosed):
        run(r.on_message(conn, msg()))
    assert conn.errors == []


def test_cancellation_is_never_swallowed():
    r, conn = MessageRouter(), StubConn()

    async def cancelled(c, m):
        raise asyncio.CancelledError()

    r.register("user_message", cancelled)

    async def scenario():
        task = asyncio.ensure_future(r.on_message(conn, msg()))
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert conn.errors == []


def test_if_the_internal_answer_cannot_be_sent_the_closed_connection_propagates():
    r, conn = MessageRouter(), StubConn()
    conn.closed = True

    async def boom(c, m):
        raise RuntimeError("x")

    r.register("user_message", boom)
    with pytest.raises(ConnectionClosed):
        run(r.on_message(conn, msg()))


# ======================================================== disconnect listeners

def test_disconnect_listeners_run_in_registration_order():
    r, order = MessageRouter(), []

    async def first(conn):
        order.append("first")

    async def second(conn):
        await asyncio.sleep(0)                  # a listener that yields must not be overtaken
        order.append("second")

    async def third(conn):
        order.append("third")

    for fn in (first, second, third):
        r.register_disconnect(fn)
    conn = StubConn()
    run(r.on_disconnect(conn))
    assert order == ["first", "second", "third"]


def test_every_listener_gets_the_connection_that_ended():
    r, got = MessageRouter(), []

    async def listener(conn):
        got.append(conn)

    r.register_disconnect(listener)
    conn = StubConn()
    run(r.on_disconnect(conn))
    assert got == [conn]


def test_a_failing_listener_does_not_stop_the_ones_after_it():
    r, order = MessageRouter(), []

    async def first(conn):
        order.append("first")

    async def bad(conn):
        raise RuntimeError(CANARY)

    async def last(conn):
        order.append("last")

    for fn in (first, bad, last):
        r.register_disconnect(fn)
    run(r.on_disconnect(StubConn()))            # does not raise
    assert order == ["first", "last"]


def test_a_listener_that_finds_the_connection_closed_is_not_an_error():
    r, order = MessageRouter(), []

    async def left(conn):
        raise ConnectionClosed("connection closed")

    async def after(conn):
        order.append("after")

    r.register_disconnect(left)
    r.register_disconnect(after)
    run(r.on_disconnect(StubConn()))
    assert order == ["after"]


def test_registering_the_same_listener_twice_keeps_one_copy():
    r, calls = MessageRouter(), []

    async def listener(conn):
        calls.append(1)

    r.register_disconnect(listener)
    r.register_disconnect(listener)
    run(r.on_disconnect(StubConn()))
    assert calls == [1]


def test_on_disconnect_with_no_listeners_does_nothing():
    run(MessageRouter().on_disconnect(StubConn()))


# ============================================================ registration rules

def test_registering_a_plain_function_is_refused():
    r = MessageRouter()

    def sync_handler(conn, m):                  # would be called and never awaited
        return None

    with pytest.raises(TypeError):
        r.register("user_message", sync_handler)
    with pytest.raises(TypeError):
        r.register_disconnect(sync_handler)


@pytest.mark.parametrize("bad", ["", None, 7, ["run.start"]])
def test_registering_a_bad_type_name_is_refused(bad):
    async def h(conn, m):
        return None

    with pytest.raises(ValueError):
        MessageRouter().register(bad, h)


def test_registering_the_same_handler_again_changes_nothing():
    r, calls = MessageRouter(), []

    async def h(conn, m):
        calls.append(1)

    r.register("cancel", h)
    r.register("cancel", h)                     # an idempotent install does exactly this
    run(r.on_message(StubConn(), msg("cancel", {})))
    assert calls == [1]


def test_a_second_different_handler_for_a_type_is_refused_unless_replace_is_given():
    r, calls = MessageRouter(), []

    async def one(conn, m):
        calls.append("one")

    async def two(conn, m):
        calls.append("two")

    r.register("cancel", one)
    with pytest.raises(router_module.RouterError):
        r.register("cancel", two)
    run(r.on_message(StubConn(), msg("cancel", {})))
    assert calls == ["one"]                     # the refused registration changed nothing
    r.register("cancel", two, replace=True)
    run(r.on_message(StubConn(), msg("cancel", {})))
    assert calls == ["one", "two"]


def test_async_callable_objects_and_partials_are_accepted():
    import functools
    r, seen = MessageRouter(), []

    class Handler:
        async def __call__(self, conn, m):
            seen.append("object")

    async def with_arg(tag, conn, m):
        seen.append(tag)

    r.register("cancel", Handler())
    r.register("user_message", functools.partial(with_arg, "partial"))
    conn = StubConn()
    run(r.on_message(conn, msg("cancel", {})))
    run(r.on_message(conn, msg("user_message")))
    assert seen == ["object", "partial"]


# ====================================================================== install

def test_get_router_returns_the_one_instance(monkeypatch):
    monkeypatch.setattr(router_module, "_ROUTER", MessageRouter())
    assert router_module.get_router() is router_module.get_router()


def test_install_puts_the_router_on_the_socket_and_is_idempotent(monkeypatch):
    fresh = MessageRouter()
    monkeypatch.setattr(router_module, "_ROUTER", fresh)
    monkeypatch.setattr(ws_module, "_HANDLER", ws_module.UnavailableHandler())
    assert router_module.install() is fresh
    assert ws_module.get_message_handler() is fresh
    assert router_module.install() is fresh                      # twice: the same state
    assert ws_module.get_message_handler() is fresh


def test_install_does_not_register_anything_by_itself(monkeypatch):
    fresh = MessageRouter()
    monkeypatch.setattr(router_module, "_ROUTER", fresh)
    monkeypatch.setattr(ws_module, "_HANDLER", ws_module.UnavailableHandler())
    router_module.install()
    assert fresh.registered_types() == ()                        # handlers are S14c-e's to add


def test_importing_the_router_does_not_replace_the_sockets_handler(monkeypatch):
    """Installing is an explicit call (server.py makes it), never an import side effect.
    The module is executed once more under another name, so the real one (and the class
    identities the other tests hold) are untouched."""
    import importlib.util
    sentinel = ws_module.UnavailableHandler()
    monkeypatch.setattr(ws_module, "_HANDLER", sentinel)
    spec = importlib.util.spec_from_file_location("router_import_probe", router_module.__file__)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    assert probe.MessageRouter is not MessageRouter                  # it really was a second import
    assert ws_module.get_message_handler() is sentinel


# =================================================== through the real socket

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DEVICE_JWT_SECRET", SECRET)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", SUPABASE_SECRET)


@pytest.fixture
def rig(monkeypatch):
    return Rig(monkeypatch)


@pytest.fixture
def device(rig):
    return rig.store.add_device("user-a")


def test_socket_unregistered_type_gets_internal_with_the_id_of_the_message(rig, device):
    r = rig.use_handler(MessageRouter())
    credential, _ = device
    with rig.session(credential) as (dev, _welcome):
        m = user_message("hello", run_id=RUN)
        dev.send(m)
        err = dev.recv_error()
        assert err["code"] == "internal"
        assert err["ref_id"] == m["id"]
        assert err["message"] == router_module.MSG_NO_HANDLER
        dev.ping_pong()                                          # and the socket carries on
    assert r.registered_types() == ()


def test_socket_a_registered_handler_receives_the_validated_message_and_the_device(rig, device):
    r = rig.use_handler(MessageRouter())
    credential, device_id = device
    seen = []

    async def on_user_message(conn, m):
        seen.append((conn.user_id, conn.device_id, m["t"], m["run_id"], m["payload"]["text"]))
        await conn.send_error("rate_limited", "queue full", ref_id=m["id"])   # any server frame will do

    r.register("user_message", on_user_message)
    with rig.session(credential) as (dev, _welcome):
        dev.send(user_message("typed text", run_id=RUN))
        assert dev.recv_error()["code"] == "rate_limited"
    assert seen == [("user-a", device_id, "user_message", RUN, "typed text")]


def test_socket_a_handler_that_raises_does_not_take_the_connection_down(rig, device):
    r = rig.use_handler(MessageRouter())
    credential, _ = device

    async def boom(conn, m):
        raise RuntimeError(CANARY)

    r.register("user_message", boom)
    with rig.session(credential) as (dev, _welcome):
        m = user_message("x", run_id=RUN)
        dev.send(m)
        err = dev.recv_error()
        assert err["code"] == "internal" and err["ref_id"] == m["id"]
        assert err["message"] == ws_module.MSG_INTERNAL
        assert CANARY not in str(err)
        dev.send(user_message("again", run_id=RUN))              # still being served
        assert dev.recv_error()["code"] == "internal"
        dev.ping_pong()


def test_socket_the_router_keeps_serving_other_types_after_one_failed(rig, device):
    r = rig.use_handler(MessageRouter())
    credential, _ = device
    seen = []

    async def boom(conn, m):
        raise RuntimeError("x")

    async def cancel(conn, m):
        seen.append(m["run_id"])
        await conn.send_error("run_not_found", "no such run", ref_id=m["id"])

    r.register("user_message", boom)
    r.register("cancel", cancel)
    with rig.session(credential) as (dev, _welcome):
        dev.send(user_message("x", run_id=RUN))
        assert dev.recv_error()["code"] == "internal"
        dev.send(envelope("cancel", {}, run_id=RUN))
        assert dev.recv_error()["code"] == "run_not_found"
    assert seen == [RUN]


def test_socket_disconnect_listeners_run_in_order_once_the_device_has_left(rig, device):
    r = rig.use_handler(MessageRouter())
    credential, device_id = device
    order = []

    async def first(conn):
        order.append(("first", conn.user_id, conn.device_id, rig.registry.is_online(conn.user_id, conn.device_id)))

    async def second(conn):
        order.append(("second", conn.user_id, conn.device_id, rig.registry.is_online(conn.user_id, conn.device_id)))

    r.register_disconnect(first)
    r.register_disconnect(second)
    with rig.session(credential) as (dev, _welcome):
        dev.ping_pong()
        assert order == []                                       # not while connected
    assert order == [("first", "user-a", device_id, False), ("second", "user-a", device_id, False)]


def test_socket_a_failing_disconnect_listener_does_not_break_the_cleanup(rig, device):
    r = rig.use_handler(MessageRouter())
    credential, _ = device
    order = []

    async def bad(conn):
        raise RuntimeError(CANARY)

    async def good(conn):
        order.append("good")

    r.register_disconnect(bad)
    r.register_disconnect(good)
    with rig.session(credential) as (dev, _welcome):
        dev.ping_pong()
    assert order == ["good"]
    assert rig.registry.count() == 0


# ====================================================== content hygiene (caplog)

def test_no_content_and_no_exception_text_reaches_the_log(rig, device, caplog):
    caplog.set_level(logging.DEBUG)
    r = rig.use_handler(MessageRouter())
    credential, _ = device

    async def boom(conn, m):
        raise RuntimeError(CANARY)

    async def bad_listener(conn):
        raise RuntimeError(CANARY)

    r.register("user_message", boom)
    r.register_disconnect(bad_listener)
    with rig.session(credential) as (dev, _welcome):
        dev.send(user_message(CANARY, run_id=RUN))                # content in the payload, a handler that fails
        dev.recv_error()
        dev.send(envelope("approval", {"call_id": "call_x", "decision": "allow", "scope": "once"},
                          run_id=RUN))                            # a type with no handler
        dev.recv_error()
        dev.ping_pong()
    assert CANARY not in caplog.text
    router_lines = [rec for rec in caplog.records if rec.name == router_module.logger.name]
    assert router_lines, "the router should log its own failures (type names only)"
    assert any("RuntimeError" in rec.getMessage() for rec in router_lines)


def test_a_type_name_that_is_not_one_of_the_schemas_is_not_logged(caplog):
    """ws.py only hands on validated types, but the router is a public seam: a name that
    does not look like a protocol type is logged as '?', never as given."""
    caplog.set_level(logging.DEBUG)
    r, conn = MessageRouter(), StubConn()
    odd = msg()
    odd["t"] = "x" * 40 + CANARY
    run(r.on_message(conn, odd))
    assert CANARY not in caplog.text
    assert conn.errors[0]["code"] == "internal"
