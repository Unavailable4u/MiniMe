"""
tests/unit/test_eo_panel_content.py — Patch 7e-S4.

eo/panel_content.py had zero test coverage before this. Priorities,
worst-silent-failure first:

  1. The allowlist gate: get_content/set_content/delete_content must
     all reject an unknown panel_key loudly (ValueError), never write
     or read a row under a key nothing else will ever look up.
  2. invalidate_for_nodes()'s selective-invalidation logic (the actual
     bug-audit fix this module documents) — only GENERATED_PANEL_KEYS
     panels are candidates at all, a panel with source_node_ids=None is
     always stale (whole-notebook regenerate), a panel whose recorded
     scope doesn't overlap the deleted node_ids must be LEFT ALONE
     (the regression the old clear_workspace()-on-every-delete behavior
     used to cause), and manual-paste panels are never touched.
  3. write_panel_from_role()'s role->panel_key mapping, its "no text ->
     no write" backstop, and the prd_writer-only Mermaid-gate call.
  4. _gate_prd_mermaid()'s strip-not-repair behavior.

Isolation: panel_content.py does `from eo import db`, reached through
that module object; FakeCursor/FakeCursorContext queue canned
fetchone()/fetchall() results in call order, same convention as
test_eo_chat_workspace.py and test_eo_chat_store.py. write_audit is
patched as `panel_content.write_audit` for the same "already bound
into this module's own namespace at import time" reason those files'
own docstrings give.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

import eo.panel_content as panel_content


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
        panel_content.db, "cursor",
        lambda **kwargs: FakeCursorContext(cursor, calls_log, **kwargs),
    )
    return calls_log


def _now():
    return datetime(2026, 1, 1, tzinfo=timezone.utc)


def _content_row(workspace_id="ws_1", panel_key="mindmap", content="hello",
                  source_node_ids=None, content_source="manual"):
    return {
        "workspace_id": workspace_id, "panel_key": panel_key, "content": content,
        "updated_at": _now(), "updated_by": "user_1",
        "source_node_ids": source_node_ids, "content_source": content_source,
    }


@pytest.fixture(autouse=True)
def _no_real_audit(monkeypatch):
    monkeypatch.setattr(panel_content, "write_audit", MagicMock())


# ---------------------------------------------------------------------
# get_content / list_content
# ---------------------------------------------------------------------

def test_get_content_rejects_unknown_panel_key():
    with pytest.raises(ValueError):
        panel_content.get_content("ws_1", "not_a_real_panel")


def test_get_content_returns_empty_shape_when_nothing_saved(monkeypatch):
    cursor = FakeCursor(fetchone_results=[None])
    _install_fake_cursor(monkeypatch, cursor)
    result = panel_content.get_content("ws_1", "mindmap")
    assert result["content"] == ""
    assert result["updated_at"] is None
    assert result["content_source"] is None


def test_get_content_returns_saved_row_when_present(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_content_row()])
    _install_fake_cursor(monkeypatch, cursor)
    result = panel_content.get_content("ws_1", "mindmap")
    assert result["content"] == "hello"
    assert result["content_source"] == "manual"


def test_get_content_defaults_content_source_to_manual_when_row_has_none(monkeypatch):
    row = _content_row(content_source=None)
    cursor = FakeCursor(fetchone_results=[row])
    _install_fake_cursor(monkeypatch, cursor)
    result = panel_content.get_content("ws_1", "mindmap")
    assert result["content_source"] == "manual"


def test_list_content_keys_by_panel_key(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[
        _content_row(panel_key="mindmap"), _content_row(panel_key="prd", content="prd text"),
    ]])
    _install_fake_cursor(monkeypatch, cursor)
    result = panel_content.list_content("ws_1")
    assert set(result.keys()) == {"mindmap", "prd"}
    assert result["prd"]["content"] == "prd text"


def test_list_content_omits_panels_with_no_saved_row(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    assert panel_content.list_content("ws_1") == {}


# ---------------------------------------------------------------------
# set_content
# ---------------------------------------------------------------------

def test_set_content_rejects_unknown_panel_key():
    with pytest.raises(ValueError):
        panel_content.set_content("ws_1", "bogus", "text", "user_1")


def test_set_content_rejects_unknown_content_source():
    with pytest.raises(ValueError):
        panel_content.set_content("ws_1", "mindmap", "text", "user_1", content_source="robot")


def test_set_content_defaults_to_manual_source_and_writes_audit(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_content_row()])
    _install_fake_cursor(monkeypatch, cursor)

    result = panel_content.set_content("ws_1", "mindmap", "hello", "user_1")

    _, params = cursor.executed[0]
    assert params[-1] == "manual"  # content_source param
    assert result["content"] == "hello"
    panel_content.write_audit.assert_called_once()


def test_set_content_passes_through_explicit_chat_source_and_source_node_ids(monkeypatch):
    cursor = FakeCursor(fetchone_results=[_content_row(content_source="chat")])
    _install_fake_cursor(monkeypatch, cursor)

    panel_content.set_content("ws_1", "mindmap", "hello", "user_1",
                               source_node_ids=["n1", "n2"], content_source="chat")

    _, params = cursor.executed[0]
    assert params[-2] == ["n1", "n2"]  # source_node_ids param
    assert params[-1] == "chat"


# ---------------------------------------------------------------------
# delete_content
# ---------------------------------------------------------------------

def test_delete_content_rejects_unknown_panel_key():
    with pytest.raises(ValueError):
        panel_content.delete_content("ws_1", "bogus", "user_1")


def test_delete_content_writes_audit(monkeypatch):
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)
    panel_content.delete_content("ws_1", "mindmap", "user_1")
    panel_content.write_audit.assert_called_once()


# ---------------------------------------------------------------------
# _gate_prd_mermaid
# ---------------------------------------------------------------------

def test_gate_prd_mermaid_no_op_without_fenced_block():
    text = "Just a plain PRD with no diagrams."
    assert panel_content._gate_prd_mermaid(text) == text


def test_gate_prd_mermaid_keeps_a_valid_block(monkeypatch):
    monkeypatch.setattr(panel_content, "looks_valid_mermaid", lambda s: True)
    text = "before\n```mermaid\ngraph TD; A-->B;\n```\nafter"
    result = panel_content._gate_prd_mermaid(text)
    assert "graph TD; A-->B;" in result


def test_gate_prd_mermaid_replaces_an_invalid_block_with_fallback_note(monkeypatch):
    monkeypatch.setattr(panel_content, "looks_valid_mermaid", lambda s: False)
    text = "before\n```mermaid\nnonsense\n```\nafter"
    result = panel_content._gate_prd_mermaid(text)
    assert "nonsense" not in result
    assert panel_content._PRD_WIRING_FALLBACK_NOTE in result


def test_gate_prd_mermaid_replaces_an_empty_block(monkeypatch):
    monkeypatch.setattr(panel_content, "looks_valid_mermaid", lambda s: True)
    text = "```mermaid\n\n```"
    result = panel_content._gate_prd_mermaid(text)
    assert panel_content._PRD_WIRING_FALLBACK_NOTE in result


# ---------------------------------------------------------------------
# _text_from_role_result
# ---------------------------------------------------------------------

def test_text_from_role_result_prefers_mermaid_key():
    result = {"text": "fallback", "mermaid": "graph TD; A-->B;"}
    assert panel_content._text_from_role_result(result) == "graph TD; A-->B;"


def test_text_from_role_result_falls_back_to_text_key():
    result = {"text": "prose only"}
    assert panel_content._text_from_role_result(result) == "prose only"


def test_text_from_role_result_returns_empty_for_non_dict():
    assert panel_content._text_from_role_result("not a dict") == ""


def test_text_from_role_result_strips_whitespace():
    assert panel_content._text_from_role_result({"text": "  hi  "}) == "hi"


# ---------------------------------------------------------------------
# write_panel_from_role
# ---------------------------------------------------------------------

def test_write_panel_from_role_returns_none_for_unmapped_role():
    result = panel_content.write_panel_from_role("ws_1", "some_other_role", {"text": "x"}, "user_1")
    assert result is None


def test_write_panel_from_role_returns_none_when_result_has_no_text():
    result = panel_content.write_panel_from_role("ws_1", "prd_writer", {"text": ""}, "user_1")
    assert result is None


def test_write_panel_from_role_writes_with_chat_source_for_mapped_role(monkeypatch):
    set_content_mock = MagicMock(return_value={"panel_key": "architecture"})
    monkeypatch.setattr(panel_content, "set_content", set_content_mock)

    result = panel_content.write_panel_from_role(
        "ws_1", "architecture_diagrammer", {"text": "diagram text"}, "user_1")

    set_content_mock.assert_called_once_with(
        "ws_1", "architecture", "diagram text", "user_1", content_source="chat")
    assert result == {"panel_key": "architecture"}


def test_write_panel_from_role_gates_mermaid_only_for_prd_writer(monkeypatch):
    monkeypatch.setattr(panel_content, "looks_valid_mermaid", lambda s: False)
    set_content_mock = MagicMock(return_value={"panel_key": "prd"})
    monkeypatch.setattr(panel_content, "set_content", set_content_mock)

    panel_content.write_panel_from_role(
        "ws_1", "prd_writer", {"text": "before\n```mermaid\nbad\n```\nafter"}, "user_1")

    written_text = set_content_mock.call_args[0][2]
    assert "bad" not in written_text
    assert panel_content._PRD_WIRING_FALLBACK_NOTE in written_text


def test_write_panel_from_role_does_not_gate_mermaid_for_other_roles(monkeypatch):
    monkeypatch.setattr(panel_content, "looks_valid_mermaid", lambda s: False)
    set_content_mock = MagicMock(return_value={"panel_key": "architecture"})
    monkeypatch.setattr(panel_content, "set_content", set_content_mock)

    panel_content.write_panel_from_role(
        "ws_1", "architecture_diagrammer", {"mermaid": "```mermaid\nbad\n```"}, "user_1")

    written_text = set_content_mock.call_args[0][2]
    assert "bad" in written_text  # not stripped -- gate is prd_writer-only


# ---------------------------------------------------------------------
# invalidate_for_nodes
# ---------------------------------------------------------------------

def test_invalidate_for_nodes_returns_empty_when_no_node_ids(monkeypatch):
    cursor = FakeCursor()
    _install_fake_cursor(monkeypatch, cursor)
    assert panel_content.invalidate_for_nodes("ws_1", [], "user_1") == []
    assert cursor.executed == []


def test_invalidate_for_nodes_clears_a_panel_with_no_recorded_scope(monkeypatch):
    """source_node_ids is NULL -> generated from the whole notebook ->
    always stale for any delete."""
    cursor = FakeCursor(fetchall_results=[[
        {"panel_key": "mindmap", "source_node_ids": None},
    ]])
    _install_fake_cursor(monkeypatch, cursor)

    cleared = panel_content.invalidate_for_nodes("ws_1", ["n1"], "user_1")

    assert cleared == ["mindmap"]
    delete_query, delete_params = cursor.executed[1]
    assert "delete from workspace_panel_content" in delete_query
    assert delete_params == ("ws_1", "mindmap")
    panel_content.write_audit.assert_called_once()


def test_invalidate_for_nodes_clears_a_panel_whose_scope_overlaps(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[
        {"panel_key": "study_quiz", "source_node_ids": ["n1", "n9"]},
    ]])
    _install_fake_cursor(monkeypatch, cursor)

    cleared = panel_content.invalidate_for_nodes("ws_1", ["n1", "n2"], "user_1")

    assert cleared == ["study_quiz"]


def test_invalidate_for_nodes_leaves_a_panel_with_non_overlapping_scope_alone(monkeypatch):
    """The actual bug-audit regression case: a generated panel scoped to
    sources NOT in this delete batch must not be touched."""
    cursor = FakeCursor(fetchall_results=[[
        {"panel_key": "study_quiz", "source_node_ids": ["n5", "n6"]},
    ]])
    _install_fake_cursor(monkeypatch, cursor)

    cleared = panel_content.invalidate_for_nodes("ws_1", ["n1", "n2"], "user_1")

    assert cleared == []
    assert len(cursor.executed) == 1  # only the initial select -- no delete fired
    panel_content.write_audit.assert_not_called()


def test_invalidate_for_nodes_only_queries_generated_panel_keys(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)

    panel_content.invalidate_for_nodes("ws_1", ["n1"], "user_1")

    select_query, select_params = cursor.executed[0]
    queried_keys = set(select_params[1])
    assert queried_keys == panel_content.GENERATED_PANEL_KEYS
    # manual-paste-only panels must never be candidates for this cascade
    assert "prd" not in queried_keys
    assert "architecture" not in queried_keys


def test_invalidate_for_nodes_no_audit_when_nothing_cleared(monkeypatch):
    cursor = FakeCursor(fetchall_results=[[]])
    _install_fake_cursor(monkeypatch, cursor)
    panel_content.invalidate_for_nodes("ws_1", ["n1"], "user_1")
    panel_content.write_audit.assert_not_called()


# ---------------------------------------------------------------------
# clear_workspace
# ---------------------------------------------------------------------

def test_clear_workspace_returns_row_count_and_writes_audit_when_nonzero(monkeypatch):
    cursor = FakeCursor()
    cursor.rowcount = 3
    _install_fake_cursor(monkeypatch, cursor)

    count = panel_content.clear_workspace("ws_1", "user_1")

    assert count == 3
    panel_content.write_audit.assert_called_once()


def test_clear_workspace_no_audit_when_zero_rows_deleted(monkeypatch):
    cursor = FakeCursor()
    cursor.rowcount = 0
    _install_fake_cursor(monkeypatch, cursor)

    count = panel_content.clear_workspace("ws_1", "user_1")

    assert count == 0
    panel_content.write_audit.assert_not_called()
