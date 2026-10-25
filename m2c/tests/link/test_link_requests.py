"""
m2c/tests/link/test_link_requests.py -- Patch C04: request/response correlation,
the ping/pong keepalive, what the client sends, and sending while not connected.

Correlation in protocol v1: a ``pong`` echoes the ping's ``nonce``; an
``error`` names the message it answers in ``ref_id`` (the message's ``id``);
there is no other reply type.  So the link keeps a table of pending requests
and settles each from one of those two, with a timeout.
"""
from __future__ import annotations

import pytest

from link_fakes import *  # noqa: F401,F403  (autouse fixtures; see link_fakes.py)

from m2c.link import (
    LinkClosedError,
    LinkMessageTooLargeError,
    LinkNotConnectedError,
    LinkRateLimitedError,
    LinkRequestError,
    LinkRequestTimeout,
)
from m2c.store import UnknownSessionError

RUN = "run_requests_01"


def start_kwargs(**over):
    kwargs = dict(mode="ask", cwd_label="demo-project", instructions="")
    kwargs.update(over)
    return kwargs


# --------------------------------------------------------------------------
# ping / pong
# --------------------------------------------------------------------------
def test_ping_returns_the_round_trip_time_and_echoes_the_nonce():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            rtt = await link.ping(nonce="n42")
            auto = await link.ping()
            await link.aclose()
            return srv, rtt, auto

    srv, rtt, auto = run_async(scenario())
    srv.assert_clean()
    pings = srv.messages(0, "ping")
    assert pings[0]["payload"] == {"nonce": "n42"}
    assert len(pings[1]["payload"]["nonce"]) >= 8                  # a minted nonce, unique per ping
    assert pings[0]["payload"]["nonce"] != pings[1]["payload"]["nonce"]
    assert rtt >= 0 and auto >= 0 and "run_id" not in pings[0]


def test_concurrent_pings_are_matched_by_nonce_even_when_answered_out_of_order():
    store = open_db()

    async def reversed_pongs(conn):
        await do_handshake(conn)
        first = await conn.recv(timeout=5)
        second = await conn.recv(timeout=5)
        await conn.send(env("pong", {"nonce": second["payload"]["nonce"]}))
        await asyncio.sleep(0.05)
        await conn.send(env("pong", {"nonce": first["payload"]["nonce"]}))
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(reversed_pongs) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            order = []

            async def one(nonce):
                await link.ping(nonce=nonce)
                order.append(nonce)

            await asyncio.gather(one("a"), one("b"))
            assert link.pending_count == 0
            await link.aclose()
            return order

    assert run_async(scenario()) == ["b", "a"]                      # each future was settled by its own nonce


def test_an_unanswered_ping_times_out_and_leaves_nothing_pending():
    store = open_db()

    async def deaf(conn):
        await do_handshake(conn)
        while True:
            await conn.recv(timeout=None)

    async def scenario():
        async with FakeServer(deaf) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(LinkRequestTimeout):
                await link.ping(timeout=0.1)
            pending = link.pending_count
            await link.aclose()
            return pending

    assert run_async(scenario()) == 0


def test_a_pong_nobody_asked_for_is_ignored():
    store = open_db()

    async def script(conn):
        await do_handshake(conn)
        await conn.send(env("pong", {"nonce": "never-asked"}))
        await conn.send(env("pong", {}))
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(script) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            await link.ping()
            state = link.state
            await link.aclose()
            return state

    assert run_async(scenario()) == "connected"


# --------------------------------------------------------------------------
# The keepalive
# --------------------------------------------------------------------------
def test_the_keepalive_pings_regularly_and_a_healthy_connection_is_kept():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv, keepalive_interval_s=0.05, keepalive_timeout_s=1.0)
            link.start()
            await link.wait_connected(5)
            await until(lambda: len(srv.messages(0, "ping")) >= 4, what="four keepalive pings")
            await link.aclose()
            return srv

    srv = run_async(scenario())
    assert len(srv.connections) == 1
    nonces = [m["payload"]["nonce"] for m in srv.messages(0, "ping")]
    assert len(set(nonces)) == len(nonces)


def test_a_connection_that_stops_answering_is_dropped_and_re_established():
    """Half-open connections look healthy to TCP; only an answer proves liveness."""
    store = open_db()

    async def goes_deaf(conn):
        await do_handshake(conn)
        answered = 0
        while True:
            msg = await conn.recv(timeout=None)
            if msg["t"] == "ping" and answered < 2:
                answered += 1
                await conn.send(env("pong", {"nonce": msg["payload"]["nonce"]}))

    async def scenario():
        async with FakeServer(goes_deaf, idle_script()) as srv:
            link = make_link(store, srv, keepalive_interval_s=0.05, keepalive_timeout_s=0.1)
            link.start()
            await link.wait_connected(5)
            await until(lambda: len(srv.connections) == 2 and link.state == "connected", what="the new connection")
            await link.aclose()
            return srv, link

    srv, link = run_async(scenario())
    assert len(srv.messages(0, "ping")) == 3                         # two answered, the third was not
    assert link.stats.connections == 2


