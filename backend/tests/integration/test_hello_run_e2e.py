"""
Patch I01 -- the walking skeleton, end to end, in CI.

Real: the device socket (S04) under uvicorn, S14's router and handlers, hello.py, the S05 adapter
(over HTTP to S05's FakeProvider), S06's run manager/buffer over their in-memory twins, S02's
credits, and on the client the genuine C04 DeviceLink, the REPL and the local SQLite store.
Fake: the database calls and Redis (see hello_testkit.py).

Each test is one scenario from the work package. Fake secrets are built at run time.
"""
import asyncio
import gc
import threading

import pytest

from codeagent import buffer, hello, ws as ws_module
from eo import credits as credits_module
from m2c.link import LinkDeviceRevokedError, recover_last_seq
from m2c.store import get_session, list_sessions
from tests.integration import hello_testkit as k
from tests.integration.hello_testkit import Session, blocking_reply, local_events, server_events, stack  # noqa: F401
from tests.unit.test_codeagent_adapter import Error, text_reply


def go(coro):
    return asyncio.run(coro)


def kinds(store, run_id):
    return [e.type for e in local_events(store, run_id)]


def k_idle(stack):
    return stack.idle()


def assert_logs_are_whole(stack, store, run_id):
    """The device's log and the server's buffer hold the same events, each seq once, none missing."""
    server = [m["seq"] for m in server_events(run_id)]
    assert server == list(range(1, len(server) + 1))
    local = [e.data["server_seq"] for e in local_events(store, run_id) if e.type.startswith("server.")]
    assert local == server                                         # same events, each once, in order
    # recover_last_seq raises unless the device's seqs are exactly 1, 2, 3, ... (no gap, no duplicate)
    assert recover_last_seq(store, run_id) == (len(server), False)


# ---------------------------------------------------------------------------------------------
# 1. a streamed reply, the ledger, the run row, the local store
# ---------------------------------------------------------------------------------------------

def test_a_message_gets_a_streamed_reply_and_one_ledger_row(stack):
    stack.provider.push(text_reply("Hello ", "world", prompt=120, completion=7, cached=20))

    async def scenario():
        async with Session(stack) as s:
            s.say("hi there")
            await s.reply_count(1)
            run_id = s.repl.run_id
            assert s.repl.replies == ["Hello world"]
            assert "Hello world" in s.text and "credits]" in s.text
            body = stack.provider.requests[0]["body"]
            assert [m["role"] for m in body["messages"]] == ["system", "user"]
            assert body["messages"][1]["content"] == "hi there"
            items = buffer.read_conversation(run_id).items
            assert items == [{"role": "user", "text": "hi there"}, {"role": "assistant", "text": "Hello world"}]
            assert stack.run_row(run_id).state == "running"       # still running for the next message
            assert buffer.read_conversation(run_id).state["cwd_label"] == "my-project"   # a label, not a path
            await s.leave()
            return run_id

    run_id = go(scenario())
    (row,) = stack.ledger.rows                                     # ONE row, numbers only, S02's figure
    assert row["surface"] == "m2c" and row["user_id"] == k.USER and row["run_id"] == run_id
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (120, 20, 7)
    assert row["credits"] == credits_module.credits_for(row["provider"], row["model"], 120, 20, 7) > 0
    assert set(row) <= {"user_id", "surface", "run_id", "provider", "model", "input_tokens",
                        "cached_input_tokens", "output_tokens", "credits"}
    assert stack.ledger.checks == [k.USER]
    store = stack.open_store()
    types = kinds(store, run_id)
    assert types.count("user.message") == 1 and types.count("assistant.message") == 1
    assert "server.run_event.text_delta" in types and "server.run_event.usage" in types
    assert get_session(store, run_id) is not None
    store.close()


def test_second_message_in_the_same_run_sees_the_first(stack):
    stack.provider.push(text_reply("One"))
    stack.provider.push(text_reply("Two"))

    async def scenario():
        async with Session(stack) as s:
            s.say("first")
            await s.reply_count(1)
            run_id = s.repl.run_id
            s.say("second")
            await s.reply_count(2)
            assert s.repl.run_id == run_id                         # one run, two turns
            await s.leave()
            return run_id

    run_id = go(scenario())
    second = stack.provider.requests[1]["body"]["messages"]
    assert [(m["role"], m["content"]) for m in second[1:]] == [("user", "first"), ("assistant", "One"),
                                                               ("user", "second")]
    assert [r["run_id"] for r in stack.ledger.rows] == [run_id, run_id]
    assert len(list_sessions(stack.open_store())) == 1


