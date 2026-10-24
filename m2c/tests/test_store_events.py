"""
m2c/tests/test_store_events.py -- Patch C02: the append-only event log.

Covers append / read-back, the next-seq helper (including concurrent
writers), the SQLite triggers that make ``events`` append-only for real
(UPDATE, DELETE, and the INSERT OR REPLACE back door), and the JSONL export.
"""
from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from m2c.store import db, events, migrations, sessions
from m2c.store.events import Event
from m2c.store.sessions import UnknownSessionError


@pytest.fixture
def conn():
    c = db.open_db()
    yield c
    c.close()


@pytest.fixture
def sid(conn) -> str:
    return sessions.create_session(conn, project_path="/home/u/proj", mode="ask").id


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --------------------------------------------------------------------------
# Append and read back
# --------------------------------------------------------------------------
def test_append_assigns_seq_starting_at_one(conn, sid):
    first = events.append_event(conn, sid, "run.started", {"mode": "ask"})
    second = events.append_event(conn, sid, "text_delta", {"text": "hi"})
    assert (first.seq, second.seq) == (1, 2)
    assert isinstance(first, Event)
    assert first.session_id == sid
    assert first.type == "run.started"
    assert first.data == {"mode": "ask"}
    assert first.ts.endswith("Z")


def test_read_back_returns_what_was_appended_in_order(conn, sid):
    payloads = [{"i": i, "text": f"line {i}"} for i in range(10)]
    for p in payloads:
        events.append_event(conn, sid, "text_delta", p)
    got = events.read_events(conn, sid)
    assert [e.seq for e in got] == list(range(1, 11))
    assert [e.data for e in got] == payloads


def test_appended_event_equals_the_one_read_back(conn, sid):
    written = events.append_event(conn, sid, "tool_result", {"ok": True, "n": [1, 2, 3]})
    assert events.read_events(conn, sid) == [written]


def test_seq_is_per_session(conn, sid):
    other = sessions.create_session(conn, project_path="/home/u/other", mode="ask").id
    events.append_event(conn, sid, "a", {})
    events.append_event(conn, sid, "a", {})
    assert events.append_event(conn, other, "a", {}).seq == 1
    assert events.last_seq(conn, sid) == 2
    assert events.last_seq(conn, other) == 1


def test_last_seq_is_zero_for_an_empty_or_unknown_session(conn, sid):
    assert events.last_seq(conn, sid) == 0
    assert events.last_seq(conn, "sess_unknown_01") == 0


def test_read_events_can_resume_after_a_seq_and_limit(conn, sid):
    for i in range(10):
        events.append_event(conn, sid, "t", {"i": i})
    assert [e.seq for e in events.read_events(conn, sid, after_seq=7)] == [8, 9, 10]
    assert [e.seq for e in events.read_events(conn, sid, after_seq=2, limit=3)] == [3, 4, 5]
    assert events.read_events(conn, sid, after_seq=10) == []


def test_read_events_for_an_unknown_session_is_empty(conn):
    assert events.read_events(conn, "sess_unknown_01") == []


def test_iter_events_streams_the_same_rows(conn, sid):
    for i in range(5):
        events.append_event(conn, sid, "t", {"i": i})
    assert list(events.iter_events(conn, sid)) == events.read_events(conn, sid)


def test_payload_is_stored_canonically(conn, sid):
    events.append_event(conn, sid, "t", {"b": 1, "a": {"z": 1, "y": 2}})
    (text,) = conn.execute("SELECT json FROM events").fetchone()
    assert text == '{"a":{"y":2,"z":1},"b":1}'


def test_non_ascii_and_awkward_characters_round_trip(conn, sid):
    payload = {"text": "\u09ac\u09be\u0982\u09b2\u09be \u65e5\u672c\u8a9e \u2028 \u2029 \x00 \U0001F600 line\nbreak \ud800"}
    events.append_event(conn, sid, "t", payload)
    assert events.read_events(conn, sid)[0].data == payload
    (text,) = conn.execute("SELECT json FROM events").fetchone()
    assert text.isascii()  # stored escaped: no encoding error is possible, ever


