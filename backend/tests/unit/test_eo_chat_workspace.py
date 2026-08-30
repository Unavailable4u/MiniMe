"""
tests/unit/test_eo_chat_workspace.py — Patch 7e-S3 (Structural group).

eo/chat_workspace.py had zero test coverage before this. It's the
"Projects" container module and, per its own docstring, is where the
whole role hierarchy (viewer < editor < moderator < partner <= owner),
the owner-transition rules (forced removal / voluntary leave / joint
state / majority voting), and attribution visibility all live. Given
the size of the module (1198 lines), this file prioritizes, in order:

  1. Access-tier gating: member_role()'s resolution and every
     _require_*() gate built on top of it -- getting a rank check
     backwards either locks out someone who should have access or
     (worse) lets a lower tier do something only a higher tier should.
  2. Owner-transition correctness: remove_owner() (forced, partner-only,
     no successor choice), leave_workspace() (voluntary, successor must
     be a current partner, joint state on no successor), and cast_vote()
     (strict-majority-of-total-partners tally, joint-only, one vote per
     partner, election clears the ballot and removes the winner's
     membership row).
  3. The promote()/active_stages_precheck()/auto_partial_promote()/
     chat_triggered_partial_promote() stage-tab family, since the
     "note" exemption from forward-only ordering and the
     complete-vs-partial branching are easy to get subtly wrong.
  4. Attribution visibility (set_show_attribution / can_see_attribution)
     and the remaining CRUD/membership functions.

Isolation follows the same convention as test_eo_chat_store.py:
chat_workspace.py does `from eo import db`, so db.cursor() is reached
through that module object; FakeCursor/FakeCursorContext below queue
canned fetchone()/fetchall() results in call order and record every
execute() (query, params) pair without touching a real Postgres
connection. A single FakeCursor instance is shared across every
db.cursor() call within one test (chat_workspace functions commonly
open several cursors in sequence -- e.g. get_workspace() calling
member_role() first -- and the queue is consumed in the order those
calls actually happen), same pattern chat_store's own tests use.

write_audit (bound into chat_workspace's own namespace via `from
eo.audit_log import write_audit`) is patched as
`chat_workspace.write_audit` for the same reason chat_store's tests
patch it on chat_store: patching eo.audit_log.write_audit would not
reach the name chat_workspace already resolved at import time. The
deferred `from eo.notify import notify` inside
chat_triggered_partial_promote() is patched at its source,
`eo.notify.notify`, since that import happens fresh on every call.
"""
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from eo import chat_workspace

# ---------------------------------------------------------------------
# Fake db.cursor() harness (same shape as test_eo_chat_store.py's)
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
        chat_workspace.db, "cursor",
        lambda **kwargs: FakeCursorContext(cursor, calls_log, **kwargs),
    )
    return calls_log


def _now():
    return datetime(2026, 1, 1, tzinfo=UTC)


def _ws_row(id="ws_1", name="My Project", owner_id="owner_1",
            show_attribution=True, stage="note", active_stages=None,
            stage_history=None, chat_ids=None):
    return {
        "id": id, "name": name, "owner_id": owner_id,
        "show_attribution": show_attribution, "stage": stage,
        "active_stages": active_stages if active_stages is not None else [stage],
        "stage_history": stage_history or [],
        "chat_ids": chat_ids or [],
        "created_at": _now(), "updated_at": _now(),
    }


@pytest.fixture(autouse=True)
def _no_real_audit(monkeypatch):
    monkeypatch.setattr(chat_workspace, "write_audit", MagicMock())


# ---------------------------------------------------------------------
# _next_stage
# ---------------------------------------------------------------------

def test_next_stage_returns_immediate_successor():
    assert chat_workspace._next_stage("research") == "plan"


def test_next_stage_returns_none_at_final_stage():
    assert chat_workspace._next_stage("growth") is None


def test_next_stage_returns_none_for_unknown_stage():
    assert chat_workspace._next_stage("bogus") is None


# ---------------------------------------------------------------------
# promote()
# ---------------------------------------------------------------------

def test_promote_rejects_unknown_mode(monkeypatch):
    with pytest.raises(ValueError):
        chat_workspace.promote("ws_1", "owner_1", mode="turbo")


def test_promote_requires_edit_access(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access",
                         MagicMock(side_effect=chat_workspace.WorkspaceAccessError("nope")))
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.promote("ws_1", "viewer_1")


