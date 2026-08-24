"""
tests/unit/test_eo_correction_candidates.py — Patch 7e (content/
knowledge group, continued from 7e-3).

eo/correction_candidates.py had zero test coverage before this. It's
the pending list a located correction sits in until a person accepts
or rejects it in the Patch Review tab, and per its own module
docstring it's the one path that ever calls
eo/secondary_data.py:apply_patch() for a correction -- getting the
apply-then-pop ordering backwards would either silently drop a
correction that never actually took effect, or leave a phantom
candidate around for one that did. Highest-value things to pin down,
in that order:

  1. accept_candidate()'s apply-BEFORE-pop ordering: a failing
     apply_patch() call must leave the candidate exactly where it was
     (never popped), so the person can retry or discard it, rather
     than the candidate vanishing on a failed apply.
  2. The `user_corrected: true` tagging happening only inside
     accept_candidate() (never at propose time, never inside the op's
     originating locator) and only for add/replace ops whose value is
     a dict -- not blindly stamped onto every op shape.
  3. accept_candidate()/reject_candidate() addressing by candidate_id,
     not list position -- same sibling-module fix pattern
     test_eo_note_candidates.py already pins down for
     eo/note_candidates.py.
  4. propose_candidate()'s required-field validation and its
     capture-before-time `before` snapshot (read once at propose time,
     never re-read at review time).

Isolation: correction_candidates.py does `from memory.bus import read,
write` AND `from eo.secondary_data import get_secondary_data,
apply_patch` -- both are ordinary module-level imports (bound names in
correction_candidates' own namespace), not deferred function-body
imports like eo/note_candidates.py's write_node/search_nodes calls.
So all four are patched directly on the correction_candidates module
object here, not on their source modules.
"""
from unittest.mock import MagicMock

import pytest

import eo.correction_candidates as correction_candidates


def _op(path="/topics/t1", value=None, kind="replace"):
    return {"op": kind, "path": path, "value": value if value is not None else {"name": "New Name"}}


# ---------------------------------------------------------------------
# _key
# ---------------------------------------------------------------------

def test_key_is_namespaced_by_workspace_id():
    assert correction_candidates._key("ws_1") == "correction_candidates:ws_1"


# ---------------------------------------------------------------------
# propose_candidate
# ---------------------------------------------------------------------

def test_propose_candidate_raises_when_required_fields_are_missing(monkeypatch):
    monkeypatch.setattr(correction_candidates, "get_secondary_data",
                         lambda ws: {"topics": {}})
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: default)

    with pytest.raises(ValueError):
        correction_candidates.propose_candidate("", "text", "scope", "t1", _op())
    with pytest.raises(ValueError):
        correction_candidates.propose_candidate("ws_1", "text", "scope", "", _op())
    with pytest.raises(ValueError):
        correction_candidates.propose_candidate("ws_1", "text", "scope", "t1", None)
    with pytest.raises(ValueError):
        correction_candidates.propose_candidate("ws_1", "text", "scope", "t1", {})


def test_propose_candidate_captures_the_topics_current_value_as_before(monkeypatch):
    monkeypatch.setattr(correction_candidates, "get_secondary_data",
                         lambda ws: {"topics": {"t1": {"name": "Old Name"}}})
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: default)
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)

    candidate = correction_candidates.propose_candidate("ws_1", "fix the name", "chat", "t1", _op())

    assert candidate["before"] == {"name": "Old Name"}


def test_propose_candidate_before_is_none_when_topic_does_not_exist_yet(monkeypatch):
    monkeypatch.setattr(correction_candidates, "get_secondary_data",
                         lambda ws: {"topics": {}})
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: default)
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)

    candidate = correction_candidates.propose_candidate("ws_1", "text", "scope", "t_missing", _op())

    assert candidate["before"] is None


def test_propose_candidate_generates_a_stable_candidate_id_with_corr_prefix(monkeypatch):
    monkeypatch.setattr(correction_candidates, "get_secondary_data", lambda ws: {"topics": {}})
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: default)
    seen = {}
    monkeypatch.setattr(correction_candidates, "write",
                         lambda key, value: seen.update({"value": value}))

    candidate = correction_candidates.propose_candidate("ws_1", "text", "scope", "t1", _op())

    assert candidate["candidate_id"].startswith("corr_")
    assert seen["value"][0]["candidate_id"] == candidate["candidate_id"]


def test_propose_candidate_stores_the_op_correction_text_and_scope_label(monkeypatch):
    monkeypatch.setattr(correction_candidates, "get_secondary_data", lambda ws: {"topics": {}})
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: default)
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)

    op = _op()
    candidate = correction_candidates.propose_candidate(
        "ws_1", "the summary was wrong", "workspace", "t1", op,
    )

    assert candidate["op"] == op
    assert candidate["correction_text"] == "the summary was wrong"
    assert candidate["scope_label"] == "workspace"
    assert candidate["topic_id"] == "t1"