def test_any_json_value_is_accepted_as_payload(conn, sid):
    for value in (None, 3, "text", [1, 2], {"k": None}, True):
        events.append_event(conn, sid, "t", value)
    assert [e.data for e in events.read_events(conn, sid)] == [None, 3, "text", [1, 2], {"k": None}, True]


def test_caller_supplied_timestamp_is_kept(conn, sid):
    e = events.append_event(conn, sid, "t", {}, ts="2026-11-02T09:00:00.250Z")
    assert e.ts == "2026-11-02T09:00:00.250Z"
    when = datetime(2026, 11, 2, 9, 0, 1, tzinfo=timezone.utc)
    assert events.append_event(conn, sid, "t", {}, ts=when).ts == "2026-11-02T09:00:01.000000Z"


@pytest.mark.parametrize("bad", [object(), {1, 2}, float("nan"), float("inf"), b"bytes"])
def test_unserialisable_payload_is_rejected_before_anything_is_written(conn, sid, bad):
    with pytest.raises((TypeError, ValueError)):
        events.append_event(conn, sid, "t", {"x": bad})
    assert events.last_seq(conn, sid) == 0
    assert not conn.in_transaction


@pytest.mark.parametrize("bad_type", ["", None, 5, "x" * 129])
def test_event_type_must_be_a_short_non_empty_string(conn, sid, bad_type):
    with pytest.raises(ValueError):
        events.append_event(conn, sid, bad_type, {})
    assert events.last_seq(conn, sid) == 0


def test_appending_to_an_unknown_session_fails_cleanly(conn):
    with pytest.raises(UnknownSessionError):
        events.append_event(conn, "sess_unknown_01", "t", {})
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0


def test_raw_insert_for_an_unknown_session_hits_the_foreign_key(conn):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute("INSERT INTO events VALUES ('nope', 1, ?, 't', '{}')", (db.utc_now_iso(),))


# --------------------------------------------------------------------------
# Batches and transactions: no gaps, no half writes
# --------------------------------------------------------------------------
def test_append_events_writes_a_batch_in_one_transaction(conn, sid):
    got = events.append_events(conn, sid, [("a", {"n": 1}), ("b", {"n": 2}), ("c", {"n": 3})])
    assert [(e.seq, e.type) for e in got] == [(1, "a"), (2, "b"), (3, "c")]
    assert events.read_events(conn, sid) == got


def test_append_events_with_an_empty_batch_is_a_no_op(conn, sid):
    assert events.append_events(conn, sid, []) == []
    assert events.last_seq(conn, sid) == 0


def test_a_bad_item_in_a_batch_writes_nothing(conn, sid):
    events.append_event(conn, sid, "first", {})
    with pytest.raises(TypeError):
        events.append_events(conn, sid, [("ok", {}), ("bad", {"x": object()})])
    assert [e.type for e in events.read_events(conn, sid)] == ["first"]


def test_rolled_back_append_leaves_no_gap_in_seq(conn, sid):
    events.append_event(conn, sid, "a", {})
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            events.append_event(conn, sid, "b", {})  # joins the outer transaction
            events.append_event(conn, sid, "c", {})
            raise RuntimeError("caller gave up")
    assert events.append_event(conn, sid, "d", {}).seq == 2
    assert [e.type for e in events.read_events(conn, sid)] == ["a", "d"]


def test_append_inside_a_caller_transaction_commits_with_it(conn, sid):
    with db.transaction(conn):
        events.append_event(conn, sid, "a", {})
        events.append_event(conn, sid, "b", {})
    assert [e.seq for e in events.read_events(conn, sid)] == [1, 2]


