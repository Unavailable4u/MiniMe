"""
tests/unit/test_eo_memory_batch.py — Patch 7e (content/knowledge group).

eo/memory_batch.py had zero test coverage before this. Per the module's
own Part 8.2 migration notes, this file closed a real permissiveness gap
during its file-to-Postgres migration: member_chat_ids used to be stored
verbatim (including bogus/unowned ids), and only downstream linking
ignored them. That makes the highest-value things to pin down:

  1. _owned_chat_ids()/create_batch()'s filtering -- an id that doesn't
     exist or isn't owned by owner_id must never make it into
     batch_members, silently or otherwise.
  2. _sync_members()'s mutual-linking: every member must end up linked
     to every OTHER member (not itself), which is the entire point of a
     "batch" versus chat_store.set_linked_chats' one-directional linking.
  3. unlink_members()'s "last member standing" dissolution rule (<=1
     remaining member deletes the whole batch, not just the members).
  4. Every read function raising FileNotFoundError -- not returning
     None or someone else's batch -- for a batch_id that doesn't exist
     or isn't owned by the caller, same ownership-scoping discipline
     test_eo_chat_store.py already pins down for eo/chat_store.py.

Isolation: memory_batch.py does `from eo import db` and `from eo import
chat_store` (module imports, not bound names), so tests patch
`memory_batch.db.cursor` and `memory_batch.chat_store.set_linked_chats`/
`chat_exists` directly. The FakeCursor/FakeCursorContext harness below
is copied from test_eo_chat_store.py's own db.cursor() stand-in --
same shape, reused rather than reinvented.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import eo.memory_batch as memory_batch


# ---------------------------------------------------------------------
# Fake db.cursor() harness (same shape as test_eo_chat_store.py's own)
# ---------------------------------------------------------------------

class FakeCursor:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.executed = []
        self._fetchone_queue = list(fetchone_results or [])
        self._fetchall_queue = list(fetchall_results or [])

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        if not self._fetchone_queue:
            return None
        return self._fetchone_queue.pop(0)

    def fetchall(self):
        if not self._fetchall_queue:
            return []
        return self._fetchall_queue.pop(0)


class FakeCursorContext:
    def __init__(self, cursor, calls_log, **kwargs):
        self.cursor = cursor
        self.calls_log = calls_log
        self.kwargs = kwargs

    def __enter__(self):
        self.calls_log.append(self.kwargs)
        return self.cursor

    def __exit__(self, *exc_info):
        return False


def _install_fake_cursor(monkeypatch, cursor, calls_log=None):
    calls_log = calls_log if calls_log is not None else []
    monkeypatch.setattr(
        memory_batch.db, "cursor",
        lambda **kwargs: FakeCursorContext(cursor, calls_log, **kwargs),
    )
    return calls_log


def _batch_row(id="batch_1", name="My Batch", member_chat_ids=None):
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    updated = datetime(2026, 1, 2, tzinfo=timezone.utc)
    return {
        "id": id, "name": name, "created_at": created, "updated_at": updated,
        "member_chat_ids": member_chat_ids or [],
    }


@pytest.fixture(autouse=True)
def _no_real_chat_store_calls(monkeypatch):
    """Every test that doesn't explicitly script chat_store calls still
    shouldn't be able to accidentally hit the real chat_store.py --
    default to permissive no-op stand-ins, overridden per-test where the
    call itself is what's being asserted on."""
    monkeypatch.setattr(memory_batch.chat_store, "set_linked_chats", MagicMock())
    monkeypatch.setattr(memory_batch.chat_store, "chat_exists", MagicMock(return_value=True))


# ---------------------------------------------------------------------
# _row_to_batch
# ---------------------------------------------------------------------

def test_row_to_batch_isoformats_the_timestamps():
    row = _batch_row()
    result = memory_batch._row_to_batch(row)
    assert result["created_at"] == "2026-01-01T00:00:00+00:00"
    assert result["updated_at"] == "2026-01-02T00:00:00+00:00"


def test_row_to_batch_defaults_member_chat_ids_to_empty_list():
    row = _batch_row(member_chat_ids=None)
    result = memory_batch._row_to_batch(row)
    assert result["member_chat_ids"] == []


# ---------------------------------------------------------------------
# _owned_chat_ids
# ---------------------------------------------------------------------

def test_owned_chat_ids_returns_empty_list_for_empty_input(monkeypatch):
    assert memory_batch._owned_chat_ids([], "owner_1") == []