# --------------------------------------------------------------------------
# Errors answered by ref_id
# --------------------------------------------------------------------------
def test_start_run_waits_for_the_first_event_and_is_rejected_by_an_error_naming_it():
    store = open_db()
    new_run(store, RUN)

    async def script(conn):
        await do_handshake(conn)
        start = await conn.recv(timeout=None)
        await conn.send(error_env("invalid_message", run_id=RUN, ref_id=start["id"]))
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(script) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(LinkRequestError) as raised:
                await link.start_run(run_id=RUN, **start_kwargs())
            pending = link.pending_count
            await link.aclose()
            return srv, rec, raised.value, pending, link

    srv, rec, exc, pending, link = run_async(scenario())
    assert exc.code == "invalid_message" and exc.ref_id == srv.messages(0, "run.start")[0]["id"]
    assert rec.errors == []                                           # it settled the request; not also a callback
    assert pending == 0
    assert link.run_info(RUN) is None                                 # a rejected run is not kept for resuming


def test_start_run_times_out_if_the_server_never_answers():
    store = open_db()
    new_run(store, RUN)

    async def deaf(conn):
        await do_handshake(conn)
        while True:
            await conn.recv(timeout=None)

    async def scenario():
        async with FakeServer(deaf) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(LinkRequestTimeout):
                await link.start_run(run_id=RUN, timeout=0.1, **start_kwargs())
            pending = link.pending_count
            await link.aclose()
            return pending

    assert run_async(scenario()) == 0


def test_an_error_about_a_fire_and_forget_message_goes_to_the_error_callback_and_the_link_stays_up():
    store = open_db()
    new_run(store, RUN)

    async def script(conn):
        await do_handshake(conn)
        await conn.recv(timeout=None)                                  # run.start
        await conn.send(synthetic_events(RUN, 5)[0])
        message = await conn.recv(timeout=None)                        # user_message
        await conn.send(error_env("provider_unavailable", run_id=RUN, ref_id=message["id"]))
        await conn.send(error_env("rate_limited", run_id=RUN, details={"retry_after_s": 3}))
        await conn.send(error_env("some_future_code", message="from a newer server"))
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(script) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, **start_kwargs())
            message_id = await link.send_user_message(RUN, "hello")
            await until(lambda: len(rec.errors) == 3, what="three errors")
            state = link.state
            await link.aclose()
            return rec, state, message_id

    rec, state, message_id = run_async(scenario())
    assert state == "connected"
    first, second, third = rec.errors
    assert (first.code, first.ref_id, first.run_id) == ("provider_unavailable", message_id, RUN)
    assert (second.code, second.details) == ("rate_limited", {"retry_after_s": 3})
    assert third.code == "internal"                                    # an unknown code is treated as internal (README 4)


def test_rate_limited_for_a_pending_request_raises_with_the_wait():
    store = open_db()
    new_run(store, RUN)

    async def script(conn):
        await do_handshake(conn)
        start = await conn.recv(timeout=None)
        await conn.send(error_env("rate_limited", run_id=RUN, ref_id=start["id"], details={"retry_after_s": 9}))
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(script) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(LinkRateLimitedError) as raised:
                await link.start_run(run_id=RUN, **start_kwargs())
            await link.aclose()
            return raised.value

    assert run_async(scenario()).retry_after_s == 9


# --------------------------------------------------------------------------
# Not connected, closed, too big, bad arguments
# --------------------------------------------------------------------------
def test_sending_while_not_connected_raises_instead_of_queueing_silently():
    store = open_db()
    new_run(store, RUN)

    async def scenario():
        link = make_link(store, "http://127.0.0.1:9")
        with pytest.raises(LinkNotConnectedError):
            await link.send_user_message(RUN, "hello")                 # never started
        with pytest.raises(LinkNotConnectedError):
            await link.ping()
        await link.aclose()
        with pytest.raises(LinkClosedError):
            await link.send_user_message(RUN, "hello")
        with pytest.raises(LinkClosedError):
            link.start()

    run_async(scenario())


def test_sending_during_a_reconnect_gap_raises():
    store = open_db()
    new_run(store, RUN)

    async def scenario():
        gate = asyncio.Event()

        async def sleeping(delay):                                     # hold the link in its backoff
            await gate.wait()

        async with FakeServer(idle_script(), idle_script()) as srv:
            link = make_link(store, srv, sleep=sleeping)
            link.start()
            await link.wait_connected(5)
            srv.connections[0].ws.transport.abort()
            await until(lambda: link.state == "reconnecting")
            with pytest.raises(LinkNotConnectedError):
                await link.send_user_message(RUN, "hello")
            gate.set()
            await link.wait_connected(5)
            await link.send_user_message(RUN, "hello")
            await link.aclose()
            return srv

    run_async(scenario())


def test_a_message_over_the_servers_limit_is_refused_before_it_is_sent():
    store = open_db()
    new_run(store, RUN)

    async def scenario():
        async with FakeServer(idle_script(welcome_env(max_message_bytes=2048))) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(LinkMessageTooLargeError):
                await link.send_user_message(RUN, "x" * 5000)
            await link.send_user_message(RUN, "short")                  # the connection is fine
            await link.aclose()
            return srv

    srv = run_async(scenario())
    assert [m["payload"]["text"] for m in srv.messages(0, "user_message")] == ["short"]