def test_concurrent_writers_get_unique_contiguous_seqs(tmp_path):
    path = tmp_path / "concurrent.db"
    setup = db.connect(path)
    migrations.migrate(setup)
    session = sessions.create_session(setup, project_path="/p", mode="ask")
    workers, per_worker = 4, 25
    errors: list[BaseException] = []
    barrier = threading.Barrier(workers)

    def work(n: int) -> None:
        c = db.connect(path)
        try:
            barrier.wait()
            for i in range(per_worker):
                events.append_event(c, session.id, "tick", {"w": n, "i": i})
        except BaseException as exc:  # noqa: BLE001 -- collected and asserted below
            errors.append(exc)
        finally:
            c.close()

    threads = [threading.Thread(target=work, args=(n,)) for n in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert errors == []
        got = events.read_events(setup, session.id)
        assert [e.seq for e in got] == list(range(1, workers * per_worker + 1))
        for n in range(workers):  # each writer's own order is preserved
            mine = [e.data["i"] for e in got if e.data["w"] == n]
            assert mine == list(range(per_worker))
    finally:
        setup.close()


# --------------------------------------------------------------------------
# events is append-only for real
# --------------------------------------------------------------------------
def test_the_triggers_exist_in_the_schema(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    assert {"events_no_update", "events_no_delete", "events_no_overwrite"} <= names


@pytest.fixture
def logged(conn, sid):
    events.append_event(conn, sid, "first", {"v": 1})
    events.append_event(conn, sid, "second", {"v": 2})
    return conn, sid, events.read_events(conn, sid)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE events SET json = '{}'",
        "UPDATE events SET type = 'forged' WHERE seq = 1",
        "UPDATE events SET seq = seq + 10",
        "UPDATE events SET ts = '1999-01-01T00:00:00Z'",
        "UPDATE events SET session_id = session_id",  # even a no-op update is refused
        "UPDATE OR REPLACE events SET json = '{}' WHERE seq = 2",
        "UPDATE OR IGNORE events SET json = '{}'",
    ],
)
def test_update_is_aborted_by_a_trigger(logged, sql):
    conn, sid, before = logged
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    assert events.read_events(conn, sid) == before


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM events",
        "DELETE FROM events WHERE seq = 1",
        "DELETE FROM events WHERE session_id = (SELECT id FROM sessions LIMIT 1)",
    ],
)
def test_delete_is_aborted_by_a_trigger(logged, sql):
    conn, sid, before = logged
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(sql)
    assert events.read_events(conn, sid) == before


@pytest.mark.parametrize("recursive", ["ON", "OFF"])
@pytest.mark.parametrize("verb", ["INSERT OR REPLACE", "REPLACE"])
def test_insert_or_replace_cannot_overwrite_an_event(logged, recursive, verb):
    """REPLACE deletes the old row *without* firing DELETE triggers unless
    recursive_triggers is on, so a DELETE trigger alone would leave this back
    door open.  The BEFORE INSERT trigger closes it either way."""
    conn, sid, before = logged
    conn.execute(f"PRAGMA recursive_triggers = {recursive}")
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        conn.execute(f"{verb} INTO events VALUES (?, 1, '2000-01-01T00:00:00Z', 'forged', '{{}}')", (sid,))
    assert events.read_events(conn, sid) == before


def test_plain_duplicate_insert_is_refused(logged):
    conn, sid, before = logged
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO events VALUES (?, 2, '2000-01-01T00:00:00Z', 'dup', '{}')", (sid,))
    assert events.read_events(conn, sid) == before


def test_a_rolled_back_failed_update_inside_a_transaction_leaves_the_log_intact(logged):
    conn, sid, before = logged
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(conn):
            events.append_event(conn, sid, "third", {})
            conn.execute("UPDATE events SET json = '{}'")
    assert events.read_events(conn, sid) == before


def test_triggers_are_part_of_the_file_so_they_survive_reopening(tmp_path):
    first = db.open_db()
    s = sessions.create_session(first, project_path="/p", mode="ask")
    events.append_event(first, s.id, "kept", {"v": 1})
    first.close()
    second = db.open_db()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            second.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            second.execute("UPDATE events SET type = 'x'")
        assert [e.type for e in events.read_events(second, s.id)] == ["kept"]
    finally:
        second.close()