# ---------------------------------------------------------------------------------------------
# 2. quota exhausted
# ---------------------------------------------------------------------------------------------

def test_quota_exhausted_never_calls_the_model_and_pauses_the_run(stack):
    stack.ledger.exhausted = True

    async def scenario():
        async with Session(stack) as s:
            s.say("hello?")
            await s.until(lambda: s.repl.run_id is None and "credit pool" in s.text, what="the quota line")
            assert [e.code for e in s.errors] == ["quota_exhausted"]
            assert s.errors[0].details == {"reset_at": stack.ledger.resets_at}
            assert "next message starts a new one" in s.text
            await s.leave()

    go(scenario())
    assert stack.provider.requests == [] and stack.ledger.rows == []
    store = stack.open_store()
    (session,) = list_sessions(store)
    states = [e.data.get("payload", e.data).get("state") for e in local_events(store, session.id)
              if e.type == "server.run_event.state_changed"]
    assert "paused_budget" in states                               # announced after the error, as fixture 04 pins
    # the REPL then ended the parked run with cancel (a paused run takes no messages)
    assert stack.run_row(session.id).state == "cancelled"


def test_a_paused_run_does_not_trap_the_prompt(stack):
    stack.ledger.exhausted = True
    stack.provider.push(text_reply("Back again"))

    async def scenario():
        async with Session(stack) as s:
            s.say("one")
            await s.until(lambda: s.repl.run_id is None and len(s.errors) == 1, what="first quota error")
            stack.ledger.exhausted = False
            s.say("two")
            await s.reply_count(1)
            assert [e.code for e in s.errors] == ["quota_exhausted"]
            await s.leave()

    go(scenario())
    assert len(list_sessions(stack.open_store())) == 2


# ---------------------------------------------------------------------------------------------
# 3. disconnect in the middle of a reply, then the automatic resume
# ---------------------------------------------------------------------------------------------

def test_disconnect_mid_reply_then_resume_writes_the_reply_again(stack):
    gate, started = threading.Event(), threading.Event()
    stack.provider.push(blocking_reply(["Hel", "lo "], gate, started))
    stack.provider.push(text_reply("Hello ", "world", prompt=40, completion=3))

    async def scenario():
        async with Session(stack) as s:
            s.say("greet me")
            await s.until(lambda: "Hello " in s.text, what="the partial reply")
            run_id = s.repl.run_id
            s.drop_connection()
            await s.until(lambda: stack.run_row(run_id).state == "paused_disconnected", what="the pause")
            assert k_idle(stack)                                   # S14 cancelled the turn
            gate.set()                                             # let the fake provider notice the hang-up
            assert stack.provider.client_hung_up.wait(10), "the provider stream was left open"
            s.allow_reconnect()
            await s.reply_count(1)                                 # the link reconnected and resumed by itself
            assert s.repl.replies == ["Hello world"] and s.repl.restarts == 1
            assert "being written again" in s.text
            assert stack.run_row(run_id).state == "running"
            assert_logs_are_whole(stack, s.store, run_id)
            assert buffer.read_conversation(run_id).items == [{"role": "user", "text": "greet me"},
                                                              {"role": "assistant", "text": "Hello world"}]
            for req in stack.provider.requests:                    # the user message was never sent twice
                assert [m["role"] for m in req["body"]["messages"]].count("user") == 1
            types = kinds(s.store, run_id)
            assert types.count("assistant.message") == 1           # the final reply, once
            assert types.count("server.run_event.notice") == 1     # the partial deltas stay in the log, with the notice
            await s.leave()

    go(scenario())
    assert len(stack.provider.requests) == 2
    assert len(stack.ledger.rows) == 1                             # only the completed call is metered
    assert stack.ledger.rows[0]["output_tokens"] == 3


def test_resume_after_a_finished_reply_restarts_nothing(stack):
    stack.provider.push(text_reply("Done"))

    async def scenario():
        async with Session(stack) as s:
            s.say("x")
            await s.reply_count(1)
            run_id = s.repl.run_id
            s.drop_connection()
            await s.until(lambda: stack.run_row(run_id).state == "paused_disconnected", what="the pause")
            s.allow_reconnect()
            await s.until(lambda: len(s.conns) >= 2, what="the reconnect")
            await s.until(lambda: stack.run_row(run_id).state == "running", what="the resume")
            await asyncio.sleep(0.3)
            assert s.repl.restarts == 0 and s.repl.replies == ["Done"]
            assert len(stack.provider.requests) == 1 and len(stack.ledger.rows) == 1
            await s.leave()

    go(scenario())