def test_promote_raises_when_workspace_missing(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.promote("ws_missing", "owner_1")


def test_promote_raises_when_already_at_final_stage(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[
        {"stage": "growth", "stage_history": [], "active_stages": ["growth"]}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.promote("ws_1", "owner_1")


def test_promote_defaults_to_immediate_successor_in_complete_mode(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "owner")
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         lambda *a, **kw: _ws_row(stage="plan", active_stages=["plan"]))
    cursor = FakeCursor(fetchone_results=[
        {"stage": "research", "stage_history": [], "active_stages": ["research"]},
    ])
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_workspace.promote("ws_1", "owner_1")

    update_query, update_params = cursor.executed[1]
    assert "update workspaces" in update_query
    assert update_params[0] == "plan"          # new stage
    assert update_params[1] == '["plan"]'      # new active_stages, complete mode replaces
    assert result["stage"] == "plan"


def test_promote_rejects_unknown_explicit_target_stage(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[
        {"stage": "research", "stage_history": [], "active_stages": ["research"]}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.promote("ws_1", "owner_1", to_stage="bogus")


def test_promote_partial_mode_rejects_backward_or_same_target(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[
        {"stage": "plan", "stage_history": [], "active_stages": ["plan"]}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.promote("ws_1", "owner_1", to_stage="research", mode="partial")


def test_promote_complete_mode_allows_backward_target(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "owner")
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         lambda *a, **kw: _ws_row(stage="research", active_stages=["research"]))
    cursor = FakeCursor(fetchone_results=[
        {"stage": "plan", "stage_history": [], "active_stages": ["plan"]},
    ])
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_workspace.promote("ws_1", "owner_1", to_stage="research", mode="complete")
    assert result["stage"] == "research"


def test_promote_rejects_target_already_active(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[
        {"stage": "plan", "stage_history": [], "active_stages": ["plan", "build"]}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.promote("ws_1", "owner_1", to_stage="build", mode="partial")


def test_promote_partial_mode_appends_active_stage_and_keeps_primary(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "owner")
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         lambda *a, **kw: _ws_row(stage="plan", active_stages=["plan", "build"]))
    cursor = FakeCursor(fetchone_results=[
        {"stage": "plan", "stage_history": [], "active_stages": ["plan"]},
    ])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.promote("ws_1", "owner_1", to_stage="build", mode="partial")

    update_query, update_params = cursor.executed[1]
    assert update_params[0] == "plan"  # primary stage unchanged in partial mode
    assert update_params[1] == '["plan", "build"]'


# ---------------------------------------------------------------------
# active_stages_precheck()
# ---------------------------------------------------------------------

def test_active_stages_precheck_workspace_not_found(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.active_stages_precheck("ws_missing", "plan")
    assert result["eligible"] is False
    assert "not found" in result["reason"]


def test_active_stages_precheck_unknown_stage(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"stage": "plan", "active_stages": ["plan"]}])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.active_stages_precheck("ws_1", "bogus")
    assert result["eligible"] is False
    assert "unknown workspace stage" in result["reason"]


def test_active_stages_precheck_already_active(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"stage": "plan", "active_stages": ["plan", "build"]}])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.active_stages_precheck("ws_1", "build")
    assert result["eligible"] is False
    assert "already active" in result["reason"]


def test_active_stages_precheck_backward_target_ineligible(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"stage": "build", "active_stages": ["build"]}])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.active_stages_precheck("ws_1", "plan")
    assert result["eligible"] is False
    assert "forward-only" in result["reason"]


def test_active_stages_precheck_note_is_exempt_from_forward_only(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"stage": "build", "active_stages": ["build"]}])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.active_stages_precheck("ws_1", "note")
    assert result["eligible"] is True


def test_active_stages_precheck_eligible_forward_target(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"stage": "plan", "active_stages": ["plan"]}])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.active_stages_precheck("ws_1", "build")
    assert result["eligible"] is True
    assert result["reason"] is None


def test_active_stages_precheck_uses_trusted_cursor(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"stage": "plan", "active_stages": ["plan"]}])
    calls = _install_fake_cursor(monkeypatch, cursor)
    chat_workspace.active_stages_precheck("ws_1", "build")
    assert calls[0] == {"trusted": True}


# ---------------------------------------------------------------------
# auto_partial_promote()
# ---------------------------------------------------------------------

def test_auto_partial_promote_returns_none_when_precheck_ineligible(monkeypatch):
    monkeypatch.setattr(chat_workspace, "active_stages_precheck",
                         lambda *a, **kw: {"eligible": False, "reason": "nope"})
    assert chat_workspace.auto_partial_promote("ws_1", "build") is None


def test_auto_partial_promote_returns_none_when_workspace_vanishes_before_update(monkeypatch):
    monkeypatch.setattr(chat_workspace, "active_stages_precheck",
                         lambda *a, **kw: {"eligible": True, "reason": None})
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.auto_partial_promote("ws_1", "build") is None


def test_auto_partial_promote_returns_none_on_race_already_active(monkeypatch):
    monkeypatch.setattr(chat_workspace, "active_stages_precheck",
                         lambda *a, **kw: {"eligible": True, "reason": None})
    cursor = FakeCursor(fetchone_results=[
        {"stage": "plan", "stage_history": [], "active_stages": ["plan", "build"]}])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.auto_partial_promote("ws_1", "build") is None


def test_auto_partial_promote_success_attributes_to_system_actor(monkeypatch):
    monkeypatch.setattr(chat_workspace, "active_stages_precheck",
                         lambda *a, **kw: {"eligible": True, "reason": None})
    cursor = FakeCursor(fetchone_results=[
        {"stage": "plan", "stage_history": [], "active_stages": ["plan"]}])
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_workspace.auto_partial_promote("ws_1", "build")

    assert result["active_stages"] == ["plan", "build"]
    assert result["stage_history"][-1]["by"] == chat_workspace._AUTO_PROMOTE_ACTOR
    assert result["stage_history"][-1]["mode"] == "partial"
    chat_workspace.write_audit.assert_called_once()
    args = chat_workspace.write_audit.call_args[0]
    assert args[0] == chat_workspace._AUTO_PROMOTE_ACTOR


# ---------------------------------------------------------------------
# chat_triggered_partial_promote()
# ---------------------------------------------------------------------