def test_the_triggers_fire_from_a_plain_sqlite_connection_too(tmp_path):
    """They live in the database, not in this package: a connection that
    knows nothing about m2c is stopped as well."""
    first = db.open_db()
    s = sessions.create_session(first, project_path="/p", mode="ask")
    events.append_event(first, s.id, "kept", {})
    first.close()
    plain = sqlite3.connect(str(db.db_path()))  # no pragmas, no helper
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            plain.execute("DELETE FROM events")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            plain.execute("INSERT OR REPLACE INTO events VALUES (?, 1, 'x', 'forged', '{}')", (s.id,))
    finally:
        plain.close()


# --------------------------------------------------------------------------
# Export to JSONL
# --------------------------------------------------------------------------
def test_export_writes_one_json_object_per_event_in_seq_order(conn, sid, tmp_path):
    for i in range(5):
        events.append_event(conn, sid, "t", {"i": i})
    out = tmp_path / "out.jsonl"
    assert events.export_jsonl(conn, out, session_id=sid) == 5
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    rows = [json.loads(line) for line in lines]
    assert [r["seq"] for r in rows] == [1, 2, 3, 4, 5]
    assert all(r["session_id"] == sid and r["type"] == "t" for r in rows)
    assert [r["json"] for r in rows] == [{"i": i} for i in range(5)]
    assert out.read_text(encoding="utf-8").endswith("\n")


def test_export_lines_have_a_fixed_key_order(conn, sid):
    events.append_event(conn, sid, "t", {"b": 1, "a": 2}, ts="2026-11-02T09:00:00.250Z")
    (line,) = list(events.iter_export_lines(conn, sid))
    assert line == (
        f'{{"session_id":"{sid}","seq":1,"ts":"2026-11-02T09:00:00.250Z",'
        '"type":"t","json":{"a":2,"b":1}}'
    )


def test_export_is_deterministic(conn, sid, tmp_path):
    for i in range(20):
        events.append_event(conn, sid, "t", {"i": i, "s": "x" * i})
    a, b = tmp_path / "a.jsonl", tmp_path / "b.jsonl"
    events.export_jsonl(conn, a, session_id=sid)
    events.export_jsonl(conn, b, session_id=sid)
    assert a.read_bytes() == b.read_bytes()


def test_export_survives_a_database_round_trip_byte_for_byte(tmp_path):
    first = db.open_db()
    s = sessions.create_session(first, project_path="/p", mode="ask")
    for i in range(10):
        events.append_event(first, s.id, "t", {"i": i}, ts=f"2026-11-02T09:00:{i:02d}.000Z")
    a = tmp_path / "a.jsonl"
    events.export_jsonl(first, a, session_id=s.id)
    first.close()
    second = db.open_db()
    try:
        b = tmp_path / "b.jsonl"
        events.export_jsonl(second, b, session_id=s.id)
    finally:
        second.close()
    assert a.read_bytes() == b.read_bytes()


def test_export_keeps_every_event_on_one_physical_line(conn, sid, tmp_path):
    events.append_event(conn, sid, "t", {"text": "a\nb\r\nc \u2028 d \u2029 e \x0b \x0c \x85"})
    out = tmp_path / "o.jsonl"
    events.export_jsonl(conn, out, session_id=sid)
    raw = out.read_bytes()
    assert raw.count(b"\n") == 1  # only the terminator
    assert raw.isascii()
    assert json.loads(raw)["json"]["text"].startswith("a\nb")


def test_export_of_one_session_excludes_the_others(conn, sid, tmp_path):
    other = sessions.create_session(conn, project_path="/p2", mode="ask").id
    events.append_event(conn, sid, "mine", {})
    events.append_event(conn, other, "theirs", {})
    out = tmp_path / "o.jsonl"
    events.export_jsonl(conn, out, session_id=sid)
    assert [json.loads(l)["type"] for l in out.read_text().splitlines()] == ["mine"]