# ---------------------------------------------------------------------------------------------
# 4. Ctrl-C, cancellation, nothing left behind
# ---------------------------------------------------------------------------------------------

def test_ctrl_c_cancels_the_run_and_the_next_message_starts_a_new_one(stack):
    gate, started = threading.Event(), threading.Event()
    stack.provider.push(blocking_reply(["Thin", "king "], gate, started))
    stack.provider.push(text_reply("Fresh ", "start"))

    async def scenario():
        async with Session(stack) as s:
            s.say("first")
            await s.until(lambda: "Thinking " in s.text, what="streaming")
            first = s.repl.run_id
            s.repl.interrupt()                                     # Ctrl-C
            await s.until(lambda: s.repl.run_id is None and "stopped" in s.text, what="the stop")
            await s.until(lambda: stack.run_row(first).state == "cancelled", what="the cancelled run")
            k.wait_for(lambda: k_idle(stack), 10, "the turn task to end")
            gate.set()
            assert stack.provider.client_hung_up.wait(10), "the provider stream was left open"
            assert stack.ledger.rows == []                         # a cancelled turn is not metered
            gc.collect()
            assert len(hello._LOCKS) == 0
            s.say("second")
            await s.reply_count(1)
            second = s.repl.run_id
            assert second is not None and second != first          # a NEW run
            assert s.repl.replies == ["Fresh start"]
            await s.leave()
            return first, second

    first, second = go(scenario())
    assert [r["run_id"] for r in stack.ledger.rows] == [second]
    assert {x.id for x in list_sessions(stack.open_store())} == {first, second}


def test_exit_cancels_the_run_cleanly(stack):
    stack.provider.push(text_reply("ok"))

    async def scenario():
        async with Session(stack) as s:
            s.say("a")
            await s.reply_count(1)
            run_id = s.repl.run_id
            assert await s.leave() == 0
            return run_id

    run_id = go(scenario())
    assert stack.run_row(run_id).state == "cancelled"
    assert k_idle(stack)
    with pytest.raises(Exception):
        buffer.read_conversation(run_id)                           # cancel deleted the server buffer


# ---------------------------------------------------------------------------------------------
# 5. the restarted turn: cancelled by cancel, and by a second disconnect
# ---------------------------------------------------------------------------------------------

async def _into_a_restarted_turn(stack, s, gate_a, started_b):
    s.say("restart me")
    await s.until(lambda: "Hello " in s.text, what="the partial reply")
    run_id = s.repl.run_id
    s.drop_connection()
    await s.until(lambda: stack.run_row(run_id).state == "paused_disconnected", what="the pause")
    gate_a.set()
    assert stack.provider.client_hung_up.wait(10)
    stack.provider.client_hung_up.clear()
    s.allow_reconnect()
    await s.until(lambda: started_b.is_set() and run_id in hello._RESTARTS, what="the restarted turn")
    return run_id


def test_the_restarted_turn_is_cancelled_by_cancel(stack):
    ga, gb, sa, sb = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    stack.provider.push(blocking_reply(["Hel", "lo "], ga, sa))
    stack.provider.push(blocking_reply(["Hel", "lo "], gb, sb))

    async def scenario():
        async with Session(stack) as s:
            run_id = await _into_a_restarted_turn(stack, s, ga, sb)
            s.repl.interrupt()                                     # cancel while the restarted turn streams
            await s.until(lambda: stack.run_row(run_id).state == "cancelled", what="the cancelled run")
            k.wait_for(lambda: k_idle(stack), 10, "the restarted turn to end")
            gb.set()
            assert stack.provider.client_hung_up.wait(10), "the restarted turn left its stream open"
            assert stack.ledger.rows == []
            await s.leave()

    go(scenario())


def test_the_restarted_turn_is_cancelled_by_a_second_disconnect_and_resume_tries_again(stack):
    ga, gb, sa, sb = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    stack.provider.push(blocking_reply(["Hel", "lo "], ga, sa))
    stack.provider.push(blocking_reply(["Hel", "lo "], gb, sb))
    stack.provider.push(text_reply("Hello ", "world", completion=4))

    async def scenario():
        async with Session(stack) as s:
            run_id = await _into_a_restarted_turn(stack, s, ga, sb)
            s.drop_connection()                                    # a second drop, inside the restarted turn
            await s.until(lambda: stack.run_row(run_id).state == "paused_disconnected", what="the second pause")
            k.wait_for(lambda: k_idle(stack), 10, "the restarted turn to be cancelled")
            gb.set()
            assert stack.provider.client_hung_up.wait(10)
            s.allow_reconnect()
            await s.reply_count(1)                                 # the third attempt completes
            assert s.repl.replies == ["Hello world"] and s.repl.restarts == 2
            assert_logs_are_whole(stack, s.store, run_id)
            assert buffer.read_conversation(run_id).items == [{"role": "user", "text": "restart me"},
                                                              {"role": "assistant", "text": "Hello world"}]
            await s.leave()

    go(scenario())
    assert len(stack.ledger.rows) == 1 and stack.ledger.rows[0]["output_tokens"] == 4