def test_chat_triggered_partial_promote_returns_none_when_ineligible(monkeypatch):
    monkeypatch.setattr(chat_workspace, "active_stages_precheck",
                         lambda *a, **kw: {"eligible": False, "reason": "nope"})
    promote_mock = MagicMock()
    monkeypatch.setattr(chat_workspace, "promote", promote_mock)
    assert chat_workspace.chat_triggered_partial_promote("ws_1", "user_1", "build") is None
    promote_mock.assert_not_called()


def test_chat_triggered_partial_promote_calls_promote_and_notifies(monkeypatch):
    monkeypatch.setattr(chat_workspace, "active_stages_precheck",
                         lambda *a, **kw: {"eligible": True, "reason": None})
    promoted = {"id": "ws_1", "active_stages": ["plan", "build"]}
    promote_mock = MagicMock(return_value=promoted)
    monkeypatch.setattr(chat_workspace, "promote", promote_mock)
    notify_mock = MagicMock()
    monkeypatch.setattr("eo.notify.notify", notify_mock)

    result = chat_workspace.chat_triggered_partial_promote(
        "ws_1", "user_1", "build", session_id="sess_1")

    promote_mock.assert_called_once_with("ws_1", "user_1", to_stage="build", mode="partial")
    notify_mock.assert_called_once()
    call_args = notify_mock.call_args[0]
    assert call_args[0] == "sess_1"
    assert call_args[1] == "workspace_promoted"
    assert call_args[2]["workspace_id"] == "ws_1"
    assert result == promoted


def test_chat_triggered_partial_promote_propagates_promote_errors(monkeypatch):
    monkeypatch.setattr(chat_workspace, "active_stages_precheck",
                         lambda *a, **kw: {"eligible": True, "reason": None})
    monkeypatch.setattr(chat_workspace, "promote",
                         MagicMock(side_effect=chat_workspace.WorkspaceAccessError("nope")))
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.chat_triggered_partial_promote("ws_1", "viewer_1", "build")


# ---------------------------------------------------------------------
# member_role() / _require_*() access gates
# ---------------------------------------------------------------------

def test_member_role_returns_none_when_workspace_missing(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.member_role("ws_missing", "user_1") is None


def test_member_role_returns_owner_when_user_is_owner(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"owner_id": "owner_1"}])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.member_role("ws_1", "owner_1") == "owner"


def test_member_role_returns_member_row_role_when_not_owner(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"owner_id": "owner_1"}, {"role": "editor"}])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.member_role("ws_1", "someone_else") == "editor"


def test_member_role_returns_none_when_no_membership_row(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"owner_id": "owner_1"}, None])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.member_role("ws_1", "stranger") is None


def test_member_role_joint_state_owner_id_none_does_not_match_anyone(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"owner_id": None}, {"role": "partner"}])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.member_role("ws_1", "partner_1") == "partner"


@pytest.mark.parametrize("role", ["viewer", "editor", "moderator", "partner", "owner"])
def test_rank_known_roles(role):
    assert chat_workspace._rank(role) >= 0


def test_rank_unknown_role_is_minus_one():
    assert chat_workspace._rank(None) == -1
    assert chat_workspace._rank("bogus") == -1


def test_require_access_raises_file_not_found_when_no_role(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: None)
    with pytest.raises(FileNotFoundError):
        chat_workspace._require_access("ws_1", "stranger")


def test_require_access_returns_role_when_present(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "viewer")
    assert chat_workspace._require_access("ws_1", "user_1") == "viewer"


def test_require_edit_access_rejects_viewer(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "viewer")
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace._require_edit_access("ws_1", "user_1")


def test_require_edit_access_allows_editor_and_above(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "editor")
    assert chat_workspace._require_edit_access("ws_1", "user_1") == "editor"


def test_require_membership_manage_access_rejects_editor(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "editor")
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace._require_membership_manage_access("ws_1", "user_1")


def test_require_membership_manage_access_allows_moderator(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "moderator")
    assert chat_workspace._require_membership_manage_access("ws_1", "user_1") == "moderator"


def test_require_owner_or_partner_rejects_moderator(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "moderator")
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace._require_owner_or_partner("ws_1", "user_1")


@pytest.mark.parametrize("role", ["owner", "partner"])
def test_require_owner_or_partner_allows_owner_and_partner(monkeypatch, role):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: role)
    assert chat_workspace._require_owner_or_partner("ws_1", "user_1") == role


# ---------------------------------------------------------------------
# create_workspace() / rename_workspace()
# ---------------------------------------------------------------------

def test_create_workspace_falls_back_to_note_for_invalid_stage(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{
        "id": "ws_1", "name": "Untitled project", "owner_id": "owner_1",
        "show_attribution": True, "stage": "note", "active_stages": ["note"],
        "created_at": _now(), "updated_at": _now(),
    }])
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_workspace.create_workspace("owner_1", "  ", stage="not_a_real_stage")

    insert_query, insert_params = cursor.executed[0]
    assert insert_params[1] == "Untitled project"  # blank name -> default
    assert insert_params[3] == "note"              # invalid stage -> default
    assert result["stage"] == "note"


def test_create_workspace_uses_provided_valid_stage(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{
        "id": "ws_1", "name": "Widget", "owner_id": "owner_1",
        "show_attribution": True, "stage": "build", "active_stages": ["build"],
        "created_at": _now(), "updated_at": _now(),
    }])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.create_workspace("owner_1", "Widget", stage="build")

    _, insert_params = cursor.executed[0]
    assert insert_params[3] == "build"