def test_the_limit_is_measured_in_utf8_bytes_not_characters():
    store = open_db()
    new_run(store, RUN)

    async def scenario():
        async with FakeServer(idle_script(welcome_env(max_message_bytes=2048))) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(LinkMessageTooLargeError):
                await link.send_user_message(RUN, "\u20ac" * 800)       # 800 characters, 2400 bytes
            await link.aclose()

    run_async(scenario())


def test_bad_arguments_are_refused_locally_and_nothing_is_sent():
    store = open_db()
    new_run(store, RUN)

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(ValueError):
                await link.start_run(run_id=RUN, **start_kwargs(cwd_label="/home/user/code/app"))    # a path, not a label
            with pytest.raises(ValueError):
                await link.start_run(run_id=RUN, **start_kwargs(cwd_label="C:\\code\\app"))
            with pytest.raises(ValueError):
                await link.send_user_message("../etc", "hi")                                          # not a run id
            with pytest.raises(ValueError):
                await link.send_approval(RUN, "call_1", decision="maybe")
            with pytest.raises(ValueError):
                await link.send_tool_stream(RUN, "call_1", stream="stdin", chunk="x")
            with pytest.raises(UnknownSessionError):
                await link.start_run(run_id="run_has_no_session", **start_kwargs())                   # session missing
            await link.aclose()
            return srv

    srv = run_async(scenario())
    assert [m["t"] for m in srv.messages(0)] == ["hello"]


def test_a_run_that_already_has_events_cannot_be_started_again():
    store = open_db()
    new_run(store, RUN)
    from m2c.store import append_event

    append_event(store, RUN, "server.run_event.state_changed",
                 {"server_seq": 1, "id": "x", "ts": "t", "payload": {"type": "state_changed", "state": "created"}})

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(ValueError):
                await link.start_run(run_id=RUN, **start_kwargs())      # use attach_run / resume_run for that
            await link.aclose()

    run_async(scenario())


def test_start_run_without_a_run_id_mints_a_uuid_and_creates_the_session():
    import uuid

    from m2c.store import get_session

    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            run_id = await link.start_run(project_path=PROJECT, wait=False, **start_kwargs(mode="plan", model_pref="auto"))
            await link.aclose()
            return srv, run_id

    srv, run_id = run_async(scenario())
    assert str(uuid.UUID(run_id)) == run_id                              # a canonical UUID
    session = get_session(store, run_id)
    assert (session.project_path, session.mode, session.model_pref) == (PROJECT, "plan", "auto")
    start = srv.messages(0, "run.start")[0]
    assert start["run_id"] == run_id and start["payload"]["profile"] == "code"      # build plan O8
    srv.assert_clean()


def test_without_a_run_id_a_project_path_is_required():
    store = open_db()

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            with pytest.raises(ValueError):
                await link.start_run(**start_kwargs())
            await link.aclose()

    run_async(scenario())


# --------------------------------------------------------------------------
# Everything the client can say is valid against the client's own schema copy
# --------------------------------------------------------------------------
def test_every_client_message_type_validates_against_the_schema_copy():
    pytest.importorskip("jsonschema", reason="install protocol/requirements.txt to validate against the schemas")
    store = open_db()
    new_run(store, RUN)

    async def scenario():
        async with FakeServer(idle_script()) as srv:
            assert srv.validator is not None
            link = make_link(store, srv, capabilities=("sandbox",))
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, wait=False, rules=[{"name": "style", "text": "Use tabs."}],
                                 history=[{"role": "user", "text": "hi"},
                                          {"role": "assistant", "text": "yo",
                                           "tool_calls": [{"call_id": "c1", "tool": "grep", "input": {"pattern": "x"}}]},
                                          {"role": "tool", "call_id": "c1", "ok": True, "output": "y"}],
                                 **start_kwargs(memory_index="notes", model_pref="auto"))
            await link.send_user_message(RUN, "hello")
            await link.send_tool_result(RUN, "c1", ok=True, output="out", truncated=False, trust="local_project",
                                        exit_code=0, duration_ms=12)
            await link.send_tool_result(RUN, "c2", ok=False, output="", truncated=True, trust="external")
            await link.send_tool_stream(RUN, "c1", stream="stdout", chunk="line")
            await link.send_approval(RUN, "c1", decision="allow", scope="session")
            await link.send_approval(RUN, "c2", decision="deny")
            await link.send_cancel(RUN)
            await link.ping()
            await link.resume_run(RUN, state=dict(profile="code", rules=[], tool_catalog_version="v0",
                                                  **start_kwargs(memory_index="", model_pref="auto")))
            await link.ping(nonce="x")
            await link.aclose()
            return srv

    srv = run_async(scenario())
    assert srv.schema_problems == [], srv.schema_problems
    seen = {m["t"] for m in srv.messages(0)}
    assert seen == CLIENT_TYPES, f"not exercised: {sorted(CLIENT_TYPES - seen)}"