def test_a_message_typed_during_a_restarted_turn_waits_for_it(stack):
    """The restarted turn never runs beside a normal one. S14 would start a turn for a message that arrives
    mid-restart; hello's per-run lock makes it wait, and it then sees the finished reply."""
    ga, gb, sa, sb = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    stack.provider.push(blocking_reply(["Hel", "lo "], ga, sa))
    stack.provider.push(blocking_reply(["Hello "], gb, sb))
    stack.provider.push(text_reply("Second"))

    async def scenario():
        async with Session(stack) as s:
            run_id = await _into_a_restarted_turn(stack, s, ga, sb)
            await s.link.send_user_message(run_id, "typed meanwhile")
            await asyncio.sleep(0.3)
            assert len(stack.provider.requests) == 2               # no third call beside the restarted turn
            gb.set()
            await s.until(lambda: len(stack.provider.requests) == 3, what="the queued turn")
            await s.until(lambda: "Second" in s.text, what="its reply")
            roles = [m["role"] for m in stack.provider.requests[2]["body"]["messages"]][1:]
            assert roles == ["user", "assistant", "user"]
            await s.leave()

    go(scenario())


# ---------------------------------------------------------------------------------------------
# 6. failures that must not trap or leak
# ---------------------------------------------------------------------------------------------

def test_a_ledger_failure_does_not_stop_the_reply(stack):
    stack.ledger.fail_commit = True
    stack.provider.push(text_reply("Still here"))

    async def scenario():
        async with Session(stack) as s:
            s.say("x")
            await s.reply_count(1)
            assert s.repl.replies == ["Still here"] and "credits]" in s.text
            await s.leave()

    go(scenario())
    assert stack.ledger.rows == []


def test_a_model_failure_pauses_the_run_and_the_prompt_recovers(stack):
    for _ in range(4):
        stack.provider.push(Error(500))

    async def scenario():
        async with Session(stack) as s:
            s.say("x")
            await s.until(lambda: s.repl.run_id is None and "[this run is over" in s.text, what="the failure")
            assert [e.code for e in s.errors] == ["provider_unavailable"]
            assert stack.ledger.rows == []
            stack.provider.script.clear()
            stack.provider.push(text_reply("Recovered"))
            s.say("again")
            await s.reply_count(1)
            assert s.repl.replies == ["Recovered"]
            await s.leave()

    go(scenario())


def test_no_provider_key_is_provider_unavailable(stack, monkeypatch):
    monkeypatch.setattr(hello, "_ADAPTER", None)
    for _, _, base in hello._PROVIDERS:
        monkeypatch.delenv(base, raising=False)
        for n in range(1, hello._MAX_KEY_SUFFIX + 1):
            monkeypatch.delenv(f"{base}_{n}", raising=False)

    async def scenario():
        async with Session(stack) as s:
            s.say("x")
            await s.until(lambda: s.errors, what="the error")
            assert s.errors[0].code == "provider_unavailable"
            await s.leave()

    go(scenario())
    assert stack.provider.requests == [] and stack.ledger.rows == []


# ---------------------------------------------------------------------------------------------
# 7. revocation
# ---------------------------------------------------------------------------------------------

def test_revoking_the_device_closes_the_session(stack):
    stack.provider.push(text_reply("hi"))
    assert ws_module.REVALIDATE_INTERVAL_SECONDS <= 30            # S04's real setting: "within about a minute"

    async def scenario():
        async with Session(stack) as s:
            s.say("x")
            await s.reply_count(1)
            run_id = s.repl.run_id
            stack.revoke_device()
            status = await asyncio.wait_for(s.task, 15)            # the prompt ends by itself
            assert status == 4 and "revoked" in s.text
            assert isinstance(s.link.failure, LinkDeviceRevokedError)
            await s.until(lambda: stack.run_row(run_id).state == "paused_disconnected", what="the run to pause")

    go(scenario())


def test_revoking_while_idle_at_the_prompt_ends_the_loop(stack):
    async def scenario():
        async with Session(stack) as s:
            stack.revoke_device()
            assert await asyncio.wait_for(s.task, 15) == 4

    go(scenario())
