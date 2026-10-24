"""
m2c/tests/test_store_sessions.py -- Patch C02: creating and reading sessions.
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from m2c.store import db, events, sessions
from m2c.store.sessions import Session, SessionExistsError, UnknownSessionError

# The protocol's run_id pattern (protocol/v1/common.json); a session id can be
# reused as a run id, so it must satisfy it.
RUN_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


@pytest.fixture
def conn():
    c = db.open_db()
    yield c
    c.close()


def test_create_session_returns_what_it_stored(conn):
    s = sessions.create_session(conn, project_path="/home/u/proj", mode="ask", model_pref="auto")
    assert isinstance(s, Session)
    assert s.project_path == "/home/u/proj"
    assert s.mode == "ask"
    assert s.model_pref == "auto"
    assert s.created_at.endswith("Z")
    assert sessions.get_session(conn, s.id) == s


def test_generated_ids_look_like_protocol_run_ids_and_differ(conn):
    ids = {sessions.create_session(conn, project_path="/p", mode="ask").id for _ in range(5)}
    assert len(ids) == 5
    assert all(RUN_ID_RE.match(i) for i in ids)


def test_model_pref_is_optional(conn):
    s = sessions.create_session(conn, project_path="/p", mode="plan")
    assert s.model_pref is None
    assert sessions.get_session(conn, s.id).model_pref is None


def test_mode_is_stored_as_given_so_new_modes_need_no_migration(conn):
    for mode in ("plan", "ask", "accept-edits", "auto"):
        s = sessions.create_session(conn, project_path="/p", mode=mode)
        assert sessions.get_session(conn, s.id).mode == mode


def test_caller_chosen_id_and_time_are_used(conn):
    when = datetime(2026, 11, 2, 9, 0, 0, tzinfo=timezone.utc)
    s = sessions.create_session(
        conn, project_path="/p", mode="ask", session_id="sess_fixture_0001", created_at=when
    )
    assert s.id == "sess_fixture_0001"
    assert s.created_at == "2026-11-02T09:00:00.000000Z"


def test_duplicate_id_is_refused_and_leaves_the_original_alone(conn):
    first = sessions.create_session(conn, project_path="/p", mode="ask", session_id="sess_dup_0001")
    with pytest.raises(SessionExistsError):
        sessions.create_session(conn, project_path="/other", mode="plan", session_id="sess_dup_0001")
    assert sessions.get_session(conn, "sess_dup_0001") == first


def test_project_path_must_be_absolute(conn):
    with pytest.raises(ValueError, match="absolute"):
        sessions.create_session(conn, project_path="relative/dir", mode="ask")


def test_project_path_is_normalised_lexically_and_accepts_path_objects(conn):
    s = sessions.create_session(conn, project_path=Path("/home/u/a/../proj/"), mode="ask")
    assert s.project_path == "/home/u/proj"


@pytest.mark.parametrize("bad_id", ["", "short", "has space in it", "bad/slash/in/id", "x" * 65, "naïve-id-0001"])
def test_malformed_session_ids_are_rejected(conn, bad_id):
    with pytest.raises(ValueError):
        sessions.create_session(conn, project_path="/p", mode="ask", session_id=bad_id)


@pytest.mark.parametrize("bad_mode", ["", None, 3])
def test_mode_must_be_a_non_empty_string(conn, bad_mode):
    with pytest.raises(ValueError):
        sessions.create_session(conn, project_path="/p", mode=bad_mode)


@pytest.mark.parametrize("bad_pref", ["", 3])
def test_model_pref_must_be_none_or_a_non_empty_string(conn, bad_pref):
    with pytest.raises(ValueError):
        sessions.create_session(conn, project_path="/p", mode="ask", model_pref=bad_pref)


def test_get_unknown_session_returns_none(conn):
    assert sessions.get_session(conn, "sess_missing_01") is None


def test_require_session_raises_for_an_unknown_id(conn):
    with pytest.raises(UnknownSessionError):
        sessions.require_session(conn, "sess_missing_01")


def test_list_sessions_is_newest_first_with_a_stable_tiebreak(conn):
    t = lambda h: datetime(2026, 11, 2, h, 0, 0, tzinfo=timezone.utc)  # noqa: E731
    a = sessions.create_session(conn, project_path="/p", mode="ask", session_id="sess_aaaa_0001", created_at=t(9))
    b = sessions.create_session(conn, project_path="/p", mode="ask", session_id="sess_bbbb_0001", created_at=t(11))
    c = sessions.create_session(conn, project_path="/p", mode="ask", session_id="sess_cccc_0001", created_at=t(11))
    assert sessions.list_sessions(conn) == [c, b, a]


def test_list_sessions_can_filter_by_project_and_limit(conn):
    a1 = sessions.create_session(conn, project_path="/home/u/a", mode="ask")
    b1 = sessions.create_session(conn, project_path="/home/u/b", mode="ask")
    a2 = sessions.create_session(conn, project_path="/home/u/a", mode="ask")
    got = sessions.list_sessions(conn, project_path="/home/u/a")
    assert {s.id for s in got} == {a1.id, a2.id}
    assert len(sessions.list_sessions(conn, limit=2)) == 2
    assert sessions.list_sessions(conn, project_path="/home/u/b") == [b1]
    assert sessions.list_sessions(conn, project_path="/home/u/none") == []


def test_list_sessions_filter_uses_the_same_normalisation_as_create(conn):
    s = sessions.create_session(conn, project_path="/home/u/proj", mode="ask")
    assert sessions.list_sessions(conn, project_path="/home/u/x/../proj") == [s]


def test_a_session_with_events_cannot_be_deleted(conn):
    s = sessions.create_session(conn, project_path="/p", mode="ask")
    events.append_event(conn, s.id, "hello", {})
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM sessions WHERE id = ?", (s.id,))
    assert sessions.get_session(conn, s.id) == s


def test_session_delete_guard_does_not_depend_on_the_foreign_keys_pragma(conn):
    s = sessions.create_session(conn, project_path="/p", mode="ask")
    events.append_event(conn, s.id, "hello", {})
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError, match="events"):
        conn.execute("DELETE FROM sessions WHERE id = ?", (s.id,))


def test_a_session_without_events_can_be_deleted_by_sql(conn):
    # No API deletes sessions yet (user-visible delete is a later work package);
    # the schema must not stand in the way of an empty one being removed.
    s = sessions.create_session(conn, project_path="/p", mode="ask")
    conn.execute("DELETE FROM sessions WHERE id = ?", (s.id,))
    assert sessions.get_session(conn, s.id) is None


def test_session_id_cannot_be_changed(conn):
    s = sessions.create_session(conn, project_path="/p", mode="ask")
    conn.execute("PRAGMA foreign_keys = OFF")
    with pytest.raises(sqlite3.IntegrityError, match="id"):
        conn.execute("UPDATE sessions SET id = 'sess_renamed_01' WHERE id = ?", (s.id,))