def test_owned_chat_ids_filters_to_only_valid_owned_ids(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[{"id": "chat_1"}, {"id": "chat_2"}]])
    _install_fake_cursor(monkeypatch, cursor)

    result = memory_batch._owned_chat_ids(["chat_1", "chat_2", "chat_bogus"], "owner_1")

    assert result == ["chat_1", "chat_2"]


def test_owned_chat_ids_preserves_input_order(monkeypatch):
    """The DB returns matches in whatever order Postgres feels like
    (chat_b before chat_a here) -- the result must still follow the
    caller's input order, not the query's."""
    cursor = FakeCursor(fetchall_results=[[{"id": "chat_b"}, {"id": "chat_a"}]])
    _install_fake_cursor(monkeypatch, cursor)

    result = memory_batch._owned_chat_ids(["chat_a", "chat_b"], "owner_1")

    assert result == ["chat_a", "chat_b"]


# ---------------------------------------------------------------------
# create_batch
# ---------------------------------------------------------------------

def test_create_batch_raises_when_fewer_than_two_members():
    with pytest.raises(ValueError):
        memory_batch.create_batch("owner_1", "Name", ["chat_1"])


def test_create_batch_filters_out_unowned_members_before_inserting(monkeypatch):
    owned_cursor = FakeCursor(fetchall_results=[[{"id": "chat_1"}, {"id": "chat_2"}]])
    get_cursor = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1", "chat_2"])])

    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        # first call: _owned_chat_ids' lookup; second: the insert
        # transaction; third: get_batch()'s own select
        if calls["n"] == 1:
            return FakeCursorContext(owned_cursor, [], **kwargs)
        elif calls["n"] == 2:
            return FakeCursorContext(FakeCursor(), [], **kwargs)
        return FakeCursorContext(get_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)

    result = memory_batch.create_batch("owner_1", "My Batch", ["chat_1", "chat_2", "chat_bogus"])

    assert result["member_chat_ids"] == ["chat_1", "chat_2"]


def test_create_batch_only_inserts_batch_members_for_valid_ids(monkeypatch):
    owned_cursor = FakeCursor(fetchall_results=[[{"id": "chat_1"}]])
    insert_cursor = FakeCursor()
    get_cursor = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1"])])

    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeCursorContext(owned_cursor, [], **kwargs)
        elif calls["n"] == 2:
            return FakeCursorContext(insert_cursor, [], **kwargs)
        return FakeCursorContext(get_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)

    memory_batch.create_batch("owner_1", "My Batch", ["chat_1", "chat_bogus"])

    batch_member_inserts = [p for q, p in insert_cursor.executed if "batch_members" in q]
    assert batch_member_inserts == [(insert_cursor.executed[1][1][0], "chat_1")]


def test_create_batch_strips_whitespace_and_defaults_empty_name(monkeypatch):
    owned_cursor = FakeCursor(fetchall_results=[[{"id": "chat_1"}, {"id": "chat_2"}]])
    insert_cursor = FakeCursor()
    get_cursor = FakeCursor(fetchone_results=[_batch_row(name="Untitled batch",
                                                          member_chat_ids=["chat_1", "chat_2"])])
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeCursorContext(owned_cursor, [], **kwargs)
        elif calls["n"] == 2:
            return FakeCursorContext(insert_cursor, [], **kwargs)
        return FakeCursorContext(get_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)

    memory_batch.create_batch("owner_1", "   ", ["chat_1", "chat_2"])

    batch_insert = [p for q, p in insert_cursor.executed if q.strip().startswith("insert into batches")][0]
    assert batch_insert[1] == "Untitled batch"


def test_create_batch_syncs_members_mutually(monkeypatch):
    owned_cursor = FakeCursor(fetchall_results=[[{"id": "chat_1"}, {"id": "chat_2"}]])
    insert_cursor = FakeCursor()
    get_cursor = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1", "chat_2"])])
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeCursorContext(owned_cursor, [], **kwargs)
        elif calls["n"] == 2:
            return FakeCursorContext(insert_cursor, [], **kwargs)
        return FakeCursorContext(get_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)
    sync_mock = MagicMock()
    monkeypatch.setattr(memory_batch.chat_store, "set_linked_chats", sync_mock)

    memory_batch.create_batch("owner_1", "My Batch", ["chat_1", "chat_2"])

    sync_mock.assert_any_call("chat_1", "owner_1", ["chat_2"])
    sync_mock.assert_any_call("chat_2", "owner_1", ["chat_1"])


# ---------------------------------------------------------------------
# get_batch / list_batches
# ---------------------------------------------------------------------