def test_rename_workspace_requires_edit_access(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access",
                         MagicMock(side_effect=chat_workspace.WorkspaceAccessError("nope")))
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.rename_workspace("ws_1", "viewer_1", "New name")


def test_rename_workspace_raises_when_not_found(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "editor")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.rename_workspace("ws_missing", "user_1", "New name")


def test_rename_workspace_blank_name_only_touches_updated_at(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "editor")
    monkeypatch.setattr(chat_workspace, "get_workspace", lambda *a, **kw: _ws_row())
    cursor = FakeCursor(fetchone_results=[{"id": "ws_1"}])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.rename_workspace("ws_1", "user_1", "   ")

    update_query, _ = cursor.executed[0]
    assert "name" not in update_query


def test_rename_workspace_truncates_and_strips(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "editor")
    monkeypatch.setattr(chat_workspace, "get_workspace", lambda *a, **kw: _ws_row())
    cursor = FakeCursor(fetchone_results=[{"id": "ws_1"}])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.rename_workspace("ws_1", "user_1", "  " + "x" * 100 + "  ")

    _, params = cursor.executed[0]
    assert params[0] == "x" * 80


# ---------------------------------------------------------------------
# add_chat / create_chat_in_workspace / remove_chat
# ---------------------------------------------------------------------

def test_add_chat_requires_edit_access(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access",
                         MagicMock(side_effect=chat_workspace.WorkspaceAccessError("nope")))
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.add_chat("ws_1", "viewer_1", "chat_1")


def test_add_chat_syncs_by_owner_after_update(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "editor")
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         lambda *a, **kw: _ws_row(chat_ids=["chat_1", "chat_2"]))
    sync_mock = MagicMock()
    monkeypatch.setattr(chat_workspace, "_sync_by_owner", sync_mock)
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.add_chat("ws_1", "user_1", "chat_1")

    sync_mock.assert_called_once_with(["chat_1", "chat_2"])
    chat_workspace.write_audit.assert_called_once()


def test_create_chat_in_workspace_creates_then_attaches_then_refetches(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "editor")
    monkeypatch.setattr(chat_workspace.chat_store, "create_chat",
                         lambda user_id, title: {"id": "chat_new"})
    add_chat_mock = MagicMock()
    monkeypatch.setattr(chat_workspace, "add_chat", add_chat_mock)
    monkeypatch.setattr(chat_workspace.chat_store, "get_chat",
                         lambda chat_id, user_id: {"id": chat_id, "title": "New Chat"})

    result = chat_workspace.create_chat_in_workspace("ws_1", "user_1", title="New Chat")

    add_chat_mock.assert_called_once_with("ws_1", "user_1", "chat_new")
    assert result["id"] == "chat_new"


def test_remove_chat_detaches_without_delete(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "editor")
    monkeypatch.setattr(chat_workspace, "get_workspace", lambda *a, **kw: _ws_row(chat_ids=[]))
    monkeypatch.setattr(chat_workspace, "_sync_by_owner", MagicMock())
    monkeypatch.setattr(chat_workspace.chat_store, "chat_exists", lambda *a, **kw: True)
    set_linked_mock = MagicMock()
    monkeypatch.setattr(chat_workspace.chat_store, "set_linked_chats", set_linked_mock)
    delete_mock = MagicMock()
    monkeypatch.setattr(chat_workspace.chat_store, "delete_chat", delete_mock)
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.remove_chat("ws_1", "user_1", "chat_1", delete_chat=False)

    delete_mock.assert_not_called()
    set_linked_mock.assert_called_once_with("chat_1", "user_1", [])
    detach_query, _ = cursor.executed[0]
    assert "workspace_id = null" in detach_query


def test_remove_chat_deletes_when_requested(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "editor")
    monkeypatch.setattr(chat_workspace, "get_workspace", lambda *a, **kw: _ws_row(chat_ids=[]))
    monkeypatch.setattr(chat_workspace, "_sync_by_owner", MagicMock())
    delete_mock = MagicMock()
    monkeypatch.setattr(chat_workspace.chat_store, "delete_chat", delete_mock)
    set_linked_mock = MagicMock()
    monkeypatch.setattr(chat_workspace.chat_store, "set_linked_chats", set_linked_mock)
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.remove_chat("ws_1", "user_1", "chat_1", delete_chat=True)

    delete_mock.assert_called_once_with("chat_1", "user_1")
    set_linked_mock.assert_not_called()
    # no "workspace_id = null" detach statement executed for delete_chat=True
    assert not any("workspace_id = null" in q for q, _ in cursor.executed)


# ---------------------------------------------------------------------
# delete_workspace() / workspace_for_chat()
# ---------------------------------------------------------------------

def test_delete_workspace_requires_owner_or_partner(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_owner_or_partner",
                         MagicMock(side_effect=chat_workspace.WorkspaceAccessError("nope")))
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.delete_workspace("ws_1", "moderator_1")


def test_delete_workspace_unlinks_every_remaining_chat(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_owner_or_partner", lambda *a, **kw: "owner")
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         lambda *a, **kw: _ws_row(chat_ids=["chat_1", "chat_2"]))
    monkeypatch.setattr(chat_workspace.chat_store, "chat_exists", lambda *a, **kw: True)
    set_linked_mock = MagicMock()
    monkeypatch.setattr(chat_workspace.chat_store, "set_linked_chats", set_linked_mock)
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.delete_workspace("ws_1", "owner_1")

    assert set_linked_mock.call_count == 2
    chat_workspace.write_audit.assert_called_once()


