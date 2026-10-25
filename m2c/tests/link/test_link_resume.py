"""
m2c/tests/link/test_link_resume.py -- Patch C04: reconnect and run.resume, the
"Done when" test of this work package:

    killing the connection mid-stream resumes without duplicate or missing events.

The server side is ``ReplayRun`` (link_fakes.py), which follows the rule of
protocol/README.md section 5: on run.resume{last_seq} re-deliver every event
with seq > last_seq verbatim.  What is checked is the DEVICE LOG, the source of
truth (build plan O1): after however many kills, the stored server events are
exactly seq 1..N once each, in order, and the local sequence has no holes.

Also here: duplicates are ignored by seq, a gap is never stored out of order
(the client asks again from last_seq), last_seq is recovered from the store
after a restart -- including a real SIGKILL of the client process -- and the
details of which runs are resumed and how.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time

import pytest

from link_fakes import *  # noqa: F401,F403  (autouse fixtures; see link_fakes.py)

from m2c.link import LinkStoreError, recover_last_seq
from m2c.store import StoreError

RUN = "run_resume_test_01"


def start_kwargs(**over):
    kwargs = dict(mode="ask", cwd_label="demo-project", instructions="Be concise.", memory_index="", model_pref="auto")
    kwargs.update(over)
    return kwargs


def assert_log_is_complete_in_order_and_unique(store, run_id, events):
    stored = server_events(store, run_id)
    assert [e.data["server_seq"] for e in stored] == [m["seq"] for m in events], "missing, duplicated or reordered"
    assert [e.data["id"] for e in stored] == [m["id"] for m in events]
    assert [e.data["payload"] for e in stored] == [m["payload"] for m in events]
    assert [e.seq for e in read_events(store, run_id)] == list(range(1, len(events) + 1)), "the local log has a hole"


def golden_03_events():
    return stream_lines(fixture_lines("03_disconnect_resume.jsonl"))


def drive_to_completion(store, events, replay, *, run_id=RUN, **link_kwargs):
    """Start a run against ``replay`` and wait until its last event is stored."""

    async def scenario():
        async with FakeServer(replay.script, repeat_last=True) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec, **link_kwargs)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=run_id, **start_kwargs())
            await until(lambda: link.run_info(run_id).terminal and link.run_info(run_id).last_seq == len(events),
                        timeout=20, what="the whole stream")
            await link.aclose()
            return srv, rec, link

    return run_async(scenario(), timeout=40)


# --------------------------------------------------------------------------
# The key test
# --------------------------------------------------------------------------
KILL_PLANS = [
    pytest.param([1], id="after-the-first-event"),
    pytest.param([5], id="mid-stream"),
    pytest.param([17], id="late"),
    pytest.param([59], id="one-before-the-end"),
    pytest.param([10, 15, 20], id="three-kills"),
    pytest.param([1, 1, 1, 1, 1], id="five-kills-one-event-each"),
    pytest.param([30, 5], id="two-kills"),
]


@pytest.mark.parametrize("plan", KILL_PLANS)
def test_killing_the_connection_mid_stream_resumes_without_duplicate_or_missing_events(plan):
    events = synthetic_events(RUN, 60)
    replay = ReplayRun(events, kill_after=plan)
    store = open_db()
    new_run(store, RUN)

    srv, rec, link = drive_to_completion(store, events, replay)

    srv.assert_clean()
    assert_log_is_complete_in_order_and_unique(store, RUN, events)
    assert rec.seqs(RUN) == list(range(1, 61)), "the callbacks must see each event once, in order"
    assert len(srv.connections) >= len([p for p in plan if p is not None]) + 1
    # every connection after the first resumed, with the run id in the envelope, and the cursor never went backwards
    resumes = srv.messages(t="run.resume")
    assert resumes and all(m["run_id"] == RUN and "seq" not in m for m in resumes)
    cursors = [m["payload"]["last_seq"] for m in resumes]
    assert cursors == sorted(cursors)
    assert replay.start_requests == 1                        # run.start is sent once; resumes do the rest


@pytest.mark.parametrize("cut", range(1, 18))
def test_the_golden_transcripts_stream_cut_at_every_point(cut):
    """Cut the real P00 stream (03_disconnect_resume: 18 events, tool proposals
    and all) after each possible event; the log is the transcript every time."""
    events = golden_03_events()
    assert len(events) == 18
    run_id = events[0]["run_id"]
    replay = ReplayRun(events, kill_after=[cut])
    store = open_db()
    new_run(store, run_id)
    srv, rec, link = drive_to_completion(store, events, replay, run_id=run_id)
    srv.assert_clean()
    assert_log_is_complete_in_order_and_unique(store, run_id, events)
    assert rec.seqs(run_id) == [m["seq"] for m in events]
    # each proposal is delivered exactly once however the stream was cut
    assert [e.payload["call_id"] for e in rec.tool_proposed] == ["call_grep_1", "call_grep_2"]


# --------------------------------------------------------------------------
# Duplicates and gaps
# --------------------------------------------------------------------------
def test_events_redelivered_after_a_resume_are_ignored_by_seq():
    events = synthetic_events(RUN, 40)
    replay = ReplayRun(events, kill_after=[12, 25], resume_overlap=5)    # re-sends 5 events we already have
    store = open_db()
    new_run(store, RUN)

    srv, rec, link = drive_to_completion(store, events, replay)

    srv.assert_clean()
    assert_log_is_complete_in_order_and_unique(store, RUN, events)
    assert rec.seqs(RUN) == list(range(1, 41))                # callbacks were not repeated either
    assert link.stats.duplicates >= 5


def test_duplicate_tool_proposals_are_delivered_once():
    events = golden_03_events()
    run_id = events[0]["run_id"]
    replay = ReplayRun(events, kill_after=[12], resume_overlap=12)       # replay all of it again
    store = open_db()
    new_run(store, run_id)
    srv, rec, link = drive_to_completion(store, events, replay, run_id=run_id)
    assert [e.payload["call_id"] for e in rec.tool_proposed] == ["call_grep_1", "call_grep_2"]
    assert_log_is_complete_in_order_and_unique(store, run_id, events)


def test_a_gap_is_never_stored_out_of_order_and_is_healed_by_resuming_from_last_seq():
    events = synthetic_events(RUN, 12)
    snapshots = {}

    async def script(conn):
        await do_handshake(conn)
        await conn.recv(timeout=None)                              # run.start
        for ev in events[:3] + events[4:8]:                       # 1, 2, 3, then 5, 6, 7, 8: seq 4 is missing
            await conn.send(ev)
        resume = await conn.recv(timeout=5)                       # the client must ask again from 3
        snapshots["resume"] = resume
        snapshots["stored_while_gap_open"] = stored_server_seqs(store, RUN)
        for ev in events[3:]:
            await conn.send(ev)

    store = open_db()
    new_run(store, RUN)

    async def scenario():
        async with FakeServer(script) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, **start_kwargs())
            await until(lambda: link.run_info(RUN).last_seq == 12, what="the gap to be healed")
            await link.aclose()
            return srv, rec, link

    srv, rec, link = run_async(scenario())
    srv.assert_clean()
    assert snapshots["resume"]["t"] == "run.resume"
    assert snapshots["resume"]["run_id"] == RUN and snapshots["resume"]["payload"] == {"last_seq": 3}
    assert snapshots["stored_while_gap_open"] == [1, 2, 3]       # 5..8 were NOT stored
    assert_log_is_complete_in_order_and_unique(store, RUN, events)
    assert rec.seqs(RUN) == list(range(1, 13))
    assert link.stats.gaps >= 1


def test_a_gap_that_persists_asks_once_not_once_per_message_then_again_after_the_retry_interval():
    events = synthetic_events(RUN, 40)
    store = open_db()
    new_run(store, RUN)

    async def script(conn):
        await do_handshake(conn)
        await conn.recv(timeout=None)                              # run.start
        await conn.send(events[0])
        for ev in events[2:30]:                                    # seq 2 never comes: 28 out-of-order events
            await conn.send(ev)
        await answer_pings(conn)

    async def scenario():
        fake_time = FastTime()
        async with FakeServer(script) as srv:
            link = make_link(store, srv, fake_time=fake_time, resume_retry_s=10.0)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, wait=False, **start_kwargs())
            await until(lambda: link.stats.gaps >= 28, what="28 out-of-order events")
            await asyncio.sleep(0.05)
            first = len(srv.messages(t="run.resume"))
            fake_time.advance(11)                                  # the retry interval passes
            await srv.connections[0].send(events[30])              # one more out-of-order event
            await until(lambda: len(srv.messages(t="run.resume")) == 2, what="the second request")
            await link.aclose()
            return first, srv

    first, srv = run_async(scenario())
    assert first == 1
    assert [m["payload"]["last_seq"] for m in srv.messages(t="run.resume")] == [1, 1]
    assert stored_server_seqs(store, RUN) == [1]


def test_a_malformed_event_that_arrives_in_order_is_recorded_as_a_marker_and_the_stream_goes_on():
    """Skipping it would create a hole; stalling on it would loop forever
    (the server replays the same bad event on every resume).  The log gets a
    content-free marker with the seq, the callbacks are not called."""
    events = synthetic_events(RUN, 8)
    broken = dict(events[3])
    broken["payload"] = {"type": "text_delta"}                       # text is required
    sent = events[:3] + [broken] + events[4:]
    store = open_db()
    new_run(store, RUN)

    async def script(conn):
        await do_handshake(conn)
        await conn.recv(timeout=None)
        for ev in sent:
            await conn.send(ev)
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(script) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, **start_kwargs())
            await until(lambda: link.run_info(RUN).last_seq == 8)
            await link.aclose()
            return srv, rec, link

    srv, rec, link = run_async(scenario())
    stored = server_events(store, RUN)
    assert [e.data["server_seq"] for e in stored] == list(range(1, 9))
    assert stored[3].type == "server.invalid" and "payload" not in stored[3].data
    assert rec.seqs(RUN) == [1, 2, 3, 5, 6, 7, 8]
    assert srv.messages(t="run.resume") == [] and link.stats.invalid == 1


def test_an_unknown_run_event_subtype_advances_seq_and_unknown_fields_are_dropped_before_storing():
    """Tolerant reader (protocol/README.md section 8): ignore what you do not know,
    but a stream event still consumes its seq."""
    events = synthetic_events(RUN, 6)
    future = env("run_event", {"type": "from_the_future", "blob": "x" * 50}, run_id=RUN, seq=3)
    extra = dict(events[3])
    extra["payload"] = {"type": "text_delta", "text": "hello", "added_later": "x"}
    sent = events[:2] + [future, extra] + events[4:]
    store = open_db()
    new_run(store, RUN)

    async def script(conn):
        await do_handshake(conn)
        await conn.recv(timeout=None)
        for ev in sent:
            await conn.send(ev)
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(script) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, **start_kwargs())
            await until(lambda: link.run_info(RUN).last_seq == 6)
            await link.aclose()
            return rec

    rec = run_async(scenario())
    stored = server_events(store, RUN)
    assert [e.data["server_seq"] for e in stored] == [1, 2, 3, 4, 5, 6]
    assert stored[2].type == "server.run_event.unknown"
    assert stored[3].data["payload"] == {"type": "text_delta", "text": "hello"}
    assert rec.seqs(RUN) == [1, 2, 4, 5, 6]


def test_events_for_a_run_this_link_does_not_know_are_dropped_and_not_stored():
    events = synthetic_events("run_somebody_else", 5)
    store = open_db()
    new_run(store, RUN)

    async def script(conn):
        await do_handshake(conn)
        for ev in events:
            await conn.send(ev)
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(script) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await until(lambda: link.stats.unknown_run == 5)
            await link.aclose()
            return rec, link

    rec, link = run_async(scenario())
    assert rec.events == [] and read_events(store, RUN) == []
    assert link.state == "closed"


# --------------------------------------------------------------------------
# Restart: last_seq comes back from the store
# --------------------------------------------------------------------------
def test_after_a_restart_last_seq_is_recovered_from_the_store_and_resume_continues_from_it():
    events = synthetic_events(RUN, 50)
    store = open_db()
    new_run(store, RUN)

    async def first_process():
        async def script(conn):
            await do_handshake(conn)
            await conn.recv(timeout=None)
            for ev in events[:20]:
                await conn.send(ev)
            await answer_pings(conn)

        async with FakeServer(script) as srv:
            link = make_link(store, srv)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, **start_kwargs())
            await until(lambda: link.run_info(RUN).last_seq == 20)
            await link.aclose()

    run_async(first_process())
    store.close()

    # "restart": a new process would open the database again and attach.
    store2 = open_db()
    assert recover_last_seq(store2, RUN) == (20, False)

    async def second_process():
        replay = ReplayRun(events)
        async with FakeServer(replay.script) as srv:
            rec = Recorder()
            link = make_link(store2, srv, rec=rec)
            info = link.attach_run(RUN)
            assert (info.last_seq, info.terminal, info.lost) == (20, False, False)
            link.start()
            await link.wait_connected(5)
            await until(lambda: rec.completed(RUN), what="the resumed stream")
            await link.aclose()
            return srv, replay, rec

    srv, replay, rec = run_async(second_process())
    srv.assert_clean()
    assert [m["t"] for m in srv.messages(0)][:2] == ["hello", "run.resume"]
    assert srv.messages(0, "run.resume")[0]["payload"] == {"last_seq": 20}
    assert replay.start_requests == 0                          # a restart resumes; it does not start again
    assert_log_is_complete_in_order_and_unique(store2, RUN, events)
    assert rec.seqs(RUN) == list(range(21, 51))                # only the new events reach the callbacks


def test_recover_last_seq_ignores_the_clients_own_events_and_finds_the_tail_in_a_long_log():
    from m2c.store import append_event

    store = open_db()
    new_run(store, RUN)
    assert recover_last_seq(store, RUN) == (0, False)
    for i in range(1, 6):
        append_event(store, RUN, "server.run_event.text_delta", {"server_seq": i, "id": "x", "ts": "t", "payload": {}})
    for i in range(300):                                           # a long tail of the client's own events
        append_event(store, RUN, "user_message", {"text": f"m{i}"})
    assert recover_last_seq(store, RUN) == (5, False)
    append_event(store, RUN, "server.run_event.state_changed",
                 {"server_seq": 6, "id": "y", "ts": "t", "payload": {"type": "state_changed", "state": "completed"}})
    append_event(store, RUN, "tool_result", {"call_id": "c"})
    assert recover_last_seq(store, RUN) == (6, True)


def test_a_log_whose_server_seqs_do_not_run_in_order_is_refused_not_trusted():
    """Fail closed: if the log were edited (or has a bug), resuming from a
    wrong cursor would lose events silently."""
    from m2c.store import append_event

    store = open_db()
    new_run(store, RUN)
    for seq in (1, 2, 4):
        append_event(store, RUN, "server.run_event.text_delta", {"server_seq": seq, "id": "x", "ts": "t", "payload": {}})
    with pytest.raises(StoreError):
        recover_last_seq(store, RUN)


# --------------------------------------------------------------------------
# Which runs are resumed, and how
# --------------------------------------------------------------------------
def test_each_active_run_is_resumed_with_its_own_cursor_right_after_welcome():
    a, b = "run_alpha_000001", "run_bravo_000002"
    ev_a, ev_b = synthetic_events(a, 10), synthetic_events(b, 10)
    store = open_db()
    new_run(store, a)
    new_run(store, b)

    async def conn0(conn):
        await do_handshake(conn)
        await conn.recv(timeout=None)                              # run.start a
        for ev in ev_a[:4]:
            await conn.send(ev)
        await conn.recv(timeout=None)                              # run.start b
        for ev in ev_b[:7]:
            await conn.send(ev)
        await asyncio.sleep(0.1)
        await conn.kill()

    async def conn1(conn):
        await do_handshake(conn)
        for _ in range(2):
            msg = await conn.recv(timeout=5)
            assert msg["t"] == "run.resume"
            source = ev_a if msg["run_id"] == a else ev_b
            for ev in source[msg["payload"]["last_seq"]:]:
                await conn.send(ev)

    async def scenario():
        async with FakeServer(conn0, conn1) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=a, **start_kwargs())
            await link.start_run(run_id=b, **start_kwargs())
            await until(lambda: rec.completed(a) and rec.completed(b), what="both runs")
            await link.aclose()
            return srv, rec

    srv, rec = run_async(scenario())
    srv.assert_clean()
    second = srv.messages(1)
    assert [m["t"] for m in second] == ["hello", "run.resume", "run.resume"]          # nothing else in between
    cursors = {m["run_id"]: m["payload"]["last_seq"] for m in second[1:]}
    assert cursors == {a: 4, b: 7}
    assert_log_is_complete_in_order_and_unique(store, a, ev_a)
    assert_log_is_complete_in_order_and_unique(store, b, ev_b)


def test_a_finished_run_is_not_resumed():
    events = synthetic_events(RUN, 8)
    replay = ReplayRun(events)
    store = open_db()
    new_run(store, RUN)

    async def scenario():
        async with FakeServer(replay.script, repeat_last=True) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, **start_kwargs())
            await until(lambda: rec.completed(RUN))
            srv.connections[0].ws.transport.abort()
            await until(lambda: len(srv.connections) == 2 and link.state == "connected", what="the reconnect")
            await asyncio.sleep(0.1)
            await link.aclose()
            return srv

    srv = run_async(scenario())
    assert [m["t"] for m in srv.messages(1)] == ["hello"]       # nothing follows a terminal state: no resume


def test_a_run_the_server_does_not_know_is_reported_and_not_resumed_in_a_loop():
    store = open_db()
    new_run(store, RUN)
    state = {"payload": None}

    async def first(conn):
        await do_handshake(conn)
        await conn.recv(timeout=None)                              # run.start
        await conn.send(synthetic_events(RUN, 5)[0])
        await asyncio.sleep(0.05)
        await conn.kill()

    async def second(conn):                                        # the automatic resume: not found
        await do_handshake(conn)
        resume = await conn.recv(timeout=5)
        await conn.send(error_env("run_not_found", run_id=RUN, ref_id=resume["id"]))
        await answer_pings(conn)

    async def third(conn):                                         # nothing is sent until the caller decides
        await do_handshake(conn)
        state["payload"] = await conn.recv(timeout=5)
        await answer_pings(conn)

    async def scenario():
        async with FakeServer(first, second, third) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, **start_kwargs())
            await until(lambda: link.run_info(RUN).lost, what="run_not_found")
            assert [e.code for e in rec.errors] == ["run_not_found"]
            srv.connections[1].ws.transport.abort()                # lose the connection again
            await until(lambda: len(srv.connections) == 3 and link.state == "connected")
            await asyncio.sleep(0.1)
            second_reconnect = [m["t"] for m in srv.messages(2)]
            # the caller decides what to do with a lost run: re-send its history from the device log
            await link.resume_run(RUN, state=dict(profile="code", rules=[], tool_catalog_version="v0",
                                                  **start_kwargs(history=[{"role": "user", "text": "hi"}])))
            await until(lambda: state["payload"] is not None)
            await link.aclose()
            return srv, second_reconnect, link

    srv, second_reconnect, link = run_async(scenario())
    assert second_reconnect == ["hello"]                            # no automatic second resume for a lost run
    resume = state["payload"]
    assert resume["t"] == "run.resume" and resume["payload"]["last_seq"] == 1
    assert resume["payload"]["state"]["history"] == [{"role": "user", "text": "hi"}]
    assert link.run_info(RUN).lost is False                         # explicit resume clears the mark
    srv.assert_clean()


def test_the_resume_state_hook_attaches_state_and_a_failing_hook_does_not_stop_the_resume():
    events = synthetic_events(RUN, 6)
    store = open_db()
    new_run(store, RUN)
    calls = []

    def hook(run_id):
        calls.append(run_id)
        if len(calls) == 1:
            return dict(profile="code", mode="ask", cwd_label="demo-project", instructions="", rules=[],
                        memory_index="", tool_catalog_version="v0", model_pref="auto",
                        history=[{"role": "user", "text": "hello"}])
        raise RuntimeError("the device log could not be read")

    async def script(conn):
        await do_handshake(conn)
        if conn.index == 0:
            await conn.recv(timeout=None)
            await conn.send(events[0])
            await asyncio.sleep(0.05)
            await conn.kill()
        else:
            await conn.recv(timeout=5)
            await answer_pings(conn)

    async def scenario():
        async with FakeServer(script, script, script) as srv:
            link = make_link(store, srv, resume_state=hook)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, **start_kwargs())
            await until(lambda: len(srv.connections) == 2 and srv.messages(1, "run.resume"))
            await until(lambda: link.state == "connected")
            srv.connections[1].ws.transport.abort()
            await until(lambda: len(srv.connections) == 3 and srv.messages(2, "run.resume"))
            await link.aclose()
            return srv

    srv = run_async(scenario())
    first, second = srv.messages(1, "run.resume")[0], srv.messages(2, "run.resume")[0]
    assert first["payload"]["state"]["history"] == [{"role": "user", "text": "hello"}]
    assert first["payload"]["last_seq"] == 1
    assert second["payload"] == {"last_seq": 1}                     # the hook failed: resume without state
    assert calls == [RUN, RUN]


# --------------------------------------------------------------------------
# The store failing is final, and leaves the log consistent
# --------------------------------------------------------------------------
def test_a_store_failure_stops_the_link_without_a_partial_event(monkeypatch):
    events = synthetic_events(RUN, 10)
    store = open_db()
    new_run(store, RUN)
    from m2c.link import client as link_client

    real_append = link_client.append_event
    calls = {"n": 0}

    def failing_append(conn, session_id, event_type, data, **kw):
        calls["n"] += 1
        if calls["n"] == 4:
            raise StoreError("disk is full")
        return real_append(conn, session_id, event_type, data, **kw)

    monkeypatch.setattr(link_client, "append_event", failing_append)
    replay = ReplayRun(events)

    async def scenario():
        async with FakeServer(replay.script, repeat_last=True) as srv:
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            link.start()
            await link.wait_connected(5)
            await link.start_run(run_id=RUN, wait=False, **start_kwargs())
            with pytest.raises(LinkStoreError):
                await link.wait_closed()
            await asyncio.sleep(0.1)
            return srv, rec, link

    srv, rec, link = run_async(scenario())
    assert link.state == "failed" and len(srv.connections) == 1       # not retried: the store is not a network blip
    assert stored_server_seqs(store, RUN) == [1, 2, 3]                # the failed event left nothing behind
    assert link.run_info(RUN).last_seq == 3
    assert rec.seqs(RUN) == [1, 2, 3]                                 # and was never reported as received
    assert store.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# --------------------------------------------------------------------------
# A real crash: SIGKILL the client process mid-stream
# --------------------------------------------------------------------------
CHILD = textwrap.dedent(
    """
    import asyncio, sys, uuid
    from m2c.auth.credentials import Credential, Secret
    from m2c.link import DeviceLink
    from m2c.store import create_session, open_db

    url, run_id = sys.argv[1], sys.argv[2]

    async def main():
        store = open_db()
        create_session(store, project_path="/home/user/code/app", mode="ask", session_id=run_id)
        cred = Credential(Secret("m2c-test-cred-" + "x" * 30), str(uuid.uuid4()), url)
        link = DeviceLink(store, credential_loader=lambda: cred, keepalive_interval_s=None)
        link.start()
        await link.wait_connected(10)
        await link.start_run(run_id=run_id, mode="ask", cwd_label="demo-project", instructions="")
        await asyncio.sleep(120)

    asyncio.run(main())
    """
)


def test_a_sigkill_of_the_client_mid_stream_leaves_a_log_that_resumes_cleanly(tmp_path):
    events = synthetic_events(RUN, 400)
    replay = ReplayRun(events, delay_s=0.004)                       # about 1.6 s end to end: time to kill it
    script_path = tmp_path / "child.py"
    script_path.write_text(CHILD, encoding="utf-8")

    async def scenario():
        async with FakeServer(replay.script, repeat_last=True) as srv:
            env_vars = dict(os.environ)
            env_vars["PYTHONPATH"] = str(PKG_DIR.parent)
            child = subprocess.Popen([sys.executable, str(script_path), srv.server_url, RUN],
                                     env=env_vars, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                watcher = open_db()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 20
                while len(read_events(watcher, RUN)) < 40:
                    if child.poll() is not None:
                        raise AssertionError(f"the child exited early: {child.stderr.read().decode()[-800:]}")
                    if loop.time() > deadline:
                        raise AssertionError("the child never stored 40 events")
                    await asyncio.sleep(0.01)
            finally:
                child.kill()                                         # SIGKILL: no cleanup of any kind
                child.wait(timeout=10)
            stored_before = stored_server_seqs(watcher, RUN)
            watcher.close()

            # A second process life: attach, resume, finish.
            store = open_db()
            rec = Recorder()
            link = make_link(store, srv, rec=rec)
            info = link.attach_run(RUN)
            link.start()
            await link.wait_connected(5)
            await until(lambda: rec.completed(RUN), timeout=20, what="the resumed stream")
            await link.aclose()
            return srv, store, stored_before, info

    srv, store, stored_before, info = run_async(scenario(), timeout=60)
    assert 40 <= len(stored_before) < 400, "the kill should land mid-stream"
    assert stored_before == list(range(1, len(stored_before) + 1)), "a SIGKILL left a hole or a duplicate"
    assert info.last_seq == stored_before[-1]
    assert srv.messages(None, "run.resume")[-1]["payload"] == {"last_seq": info.last_seq}
    assert_log_is_complete_in_order_and_unique(store, RUN, events)
    assert store.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