def test_get_batch_raises_file_not_found_when_no_row(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        memory_batch.get_batch("batch_missing", "owner_1")


def test_get_batch_returns_the_batch_when_found(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_batch_row(id="batch_1")])
    _install_fake_cursor(monkeypatch, cursor)
    result = memory_batch.get_batch("batch_1", "owner_1")
    assert result["id"] == "batch_1"


def test_get_batch_scopes_the_query_by_owner_id(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_batch_row()])
    _install_fake_cursor(monkeypatch, cursor)

    memory_batch.get_batch("batch_1", "owner_1")

    query, params = cursor.executed[0]
    assert "b.owner_id = %s" in query
    assert params == ("batch_1", "owner_1")


def test_list_batches_returns_every_row_mapped(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[_batch_row(id="batch_1"), _batch_row(id="batch_2")]])
    _install_fake_cursor(monkeypatch, cursor)

    result = memory_batch.list_batches("owner_1")

    assert [b["id"] for b in result] == ["batch_1", "batch_2"]


# ---------------------------------------------------------------------
# rename_batch
# ---------------------------------------------------------------------

def test_rename_batch_raises_file_not_found_when_update_matches_nothing(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        memory_batch.rename_batch("batch_missing", "owner_1", "New Name")


def test_rename_batch_truncates_name_to_80_chars(monkeypatch):
    update_cursor = FakeCursor(fetchone_results=[{"id": "batch_1"}])
    get_cursor = FakeCursor(fetchone_results=[_batch_row(name="x" * 80)])
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        return FakeCursorContext(update_cursor if calls["n"] == 1 else get_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)

    memory_batch.rename_batch("batch_1", "owner_1", "x" * 100)

    query, params = update_cursor.executed[0]
    assert params[0] == "x" * 80


def test_rename_batch_with_blank_name_only_touches_updated_at(monkeypatch):
    update_cursor = FakeCursor(fetchone_results=[{"id": "batch_1"}])
    get_cursor = FakeCursor(fetchone_results=[_batch_row()])
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        return FakeCursorContext(update_cursor if calls["n"] == 1 else get_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)

    memory_batch.rename_batch("batch_1", "owner_1", "   ")

    query, params = update_cursor.executed[0]
    assert "set updated_at" in query
    assert "name = %s" not in query


# ---------------------------------------------------------------------
# unlink_members
# ---------------------------------------------------------------------

def test_unlink_members_dissolves_batch_when_one_or_fewer_members_would_remain(monkeypatch):
    get_cursor = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1", "chat_2"])])
    delete_cursor = FakeCursor()
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        return FakeCursorContext(get_cursor if calls["n"] == 1 else delete_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)
    clear_mock = MagicMock()
    monkeypatch.setattr(memory_batch.chat_store, "set_linked_chats", clear_mock)

    result = memory_batch.unlink_members("batch_1", "owner_1", ["chat_2"])

    assert result is None
    delete_query = [q for q, p in delete_cursor.executed if "delete from batches" in q]
    assert delete_query
    # both original members get cleared, not just the removed one
    clear_mock.assert_any_call("chat_1", "owner_1", [])
    clear_mock.assert_any_call("chat_2", "owner_1", [])


def test_unlink_members_keeps_batch_alive_when_two_or_more_would_remain(monkeypatch):
    get_cursor_1 = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["c1", "c2", "c3"])])
    delete_cursor = FakeCursor()
    get_cursor_2 = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["c1", "c2"])])
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeCursorContext(get_cursor_1, [], **kwargs)
        elif calls["n"] == 2:
            return FakeCursorContext(delete_cursor, [], **kwargs)
        return FakeCursorContext(get_cursor_2, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)
    clear_mock = MagicMock()
    sync_mock = MagicMock()
    monkeypatch.setattr(memory_batch.chat_store, "set_linked_chats",
                         lambda *a, **k: (clear_mock(*a, **k), sync_mock(*a, **k)))

    result = memory_batch.unlink_members("batch_1", "owner_1", ["c3"])

    assert result is not None
    assert result["member_chat_ids"] == ["c1", "c2"]
    delete_member_query = [q for q, p in delete_cursor.executed if "delete from batch_members" in q]
    assert delete_member_query