def test_delete_workspace_detaches_chats_workspace_id(monkeypatch):
    """Bug fix regression test: deleting a workspace must null out
    workspace_id on every chat still pointing at it -- not just clear
    linked_chat_ids -- or those chats keep showing as grouped under a
    project that no longer exists (chat tab lists chats independent of
    any workspace join, unlike tabs that re-resolve the workspace and
    safely 404). This must run as an `update ... where workspace_id = %s`
    against the chats table BEFORE (or as part of the same transaction
    as) the `delete from workspaces` -- not filtered down to just
    ws["chat_ids"], since that array intentionally omits other members'
    private chats, which still need detaching."""
    monkeypatch.setattr(chat_workspace, "_require_owner_or_partner", lambda *a, **kw: "owner")
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         lambda *a, **kw: _ws_row(chat_ids=["chat_1", "chat_2"]))
    monkeypatch.setattr(chat_workspace.chat_store, "chat_exists", lambda *a, **kw: True)
    monkeypatch.setattr(chat_workspace.chat_store, "set_linked_chats", MagicMock())
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.delete_workspace("ws_1", "owner_1")

    detach_queries = [(q, p) for q, p in cursor.executed if "workspace_id = null" in q]
    assert len(detach_queries) == 1
    query, params = detach_queries[0]
    assert "update chats" in query
    assert "where workspace_id = %s" in query
    assert params[-1] == "ws_1"

    delete_queries = [q for q, _ in cursor.executed if q.strip().startswith("delete from workspaces")]
    assert len(delete_queries) == 1


def test_workspace_for_chat_returns_none_when_chat_missing_or_unowned(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.workspace_for_chat("chat_1", "user_1") is None


def test_workspace_for_chat_returns_none_when_chat_has_no_workspace(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"workspace_id": None}])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.workspace_for_chat("chat_1", "user_1") is None


def test_workspace_for_chat_returns_none_when_workspace_lookup_404s(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"workspace_id": "ws_1"}])
    _install_fake_cursor(monkeypatch, cursor)
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         MagicMock(side_effect=FileNotFoundError("ws_1")))
    assert chat_workspace.workspace_for_chat("chat_1", "user_1") is None


def test_workspace_for_chat_returns_workspace_when_found(monkeypatch):
    cursor = FakeCursor(fetchone_results=[{"workspace_id": "ws_1"}])
    _install_fake_cursor(monkeypatch, cursor)
    monkeypatch.setattr(chat_workspace, "get_workspace", lambda *a, **kw: _ws_row())
    result = chat_workspace.workspace_for_chat("chat_1", "user_1")
    assert result["id"] == "ws_1"


# ---------------------------------------------------------------------
# list_notify_targets() / list_members()
# ---------------------------------------------------------------------

