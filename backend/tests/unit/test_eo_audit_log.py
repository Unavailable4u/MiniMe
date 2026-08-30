"""
tests/unit/test_eo_audit_log.py — Patch 7e-2.

eo/audit_log.py had zero test coverage before this. The one hard
guarantee this module makes -- write_audit() NEVER raises, even on a
DB failure, because "an audit-logging failure must never break the
actual operation it's describing" -- is exactly the kind of contract
that looks trivially true reading the code and then silently breaks
the first time someone "cleans up" the try/except during a refactor.
Pinned here as an explicit regression test, plus the read-side access
posture (list_for_target uses trusted=True; list_for_user scopes by
user_id) since getting those backwards would either lock a legitimate
admin query out or -- worse -- open a cross-user read.

Isolation: audit_log.py does `from eo import db` (a module import, not
individual names) -- db.cursor/db.Json are reached through that same
module object whether patched as `audit_log.db.X` or `eo.db.X`; tests
here patch on `audit_log.db` for locality with the rest of this file.
db.cursor() is a context manager -- FakeCursor/fake_cursor_cm below is
a minimal stand-in that records every execute() call and returns a
canned fetchall() result, without touching a real Postgres connection.
"""
from datetime import UTC, datetime

from eo import audit_log


class FakeCursor:
    def __init__(self, fetchall_result=None, fetchone_result=None):
        self.executed = []
        self._fetchall_result = fetchall_result or []
        self._fetchone_result = fetchone_result

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self._fetchall_result

    def fetchone(self):
        return self._fetchone_result


class FakeCursorContext:
    """Stand-in for db.cursor()'s @contextmanager -- records the
    kwargs it was called with (user_id / trusted) so tests can assert
    which access posture a given function actually used."""

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
        audit_log.db, "cursor",
        lambda **kwargs: FakeCursorContext(cursor, calls_log, **kwargs),
    )
    return calls_log


# ---------------------------------------------------------------------
# write_audit
# ---------------------------------------------------------------------

def test_write_audit_inserts_with_user_scoped_cursor(monkeypatch):
    fake_cursor = FakeCursor()
    calls_log = _install_fake_cursor(monkeypatch, fake_cursor)

    audit_log.write_audit("user_1", "workspace.rename", "workspace", "ws_1",
                           {"old_name": "a", "new_name": "b"})

    assert calls_log == [{"user_id": "user_1"}]
    query, params = fake_cursor.executed[0]
    assert "insert into audit_log" in query
    assert params[0] == "user_1"
    assert params[1] == "workspace.rename"
    assert params[2] == "workspace"
    assert params[3] == "ws_1"
    assert params[4].adapted == {"old_name": "a", "new_name": "b"}


def test_write_audit_defaults_detail_to_empty_dict_when_none(monkeypatch):
    fake_cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, fake_cursor)

    audit_log.write_audit("user_1", "chat.delete", "chat", "chat_1")

    _, params = fake_cursor.executed[0]
    assert params[4].adapted == {}


def test_write_audit_never_raises_when_the_cursor_context_manager_fails(monkeypatch):
    """Regression test for the module's own explicit contract: a DB
    failure at write_audit() must never propagate to the caller."""
    def broken_cursor(**kwargs):
        raise RuntimeError("connection pool exhausted")

    monkeypatch.setattr(audit_log.db, "cursor", broken_cursor)

    audit_log.write_audit("user_1", "workspace.rename", "workspace", "ws_1")  # must not raise


def test_write_audit_never_raises_when_execute_fails(monkeypatch):
    class FailingCursor(FakeCursor):
        def execute(self, query, params=None):
            raise RuntimeError("syntax error in insert")

    _install_fake_cursor(monkeypatch, FailingCursor())

    audit_log.write_audit("user_1", "workspace.rename", "workspace", "ws_1")  # must not raise


# ---------------------------------------------------------------------
# list_for_target
# ---------------------------------------------------------------------

def test_list_for_target_uses_trusted_cursor_not_a_user_scoped_one(monkeypatch):
    """No access check happens in this module (per its own docstring)
    -- it must go in via trusted=True, not user_id, or a real caller-
    scoped RLS policy would silently hide rows an admin needs to see."""
    calls_log = _install_fake_cursor(monkeypatch, FakeCursor(fetchall_result=[]))

    audit_log.list_for_target("workspace", "ws_1")

    assert calls_log == [{"trusted": True}]