def test_export_of_everything_orders_by_session_then_seq(conn, tmp_path):
    t = lambda h: datetime(2026, 11, 2, h, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    late = sessions.create_session(conn, project_path="/p", mode="ask", session_id="sess_late_0001", created_at=t(12))
    early = sessions.create_session(conn, project_path="/p", mode="ask", session_id="sess_early_001", created_at=t(8))
    events.append_event(conn, late.id, "l1", {})
    events.append_event(conn, early.id, "e1", {})
    events.append_event(conn, late.id, "l2", {})
    events.append_event(conn, early.id, "e2", {})
    out = tmp_path / "all.jsonl"
    assert events.export_jsonl(conn, out) == 4
    got = [(json.loads(l)["session_id"], json.loads(l)["seq"]) for l in out.read_text().splitlines()]
    assert got == [(early.id, 1), (early.id, 2), (late.id, 1), (late.id, 2)]


def test_export_of_an_empty_session_is_an_empty_file(conn, sid, tmp_path):
    out = tmp_path / "empty.jsonl"
    assert events.export_jsonl(conn, out, session_id=sid) == 0
    assert out.read_bytes() == b""


def test_export_of_an_unknown_session_raises_and_writes_nothing(conn, tmp_path):
    out = tmp_path / "none.jsonl"
    with pytest.raises(UnknownSessionError):
        events.export_jsonl(conn, out, session_id="sess_unknown_01")
    assert not out.exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".none")] == []


def test_export_to_a_text_stream(conn, sid):
    events.append_event(conn, sid, "t", {"i": 1})
    buf = io.StringIO()
    assert events.export_jsonl(conn, buf, session_id=sid) == 1
    assert json.loads(buf.getvalue())["json"] == {"i": 1}


@pytest.mark.parametrize("umask", [0o000, 0o022])
def test_export_file_is_0600_whatever_the_umask(conn, sid, tmp_path, umask):
    events.append_event(conn, sid, "t", {"secret": "tool output may hold anything"})
    out = tmp_path / "o.jsonl"
    old = os.umask(umask)
    try:
        events.export_jsonl(conn, out, session_id=sid)
    finally:
        os.umask(old)
    assert _mode(out) == 0o600


def test_export_replaces_an_existing_file_and_tightens_its_mode(conn, sid, tmp_path):
    events.append_event(conn, sid, "t", {})
    out = tmp_path / "o.jsonl"
    out.write_text("old contents\n")
    os.chmod(out, 0o644)
    events.export_jsonl(conn, out, session_id=sid)
    assert _mode(out) == 0o600
    assert "old contents" not in out.read_text()


def test_export_never_writes_through_a_symlink(conn, sid, tmp_path):
    events.append_event(conn, sid, "t", {})
    target = tmp_path / "precious.txt"
    target.write_text("do not touch")
    link = tmp_path / "o.jsonl"
    link.symlink_to(target)
    events.export_jsonl(conn, link, session_id=sid)
    assert target.read_text() == "do not touch"
    assert not link.is_symlink() and link.read_text().startswith("{")


def test_failed_export_leaves_no_temp_file_and_keeps_the_old_one(conn, sid, tmp_path, monkeypatch):
    events.append_event(conn, sid, "t", {})
    outdir = tmp_path / "exports"
    outdir.mkdir()
    out = outdir / "o.jsonl"
    out.write_text("previous export\n")

    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk on fire"):
        events.export_jsonl(conn, out, session_id=sid)
    assert out.read_text() == "previous export\n"
    assert sorted(p.name for p in outdir.iterdir()) == ["o.jsonl"]


def test_export_reports_a_corrupt_row_instead_of_writing_invalid_json(tmp_path):
    path = tmp_path / "corrupt.db"
    c = db.connect(path)
    migrations.migrate(c)
    s = sessions.create_session(c, project_path="/p", mode="ask")
    # A forged row from a connection that bypassed the helper:
    c.execute("INSERT INTO events VALUES (?, 1, ?, 't', '{not json')", (s.id, db.utc_now_iso()))
    out = tmp_path / "o.jsonl"
    try:
        with pytest.raises(db.StoreError, match="seq 1"):
            events.export_jsonl(c, out, session_id=s.id)
        assert not out.exists()
    finally:
        c.close()
