"""
m2c/tests/link/test_link_golden.py -- Patch C04: replay the P00 golden
transcripts (protocol/fixtures/*.jsonl) against the client.

This is the "client side" of the contract (protocol/README.md section 9): a
fake server plays the server lines of a transcript verbatim and checks every
client line it receives (type, run_id, payload; ids and timestamps are not
compared).  ONE generic driver performs the client's side of any transcript,
so a new golden transcript is covered without new test code:

    run.start        -> link.start_run(**payload)
    user_message     -> link.send_user_message
    tool_result      -> link.send_tool_result, once the tool_proposed it
                        answers has been delivered to the callback
    approval         -> link.send_approval, same wait
    ping             -> link.ping(nonce)
    hello / run.resume are the link's own business and are checked, not driven.

Afterwards the local store must hold exactly the transcript's server stream,
in order, with the server's seq recorded.
"""
from __future__ import annotations

import pytest

from link_fakes import *  # noqa: F401,F403  (autouse fixtures; see link_fakes.py)

FIXTURES = [
    "01_plain_chat.jsonl",
    "02_edit_with_approval.jsonl",
    "03_disconnect_resume.jsonl",
    "04_quota_error.jsonl",
]


async def drive_client(link, rec, srv, group, run_id):
    """Perform the client lines of one connection's transcript."""
    for line in group:
        t, payload = line["t"], line.get("payload", {})
        if t not in CLIENT_TYPES or t in ("hello", "run.resume"):
            continue
        if t == "run.start":
            await link.start_run(run_id=run_id, **payload)
        elif t == "user_message":
            await link.send_user_message(run_id, payload["text"])
        elif t in ("tool_result", "approval"):
            call_id = payload["call_id"]
            await until(lambda: any(e.payload["call_id"] == call_id for e in rec.tool_proposed),
                        what=f"tool_proposed {call_id}")
            if t == "tool_result":
                await link.send_tool_result(
                    run_id, call_id, ok=payload["ok"], output=payload["output"], truncated=payload["truncated"],
                    trust=payload["trust"], exit_code=payload.get("exit_code"), duration_ms=payload.get("duration_ms"))
            else:
                await link.send_approval(run_id, call_id, decision=payload["decision"], scope=payload["scope"])
        elif t == "ping":
            await link.ping(nonce=payload.get("nonce"))
        else:  # pragma: no cover - a new client type needs a driver line
            raise AssertionError(f"driver does not know client message {t}")


def replay(name):
    lines = fixture_lines(name)
    groups = split_connections(lines)
    run_id = next(m["run_id"] for m in lines if "run_id" in m)
    hello = groups[0][0]["payload"]
    store = open_db()
    new_run(store, run_id)

    async def scenario():
        scripts = [fixture_script(g, kill_at_end=(i < len(groups) - 1)) for i, g in enumerate(groups)]
        async with FakeServer(*scripts) as srv:
            rec = Recorder()
            cred = make_credential(srv.server_url, hello["device_credential"])
            link = make_link(store, srv, rec=rec, credential=cred, client_version=hello["client_version"],
                             capabilities=tuple(hello["capabilities"]))
            link.start()
            await link.wait_connected(5)
            for i, group in enumerate(groups):
                if i > 0:
                    # the link itself reconnects, says hello and resumes; wait for it.
                    await until(lambda: len(srv.connections) > i and link.state == "connected", what=f"connection {i}")
                await drive_client(link, rec, srv, group, run_id)
            await until(lambda: len(srv.connections) == len(groups)
                        and all(c.script_done for c in srv.connections), what="the transcript to finish")
            await link.aclose()
            return srv, rec, link

    srv, rec, link = run_async(scenario())
    return lines, groups, run_id, store, srv, rec, link