def test_list_notify_targets_empty_when_workspace_missing(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.list_notify_targets("ws_missing") == []


def test_list_notify_targets_dedupes_and_drops_falsy(monkeypatch):
    cursor = FakeCursor(
        fetchone_results=[{"owner_id": "owner_1"}],
        fetchall_results=[[{"user_id": "owner_1"}, {"user_id": "partner_1"}, {"user_id": None}]],
    )
    _install_fake_cursor(monkeypatch, cursor)
    result = set(chat_workspace.list_notify_targets("ws_1"))
    assert result == {"owner_1", "partner_1"}


def test_list_members_requires_access(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_access",
                         MagicMock(side_effect=FileNotFoundError("ws_1")))
    with pytest.raises(FileNotFoundError):
        chat_workspace.list_members("ws_1", "stranger")


def test_list_members_shapes_rows(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_access", lambda *a, **kw: "viewer")
    cursor = FakeCursor(fetchall_results=[[
        {"user_id": "u1", "role": "editor", "can_toggle_attribution": False, "added_at": _now()},
    ]])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.list_members("ws_1", "user_1")
    assert result == [{
        "user_id": "u1", "role": "editor", "can_toggle_attribution": False,
        "added_at": _now().isoformat(),
    }]


# ---------------------------------------------------------------------
# add_member() / update_member_role() / remove_member()
# ---------------------------------------------------------------------

def test_add_member_rejects_invalid_role():
    with pytest.raises(ValueError):
        chat_workspace.add_member("ws_1", "owner_1", "target_1", role="superadmin")


def test_add_member_requires_moderator_or_above(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         MagicMock(side_effect=chat_workspace.WorkspaceAccessError("nope")))
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.add_member("ws_1", "editor_1", "target_1", role="viewer")


def test_add_member_moderator_cannot_grant_partner(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "moderator")
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.add_member("ws_1", "mod_1", "target_1", role="partner")


def test_add_member_owner_can_grant_partner(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": "owner_1"},
        {"user_id": "target_1", "role": "partner", "can_toggle_attribution": False, "added_at": _now()},
    ])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.add_member("ws_1", "owner_1", "target_1", role="partner")
    assert result["role"] == "partner"


def test_add_member_raises_when_workspace_missing(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.add_member("ws_missing", "owner_1", "target_1")


def test_add_member_rejects_adding_the_owner_as_a_member(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[{"owner_id": "owner_1"}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.add_member("ws_1", "owner_1", "owner_1", role="viewer")


def test_update_member_role_rejects_invalid_role():
    with pytest.raises(ValueError):
        chat_workspace.update_member_role("ws_1", "owner_1", "target_1", "superadmin")


def test_update_member_role_raises_when_target_not_a_member(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.update_member_role("ws_1", "owner_1", "target_1", "editor")


def test_update_member_role_moderator_cannot_touch_partner_tier(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "moderator")
    cursor = FakeCursor(fetchone_results=[{"role": "partner"}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.update_member_role("ws_1", "mod_1", "target_1", "editor")


def test_update_member_role_moderator_can_touch_non_partner_tiers(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "moderator")
    cursor = FakeCursor(fetchone_results=[
        {"role": "editor"},
        {"user_id": "target_1", "role": "moderator", "can_toggle_attribution": False, "added_at": _now()},
    ])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.update_member_role("ws_1", "mod_1", "target_1", "moderator")
    assert result["role"] == "moderator"


def test_remove_member_raises_when_target_not_a_member(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.remove_member("ws_1", "owner_1", "target_1")


def test_remove_member_moderator_cannot_remove_partner(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "moderator")
    cursor = FakeCursor(fetchone_results=[{"role": "partner"}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.remove_member("ws_1", "mod_1", "target_1")


def test_remove_member_owner_can_remove_partner(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_membership_manage_access",
                         lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[{"role": "partner"}])
    _install_fake_cursor(monkeypatch, cursor)
    chat_workspace.remove_member("ws_1", "owner_1", "target_1")
    chat_workspace.write_audit.assert_called_once()


# ---------------------------------------------------------------------
# remove_owner() — forced removal
# ---------------------------------------------------------------------

def test_remove_owner_requires_partner_role(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "moderator")
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.remove_owner("ws_1", "mod_1")


def test_remove_owner_raises_when_workspace_missing(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "partner")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.remove_owner("ws_missing", "partner_1")


def test_remove_owner_raises_when_already_joint(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "partner")
    cursor = FakeCursor(fetchone_results=[{"owner_id": None}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.remove_owner("ws_1", "partner_1")


def test_remove_owner_nulls_owner_and_clears_votes(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "partner")
    monkeypatch.setattr(chat_workspace, "get_workspace", lambda *a, **kw: _ws_row(owner_id=None))
    cursor = FakeCursor(fetchone_results=[{"owner_id": "owner_1"}])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.remove_owner("ws_1", "partner_1")

    update_query, update_params = cursor.executed[1]
    assert "owner_id = null" in update_query
    assert update_params[-1] == "ws_1"
    delete_query, _ = cursor.executed[2]
    assert "delete from workspace_owner_votes" in delete_query
    chat_workspace.write_audit.assert_called_once()


# ---------------------------------------------------------------------
# leave_workspace() — voluntary exit
# ---------------------------------------------------------------------

def test_leave_workspace_raises_when_no_relationship(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: None)
    with pytest.raises(FileNotFoundError):
        chat_workspace.leave_workspace("ws_1", "stranger")


def test_leave_workspace_non_owner_just_deletes_membership_row(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "editor")
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_workspace.leave_workspace("ws_1", "editor_1")

    assert result is None
    delete_query, _ = cursor.executed[0]
    assert "delete from workspace_members" in delete_query
    chat_workspace.write_audit.assert_called_once()


def test_leave_workspace_owner_with_no_successor_goes_joint(monkeypatch):
    def fake_member_role(ws_id, uid):
        return "owner" if uid == "owner_1" else None
    monkeypatch.setattr(chat_workspace, "member_role", fake_member_role)
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_workspace.leave_workspace("ws_1", "owner_1")

    assert result is None
    update_query, update_params = cursor.executed[0]
    assert "owner_id = null" in update_query
    assert update_params[-1] == "ws_1"


def test_leave_workspace_owner_successor_must_be_a_current_partner(monkeypatch):
    def fake_member_role(ws_id, uid):
        if uid == "owner_1":
            return "owner"
        if uid == "not_a_partner":
            return "editor"
        return None
    monkeypatch.setattr(chat_workspace, "member_role", fake_member_role)
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.leave_workspace("ws_1", "owner_1", successor_id="not_a_partner")


def test_leave_workspace_owner_names_successor_transfers_ownership(monkeypatch):
    def fake_member_role(ws_id, uid):
        if uid == "owner_1":
            return "owner"
        if uid == "partner_1":
            return "partner"
        return None
    monkeypatch.setattr(chat_workspace, "member_role", fake_member_role)
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_workspace.leave_workspace("ws_1", "owner_1", successor_id="partner_1")

    assert result is None
    owner_update_query, owner_update_params = cursor.executed[0]
    assert owner_update_params[0] == "partner_1"
    member_delete_query, member_delete_params = cursor.executed[1]
    assert "delete from workspace_members" in member_delete_query
    assert member_delete_params == ("ws_1", "partner_1")


# ---------------------------------------------------------------------
# get_vote_status() / cast_vote()
# ---------------------------------------------------------------------

def test_get_vote_status_raises_when_workspace_missing(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_access", lambda *a, **kw: "partner")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.get_vote_status("ws_missing", "partner_1")


def test_get_vote_status_reports_joint_and_votes(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_access", lambda *a, **kw: "partner")
    cursor = FakeCursor(
        fetchone_results=[{"owner_id": None}, {"n": 3}],
        fetchall_results=[[
            {"voter_id": "p1", "vote_target": "p2", "cast_at": _now()},
        ]],
    )
    _install_fake_cursor(monkeypatch, cursor)

    result = chat_workspace.get_vote_status("ws_1", "p1")

    assert result["is_joint"] is True
    assert result["total_partners"] == 3
    assert result["votes"] == [{"voter_id": "p1", "vote_target": "p2", "cast_at": _now().isoformat()}]


def test_cast_vote_requires_partner_role(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "moderator")
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.cast_vote("ws_1", "mod_1", "p2")


def test_cast_vote_raises_when_workspace_missing(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "partner")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.cast_vote("ws_missing", "p1", "p2")


def test_cast_vote_raises_when_workspace_already_has_an_owner(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "partner")
    cursor = FakeCursor(fetchone_results=[{"owner_id": "owner_1"}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.cast_vote("ws_1", "p1", "p2")


def test_cast_vote_rejects_non_partner_vote_target(monkeypatch):
    def fake_member_role(ws_id, uid):
        return "partner" if uid == "p1" else "editor"
    monkeypatch.setattr(chat_workspace, "member_role", fake_member_role)
    cursor = FakeCursor(fetchone_results=[{"owner_id": None}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.cast_vote("ws_1", "p1", "not_a_partner")


def test_cast_vote_records_vote_without_majority(monkeypatch):
    def fake_member_role(ws_id, uid):
        return "partner"
    monkeypatch.setattr(chat_workspace, "member_role", fake_member_role)
    monkeypatch.setattr(chat_workspace, "get_vote_status",
                         lambda *a, **kw: {"workspace_id": "ws_1", "is_joint": True,
                                            "total_partners": 3, "votes": []})
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": None},   # initial owner check
        {"n": 3},             # total_partners
        {"vote_target": "p2", "n": 1},  # top vote, not a majority of 3
    ])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.cast_vote("ws_1", "p1", "p2")

    # no election update statement should have been executed
    assert not any("owner_id = %s" in q for q, _ in cursor.executed[1:])
    chat_workspace.write_audit.assert_not_called()


def test_cast_vote_strict_majority_elects_winner_and_clears_ballot(monkeypatch):
    def fake_member_role(ws_id, uid):
        return "partner"
    monkeypatch.setattr(chat_workspace, "member_role", fake_member_role)
    monkeypatch.setattr(chat_workspace, "get_vote_status",
                         lambda *a, **kw: {"workspace_id": "ws_1", "is_joint": False,
                                            "total_partners": 3, "votes": []})
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": None},           # initial owner check
        {"n": 3},                     # total_partners
        {"vote_target": "p2", "n": 2},  # 2 of 3 -> strict majority
    ])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.cast_vote("ws_1", "p1", "p2")

    queries = [q for q, _ in cursor.executed]
    assert any("update workspaces set owner_id" in q for q in queries)
    assert any("delete from workspace_members" in q for q in queries)
    assert any("delete from workspace_owner_votes" in q for q in queries)
    chat_workspace.write_audit.assert_called_once()
    args = chat_workspace.write_audit.call_args[0]
    assert args[1] == "workspace.owner_elected"


def test_cast_vote_exact_half_is_not_a_strict_majority(monkeypatch):
    def fake_member_role(ws_id, uid):
        return "partner"
    monkeypatch.setattr(chat_workspace, "member_role", fake_member_role)
    monkeypatch.setattr(chat_workspace, "get_vote_status",
                         lambda *a, **kw: {"workspace_id": "ws_1", "is_joint": True,
                                            "total_partners": 4, "votes": []})
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": None},
        {"n": 4},
        {"vote_target": "p2", "n": 2},  # 2 of 4 -- exactly half, not strict majority
    ])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.cast_vote("ws_1", "p1", "p2")

    chat_workspace.write_audit.assert_not_called()


def test_cast_vote_joint_vote_target_none_never_elects(monkeypatch):
    def fake_member_role(ws_id, uid):
        return "partner"
    monkeypatch.setattr(chat_workspace, "member_role", fake_member_role)
    monkeypatch.setattr(chat_workspace, "get_vote_status",
                         lambda *a, **kw: {"workspace_id": "ws_1", "is_joint": True,
                                            "total_partners": 3, "votes": []})
    cursor = FakeCursor(fetchone_results=[
        {"owner_id": None},
        {"n": 3},
        None,  # no non-null vote_target rows at all (everyone voted joint / no rows)
    ])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.cast_vote("ws_1", "p1", None)

    chat_workspace.write_audit.assert_not_called()


# ---------------------------------------------------------------------
# set_show_attribution() / set_moderator_attribution_grant() /
# can_see_attribution()
# ---------------------------------------------------------------------

def test_set_show_attribution_raises_when_workspace_missing(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: None)
    with pytest.raises(FileNotFoundError):
        chat_workspace.set_show_attribution("ws_1", "user_1", True)


def test_set_show_attribution_viewer_cannot_toggle(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "viewer")
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.set_show_attribution("ws_1", "viewer_1", True)


def test_set_show_attribution_moderator_without_flag_rejected(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "moderator")
    cursor = FakeCursor(fetchone_results=[{"can_toggle_attribution": False}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.set_show_attribution("ws_1", "mod_1", True)


def test_set_show_attribution_moderator_with_flag_allowed(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "moderator")
    monkeypatch.setattr(chat_workspace, "get_workspace", lambda *a, **kw: _ws_row())
    cursor = FakeCursor(fetchone_results=[{"can_toggle_attribution": True}])
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.set_show_attribution("ws_1", "mod_1", True)
    chat_workspace.write_audit.assert_called_once()


@pytest.mark.parametrize("role", ["owner", "partner"])
def test_set_show_attribution_owner_and_partner_always_allowed(monkeypatch, role):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: role)
    monkeypatch.setattr(chat_workspace, "get_workspace", lambda *a, **kw: _ws_row())
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)

    chat_workspace.set_show_attribution("ws_1", "user_1", False)
    chat_workspace.write_audit.assert_called_once()


def test_set_moderator_attribution_grant_requires_owner_or_partner(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_owner_or_partner",
                         MagicMock(side_effect=chat_workspace.WorkspaceAccessError("nope")))
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.set_moderator_attribution_grant("ws_1", "mod_1", "target_1", True)


def test_set_moderator_attribution_grant_raises_when_target_not_a_member(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_owner_or_partner", lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(FileNotFoundError):
        chat_workspace.set_moderator_attribution_grant("ws_1", "owner_1", "target_1", True)


def test_set_moderator_attribution_grant_rejects_non_moderator_target(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_owner_or_partner", lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[{"role": "editor"}])
    _install_fake_cursor(monkeypatch, cursor)
    with pytest.raises(ValueError):
        chat_workspace.set_moderator_attribution_grant("ws_1", "owner_1", "target_1", True)


def test_set_moderator_attribution_grant_success(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_owner_or_partner", lambda *a, **kw: "owner")
    cursor = FakeCursor(fetchone_results=[
        {"role": "moderator"},
        {"user_id": "target_1", "role": "moderator", "can_toggle_attribution": True, "added_at": _now()},
    ])
    _install_fake_cursor(monkeypatch, cursor)
    result = chat_workspace.set_moderator_attribution_grant("ws_1", "owner_1", "target_1", True)
    assert result["can_toggle_attribution"] is True


@pytest.mark.parametrize("role", ["owner", "partner", "moderator"])
def test_can_see_attribution_always_true_for_high_tiers(monkeypatch, role):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: role)
    assert chat_workspace.can_see_attribution("ws_1", "user_1") is True


def test_can_see_attribution_viewer_follows_workspace_flag_on(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "viewer")
    cursor = FakeCursor(fetchone_results=[{"show_attribution": True}])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.can_see_attribution("ws_1", "viewer_1") is True


def test_can_see_attribution_editor_follows_workspace_flag_off(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "editor")
    cursor = FakeCursor(fetchone_results=[{"show_attribution": False}])
    _install_fake_cursor(monkeypatch, cursor)
    assert chat_workspace.can_see_attribution("ws_1", "editor_1") is False


def test_can_see_attribution_no_access_is_false(monkeypatch):
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: None)
    assert chat_workspace.can_see_attribution("ws_1", "stranger") is False


# ---------------------------------------------------------------------
# export_workspace_data() / import_workspace_data()
# ---------------------------------------------------------------------

def test_export_workspace_data_scopes_chats_and_includes_role(monkeypatch):
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         lambda *a, **kw: _ws_row(chat_ids=["chat_1", "chat_2"]))
    monkeypatch.setattr(chat_workspace, "member_role", lambda *a, **kw: "editor")
    export_mock = MagicMock(return_value=[{"id": "chat_1"}])
    monkeypatch.setattr(chat_workspace.chat_store, "export_chats", export_mock)

    manifest = chat_workspace.export_workspace_data("ws_1", "user_1")

    export_mock.assert_called_once_with("user_1", ["chat_1", "chat_2"])
    assert manifest["workspace"]["your_role"] == "editor"
    assert manifest["chats"] == [{"id": "chat_1"}]
    chat_workspace.write_audit.assert_called_once()


def test_export_workspace_data_propagates_no_access_error(monkeypatch):
    monkeypatch.setattr(chat_workspace, "get_workspace",
                         MagicMock(side_effect=FileNotFoundError("ws_1")))
    with pytest.raises(FileNotFoundError):
        chat_workspace.export_workspace_data("ws_1", "stranger")


def test_import_workspace_data_requires_edit_access(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access",
                         MagicMock(side_effect=chat_workspace.WorkspaceAccessError("nope")))
    with pytest.raises(chat_workspace.WorkspaceAccessError):
        chat_workspace.import_workspace_data("ws_1", "viewer_1", {"chats": []})


def test_import_workspace_data_restores_chats_owned_by_caller(monkeypatch):
    monkeypatch.setattr(chat_workspace, "_require_edit_access", lambda *a, **kw: "editor")
    restore_mock = MagicMock(return_value=[{"id": "chat_new_1"}, {"id": "chat_new_2"}])
    monkeypatch.setattr(chat_workspace.chat_store, "restore_chats", restore_mock)

    result = chat_workspace.import_workspace_data(
        "ws_1", "user_1", {"chats": [{"id": "old_chat"}]})

    restore_mock.assert_called_once_with("user_1", [{"id": "old_chat"}], workspace_id="ws_1")
    assert result == {"restored_chat_ids": ["chat_new_1", "chat_new_2"], "count": 2}
    chat_workspace.write_audit.assert_called_once()