def test_list_for_target_filters_by_target_type_and_id_with_limit(monkeypatch):
    fake_cursor = FakeCursor(fetchall_result=[])
    _install_fake_cursor(monkeypatch, fake_cursor)

    audit_log.list_for_target("workspace", "ws_1", limit=50)

    _, params = fake_cursor.executed[0]
    assert params == ("workspace", "ws_1", 50)


def test_list_for_target_maps_rows_through_row_to_entry(monkeypatch):
    created = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
    row = {
        "id": 1, "user_id": "user_1", "action": "workspace.rename",
        "target_type": "workspace", "target_id": "ws_1",
        "detail": {"old_name": "a"}, "created_at": created,
    }
    _install_fake_cursor(monkeypatch, FakeCursor(fetchall_result=[row]))

    result = audit_log.list_for_target("workspace", "ws_1")

    assert result == [{
        "id": 1, "user_id": "user_1", "action": "workspace.rename",
        "target_type": "workspace", "target_id": "ws_1",
        "detail": {"old_name": "a"}, "created_at": created.isoformat(),
    }]


# ---------------------------------------------------------------------
# list_for_user
# ---------------------------------------------------------------------

def test_list_for_user_uses_user_scoped_cursor_not_trusted(monkeypatch):
    calls_log = _install_fake_cursor(monkeypatch, FakeCursor(fetchall_result=[]))

    audit_log.list_for_user("user_1")

    assert calls_log == [{"user_id": "user_1"}]


def test_list_for_user_filters_by_user_id_with_limit(monkeypatch):
    fake_cursor = FakeCursor(fetchall_result=[])
    _install_fake_cursor(monkeypatch, fake_cursor)

    audit_log.list_for_user("user_1", limit=25)

    _, params = fake_cursor.executed[0]
    assert params == ("user_1", 25)


# ---------------------------------------------------------------------
# _row_to_entry
# ---------------------------------------------------------------------

def test_row_to_entry_converts_created_at_to_isoformat():
    created = datetime(2026, 1, 1, tzinfo=UTC)
    row = {
        "id": 5, "user_id": "u", "action": "a", "target_type": "t",
        "target_id": "tid", "detail": {}, "created_at": created,
    }
    assert audit_log._row_to_entry(row)["created_at"] == created.isoformat()


def test_row_to_entry_handles_a_null_created_at_without_raising():
    row = {
        "id": 5, "user_id": "u", "action": "a", "target_type": "t",
        "target_id": "tid", "detail": {}, "created_at": None,
    }
    assert audit_log._row_to_entry(row)["created_at"] is None


# ---------------------------------------------------------------------
# find_deletion_snapshot (Option A: authorize a former owner/partner
# against a deleted workspace's own audit trail)
# ---------------------------------------------------------------------

def test_find_deletion_snapshot_uses_trusted_cursor(monkeypatch):
    """Same posture as list_for_target(): this is an internal
    authorization lookup a route makes on the caller's behalf, not a
    caller-scoped read, so it must go in via trusted=True."""
    calls_log = _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=None))

    audit_log.find_deletion_snapshot("workspace", "ws_1")

    assert calls_log == [{"trusted": True}]


def test_find_deletion_snapshot_queries_the_delete_action_for_this_target(monkeypatch):
    fake_cursor = FakeCursor(fetchone_result=None)
    _install_fake_cursor(monkeypatch, fake_cursor)

    audit_log.find_deletion_snapshot("workspace", "ws_1")

    query, params = fake_cursor.executed[0]
    assert "target_type = %s and target_id = %s and action = %s" in query
    assert params == ("workspace", "ws_1", "workspace.delete")


def test_find_deletion_snapshot_returns_the_detail_dict_when_found(monkeypatch):
    row = {"detail": {"name": "My Project", "authorized_viewer_ids": ["owner_1", "partner_1"]}}
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=row))

    result = audit_log.find_deletion_snapshot("workspace", "ws_1")

    assert result == {"name": "My Project", "authorized_viewer_ids": ["owner_1", "partner_1"]}


def test_find_deletion_snapshot_returns_none_when_never_deleted(monkeypatch):
    _install_fake_cursor(monkeypatch, FakeCursor(fetchone_result=None))

    assert audit_log.find_deletion_snapshot("workspace", "ws_never_deleted") is None