def test_propose_candidate_appends_to_existing_candidates_without_dropping_them(monkeypatch):
    existing = [{"candidate_id": "corr_old", "topic_id": "t0"}]
    seen = {}
    monkeypatch.setattr(correction_candidates, "get_secondary_data", lambda ws: {"topics": {}})
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(existing))
    monkeypatch.setattr(correction_candidates, "write",
                         lambda key, value: seen.update({"key": key, "value": value}))

    correction_candidates.propose_candidate("ws_1", "text", "scope", "t1", _op())

    assert seen["key"] == "correction_candidates:ws_1"
    assert len(seen["value"]) == 2
    assert seen["value"][0] == existing[0]


# ---------------------------------------------------------------------
# list_candidates
# ---------------------------------------------------------------------

def test_list_candidates_returns_empty_list_by_default(monkeypatch):
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: default)
    assert correction_candidates.list_candidates("ws_1") == []


def test_list_candidates_reads_the_workspace_scoped_key(monkeypatch):
    seen = {}

    def fake_read(key, default=None):
        seen["key"] = key
        return default

    monkeypatch.setattr(correction_candidates, "read", fake_read)
    correction_candidates.list_candidates("ws_1")

    assert seen["key"] == "correction_candidates:ws_1"


# ---------------------------------------------------------------------
# accept_candidate
# ---------------------------------------------------------------------

def test_accept_candidate_raises_file_not_found_for_unknown_id(monkeypatch):
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: [])
    with pytest.raises(FileNotFoundError):
        correction_candidates.accept_candidate("ws_1", "corr_missing")


def test_accept_candidate_applies_the_op_via_apply_patch(monkeypatch):
    op = _op(value={"name": "New Name"})
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1", "op": op,
                   "before": {"name": "Old"}, "correction_text": "x", "scope_label": "chat"}]
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)
    apply_patch_mock = MagicMock()
    monkeypatch.setattr(correction_candidates, "apply_patch", apply_patch_mock)

    correction_candidates.accept_candidate("ws_1", "corr_a")

    apply_patch_mock.assert_called_once()
    args, _kwargs = apply_patch_mock.call_args
    assert args[0] == "ws_1"
    [applied_op] = args[1]
    assert applied_op["value"]["name"] == "New Name"


def test_accept_candidate_tags_dict_valued_replace_ops_as_user_corrected(monkeypatch):
    op = _op(kind="replace", value={"name": "New Name"})
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1", "op": op,
                   "before": {}, "correction_text": "x", "scope_label": "chat"}]
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)
    apply_patch_mock = MagicMock()
    monkeypatch.setattr(correction_candidates, "apply_patch", apply_patch_mock)

    result = correction_candidates.accept_candidate("ws_1", "corr_a")

    assert result["op"]["value"]["user_corrected"] is True
    [applied_op] = apply_patch_mock.call_args.args[1]
    assert applied_op["value"]["user_corrected"] is True


def test_accept_candidate_tags_dict_valued_add_ops_as_user_corrected(monkeypatch):
    op = _op(kind="add", value={"name": "Brand New"})
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1", "op": op,
                   "before": None, "correction_text": "x", "scope_label": "chat"}]
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)
    monkeypatch.setattr(correction_candidates, "apply_patch", MagicMock())

    result = correction_candidates.accept_candidate("ws_1", "corr_a")

    assert result["op"]["value"]["user_corrected"] is True


def test_accept_candidate_does_not_tag_remove_ops(monkeypatch):
    op = {"op": "remove", "path": "/topics/t1"}
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1", "op": op,
                   "before": {"name": "Old"}, "correction_text": "x", "scope_label": "chat"}]
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)
    monkeypatch.setattr(correction_candidates, "apply_patch", MagicMock())

    result = correction_candidates.accept_candidate("ws_1", "corr_a")

    assert "user_corrected" not in result["op"]


def test_accept_candidate_does_not_tag_ops_whose_value_is_not_a_dict(monkeypatch):
    """e.g. a replace op targeting a scalar/list field -- tagging would
    corrupt the value shape rather than annotate it."""
    op = _op(kind="replace", value="a plain string, not a dict")
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1", "op": op,
                   "before": "old string", "correction_text": "x", "scope_label": "chat"}]
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)
    monkeypatch.setattr(correction_candidates, "apply_patch", MagicMock())

    result = correction_candidates.accept_candidate("ws_1", "corr_a")

    assert result["op"]["value"] == "a plain string, not a dict"