def test_unlink_members_raises_file_not_found_for_unknown_batch(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        memory_batch.unlink_members("batch_missing", "owner_1", ["chat_1"])


# ---------------------------------------------------------------------
# add_member
# ---------------------------------------------------------------------

def test_add_member_returns_batch_unchanged_when_chat_id_not_owned(monkeypatch):
    get_cursor = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1"])])
    owned_cursor = FakeCursor(fetchall_results=[[]])  # _owned_chat_ids finds nothing
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        return FakeCursorContext(get_cursor if calls["n"] == 1 else owned_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)

    result = memory_batch.add_member("batch_1", "owner_1", "chat_bogus")

    assert result["member_chat_ids"] == ["chat_1"]


def test_add_member_inserts_and_resyncs_when_chat_id_is_owned(monkeypatch):
    get_cursor_1 = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1"])])
    owned_cursor = FakeCursor(fetchall_results=[[{"id": "chat_2"}]])
    insert_cursor = FakeCursor()
    get_cursor_2 = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1", "chat_2"])])
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        seq = [get_cursor_1, owned_cursor, insert_cursor, get_cursor_2]
        return FakeCursorContext(seq[calls["n"] - 1], [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)
    sync_mock = MagicMock()
    monkeypatch.setattr(memory_batch.chat_store, "set_linked_chats", sync_mock)

    result = memory_batch.add_member("batch_1", "owner_1", "chat_2")

    assert result["member_chat_ids"] == ["chat_1", "chat_2"]
    sync_mock.assert_any_call("chat_1", "owner_1", ["chat_2"])
    sync_mock.assert_any_call("chat_2", "owner_1", ["chat_1"])


def test_add_member_raises_file_not_found_for_unknown_batch(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        memory_batch.add_member("batch_missing", "owner_1", "chat_1")


# ---------------------------------------------------------------------
# delete_batch
# ---------------------------------------------------------------------

def test_delete_batch_clears_linked_chats_for_every_member(monkeypatch):
    get_cursor = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1", "chat_2"])])
    delete_cursor = FakeCursor()
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        return FakeCursorContext(get_cursor if calls["n"] == 1 else delete_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)
    clear_mock = MagicMock()
    monkeypatch.setattr(memory_batch.chat_store, "set_linked_chats", clear_mock)

    memory_batch.delete_batch("batch_1", "owner_1")

    clear_mock.assert_any_call("chat_1", "owner_1", [])
    clear_mock.assert_any_call("chat_2", "owner_1", [])


def test_delete_batch_skips_members_whose_chat_no_longer_exists(monkeypatch):
    get_cursor = FakeCursor(fetchone_results=[_batch_row(member_chat_ids=["chat_1", "chat_gone"])])
    delete_cursor = FakeCursor()
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        return FakeCursorContext(get_cursor if calls["n"] == 1 else delete_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)
    clear_mock = MagicMock()
    monkeypatch.setattr(memory_batch.chat_store, "set_linked_chats", clear_mock)
    monkeypatch.setattr(memory_batch.chat_store, "chat_exists",
                         lambda cid, owner_id: cid != "chat_gone")

    memory_batch.delete_batch("batch_1", "owner_1")

    clear_mock.assert_called_once_with("chat_1", "owner_1", [])


def test_delete_batch_raises_file_not_found_for_unknown_batch(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        memory_batch.delete_batch("batch_missing", "owner_1")


# ---------------------------------------------------------------------
# batch_for_chat
# ---------------------------------------------------------------------

def test_batch_for_chat_returns_none_when_chat_is_not_in_any_batch(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    assert memory_batch.batch_for_chat("chat_1", "owner_1") is None


def test_batch_for_chat_returns_the_containing_batch(monkeypatch):
    lookup_cursor = FakeCursor(fetchone_results=[{"batch_id": "batch_1"}])
    get_cursor = FakeCursor(fetchone_results=[_batch_row(id="batch_1")])
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        return FakeCursorContext(lookup_cursor if calls["n"] == 1 else get_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)

    result = memory_batch.batch_for_chat("chat_1", "owner_1")

    assert result["id"] == "batch_1"


def test_batch_for_chat_returns_none_when_the_batch_lookup_returns_a_stale_id(monkeypatch):
    """batch_members points at a batch_id that get_batch() itself can no
    longer find (e.g. deleted between the two queries) -- must degrade
    to None, not raise FileNotFoundError out of this function."""
    lookup_cursor = FakeCursor(fetchone_results=[{"batch_id": "batch_gone"}])
    get_cursor = FakeCursor(fetchone_results=[None])
    calls = {"n": 0}

    def fake_cursor(**kwargs):
        calls["n"] += 1
        return FakeCursorContext(lookup_cursor if calls["n"] == 1 else get_cursor, [], **kwargs)

    monkeypatch.setattr(memory_batch.db, "cursor", fake_cursor)

    assert memory_batch.batch_for_chat("chat_1", "owner_1") is None