@pytest.mark.parametrize("name", FIXTURES)
def test_the_client_says_what_the_golden_transcript_says(name):
    lines, groups, run_id, store, srv, rec, link = replay(name)
    srv.assert_clean()                       # every client line matched; every message valid against the schema copy
    assert len(srv.connections) == len(groups)


@pytest.mark.parametrize("name", FIXTURES)
def test_the_store_ends_up_with_exactly_the_servers_stream(name):
    lines, groups, run_id, store, srv, rec, link = replay(name)
    expected = stream_lines(lines)
    stored = server_events(store, run_id)
    assert [e.data["server_seq"] for e in stored] == [m["seq"] for m in expected]
    assert [e.data["id"] for e in stored] == [m["id"] for m in expected]
    assert [e.data["payload"] for e in stored] == [m["payload"] for m in expected]
    # the local log is contiguous and in order, and the callbacks saw the same stream once
    assert [e.seq for e in read_events(store, run_id)] == list(range(1, len(expected) + 1))
    assert rec.seqs(run_id) == [m["seq"] for m in expected]
    assert link.run_info(run_id).last_seq == expected[-1]["seq"]


def test_plain_chat_streams_text_and_the_run_completes():
    lines, groups, run_id, store, srv, rec, link = replay("01_plain_chat.jsonl")
    text = "".join(e.payload["text"] for e in rec.events if e.subtype == "text_delta")
    assert text == "A mutex is a lock that lets only one thread at a time enter a critical section. " \
                   "Other threads that ask for it wait until the owner releases it."
    assert rec.completed(run_id)
    assert link.run_info(run_id).terminal is True


def test_edit_with_approval_delivers_each_proposal_once_with_its_risk():
    lines, groups, run_id, store, srv, rec, link = replay("02_edit_with_approval.jsonl")
    assert [(e.payload["call_id"], e.payload["tool"], e.payload["risk"]) for e in rec.tool_proposed] == [
        ("call_read_1", "read_file", "R0"),
        ("call_edit_1", "edit_file", "R1"),
    ]
    assert all(e.type == "tool_proposed" and e.seq > 0 for e in rec.tool_proposed)
    # the client sent the device's decision and the result, in the order of the transcript
    assert [m["t"] for m in srv.messages(0)] == ["hello", "run.start", "user_message", "tool_result", "approval", "tool_result"]


def test_disconnect_resume_sends_run_resume_with_the_last_seq_and_the_run_id():
    lines, groups, run_id, store, srv, rec, link = replay("03_disconnect_resume.jsonl")
    second = srv.messages(1)
    assert [m["t"] for m in second][:2] == ["hello", "run.resume"]
    assert second[1]["run_id"] == run_id and second[1]["payload"] == {"last_seq": 5}
    assert "seq" not in second[1]
    # the proposal that was cut off (seq 5) is delivered once; the retry (seq 13) is a new one.
    assert [e.payload["call_id"] for e in rec.tool_proposed] == ["call_grep_1", "call_grep_2"]
    # the replay told us the cut call was closed as disconnected
    finished = [e.payload for e in rec.events if e.subtype == "tool_finished"]
    assert [(f["call_id"], f["status"]) for f in finished] == [("call_grep_1", "disconnected"), ("call_grep_2", "ok")]


def test_quota_error_is_reported_not_retried_and_the_pause_is_stored():
    lines, groups, run_id, store, srv, rec, link = replay("04_quota_error.jsonl")
    assert [(e.code, e.run_id) for e in rec.errors] == [("quota_exhausted", run_id)]
    err = rec.errors[0]
    assert err.details == {"reset_at": "2026-12-01T00:00:00Z"} and err.ref_id is not None
    assert [w.payload["spent_pct"] for w in rec.budget] == [80]
    assert rec.state_reached(run_id, "paused_budget")
    assert len(srv.connections) == 1                 # no blind retry, no reconnect
    # errors are not part of the replayable stream, so they are not in the event log
    assert not any(e.type == "server.error" for e in read_events(store, run_id))