def test_accept_candidate_never_mutates_the_original_ops_value_dict(monkeypatch):
    """The tagging must build a new value dict, not mutate the
    candidate's stored op in place -- a failed apply_patch() (tested
    separately) needs the untouched original still sitting in the
    pending list afterward."""
    original_value = {"name": "New Name"}
    op = _op(value=original_value)
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1", "op": op,
                   "before": {}, "correction_text": "x", "scope_label": "chat"}]
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)
    monkeypatch.setattr(correction_candidates, "apply_patch", MagicMock())

    correction_candidates.accept_candidate("ws_1", "corr_a")

    assert "user_corrected" not in original_value


def test_accept_candidate_removes_the_candidate_from_the_pending_list_after_apply(monkeypatch):
    candidates = [
        {"candidate_id": "corr_a", "topic_id": "t1", "op": _op(), "before": {},
         "correction_text": "x", "scope_label": "chat"},
        {"candidate_id": "corr_b", "topic_id": "t2", "op": _op(), "before": {},
         "correction_text": "y", "scope_label": "chat"},
    ]
    seen = {}
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write",
                         lambda key, value: seen.update({"value": value}))
    monkeypatch.setattr(correction_candidates, "apply_patch", MagicMock())

    correction_candidates.accept_candidate("ws_1", "corr_a")

    assert [c["candidate_id"] for c in seen["value"]] == ["corr_b"]


def test_accept_candidate_addresses_by_id_not_list_position(monkeypatch):
    """Same sibling-module regression pin as
    test_eo_note_candidates.py's accept_candidate test: accepting
    corr_a must remove corr_a regardless of its position in the
    stored list."""
    candidates = [
        {"candidate_id": "corr_b", "topic_id": "t2", "op": _op(), "before": {},
         "correction_text": "y", "scope_label": "chat"},
        {"candidate_id": "corr_a", "topic_id": "t1", "op": _op(), "before": {},
         "correction_text": "x", "scope_label": "chat"},
    ]
    seen = {}
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write",
                         lambda key, value: seen.update({"value": value}))
    monkeypatch.setattr(correction_candidates, "apply_patch", MagicMock())

    correction_candidates.accept_candidate("ws_1", "corr_a")

    assert [c["candidate_id"] for c in seen["value"]] == ["corr_b"]


def test_accept_candidate_leaves_the_pending_list_untouched_when_apply_patch_fails(monkeypatch):
    """The core ordering guarantee this module's docstring promises:
    a failed apply_patch() (e.g. the topic vanished) must not pop the
    candidate -- write() must never even be called in that case."""
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1", "op": _op(), "before": {},
                   "correction_text": "x", "scope_label": "chat"}]
    write_mock = MagicMock()
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", write_mock)

    def failing_apply_patch(workspace_id, ops):
        raise ValueError("topic no longer exists")

    monkeypatch.setattr(correction_candidates, "apply_patch", failing_apply_patch)

    with pytest.raises(ValueError):
        correction_candidates.accept_candidate("ws_1", "corr_a")

    write_mock.assert_not_called()


def test_accept_candidate_returns_the_candidate_with_the_tagged_op(monkeypatch):
    op = _op(value={"name": "New Name"})
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1", "op": op,
                   "before": {"name": "Old"}, "correction_text": "fix name", "scope_label": "chat"}]
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)
    monkeypatch.setattr(correction_candidates, "apply_patch", MagicMock())

    result = correction_candidates.accept_candidate("ws_1", "corr_a")

    assert result["candidate_id"] == "corr_a"
    assert result["topic_id"] == "t1"
    assert result["before"] == {"name": "Old"}
    assert result["correction_text"] == "fix name"
    assert result["op"]["value"]["user_corrected"] is True


# ---------------------------------------------------------------------
# reject_candidate
# ---------------------------------------------------------------------

def test_reject_candidate_raises_file_not_found_for_unknown_id(monkeypatch):
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: [])
    with pytest.raises(FileNotFoundError):
        correction_candidates.reject_candidate("ws_1", "corr_missing")


def test_reject_candidate_removes_only_the_matching_candidate(monkeypatch):
    candidates = [
        {"candidate_id": "corr_a", "topic_id": "t1"},
        {"candidate_id": "corr_b", "topic_id": "t2"},
    ]
    seen = {}
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write",
                         lambda key, value: seen.update({"value": value}))

    correction_candidates.reject_candidate("ws_1", "corr_a")

    assert [c["candidate_id"] for c in seen["value"]] == ["corr_b"]


def test_reject_candidate_never_calls_apply_patch(monkeypatch):
    """Rejecting a correction must never touch the real Secondary Data
    document -- the whole point of the propose/accept/reject split."""
    candidates = [{"candidate_id": "corr_a", "topic_id": "t1"}]
    apply_patch_mock = MagicMock()
    monkeypatch.setattr(correction_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(correction_candidates, "write", lambda key, value: None)
    monkeypatch.setattr(correction_candidates, "apply_patch", apply_patch_mock)

    correction_candidates.reject_candidate("ws_1", "corr_a")

    apply_patch_mock.assert_not_called()
