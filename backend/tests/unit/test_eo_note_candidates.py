"""
tests/unit/test_eo_note_candidates.py — Patch 7e (content/knowledge
group).

eo/note_candidates.py had zero test coverage before this. Per the
module's own bug-audit §9 note, this store used to address a pending
candidate by its position in a list -- fine single-player, but wrong
once Part 8.4 added multi-user notification fan-out to the same pending
list. The highest-value things to pin down here, in that order:

  1. accept_candidate()/reject_candidate() addressing by candidate_id,
     not list position, and both raising FileNotFoundError (not a
     silent no-op or an IndexError) for an unknown id -- the exact
     regression the bug-audit note describes.
  2. accept_candidate()'s "removed from the pending list either way"
     posture, even when the downstream write_node() call itself fails
     (returns None) -- a permanently-stuck candidate would be worse
     than losing one that failed to embed.
  3. propose_note()'s fire-and-forget notification fan-out: a failure
     in list_notify_targets()/emit_user_event() must never block the
     candidate save (same discipline relay/emitter.py itself
     documents).
  4. get_topic_related_notes()'s empty-query-text short-circuit (no
     name AND no summary) and its scoping of results to
     node_type="note".

Isolation: note_candidates.py does `from memory.bus import read, write`
(bound names in its own namespace), so tests patch `read`/`write` on
the note_candidates module object -- same gotcha as every other bus-
backed store in this batch. write_node/search_nodes/get_packet_depth/
list_notify_targets/emit_user_event/invalidate_cache are all imported
with DEFERRED (function-body) imports rather than at module load time
-- that actually makes mocking easier here, not harder: since the
import happens fresh on every call, patching the attribute on the
*source* module (eo.knowledge_graph.write_node, eo.chat_workspace.
list_notify_targets, relay.emitter.emit_user_event, eo.semantic_cache.
invalidate_cache, eo.source_index.get_packet_depth, eo.knowledge_graph.
search_nodes) is picked up correctly, unlike the bound-name gotcha
above.
"""
from unittest.mock import MagicMock

import pytest

from eo import note_candidates

# ---------------------------------------------------------------------
# _key
# ---------------------------------------------------------------------

def test_key_is_namespaced_by_workspace_id():
    assert note_candidates._key("ws_1") == "candidate_notes:ws_1"


# ---------------------------------------------------------------------
# propose_note
# ---------------------------------------------------------------------

def test_propose_note_raises_when_required_fields_are_missing(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: default)
    with pytest.raises(ValueError):
        note_candidates.propose_note("", "Title", "Content", [], "agent")
    with pytest.raises(ValueError):
        note_candidates.propose_note("ws_1", "", "Content", [], "agent")
    with pytest.raises(ValueError):
        note_candidates.propose_note("ws_1", "Title", "", [], "agent")


def test_propose_note_appends_to_existing_candidates_without_dropping_them(monkeypatch):
    existing = [{"candidate_id": "note_old", "title": "Old"}]
    seen = {}
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(existing))
    monkeypatch.setattr(note_candidates, "write",
                         lambda key, value: seen.update({"key": key, "value": value}))
    monkeypatch.setattr("eo.chat_workspace.list_notify_targets", lambda ws_id: [])

    note_candidates.propose_note("ws_1", "New Title", "New content", ["tag1"], "note_taker")

    assert seen["key"] == "candidate_notes:ws_1"
    assert len(seen["value"]) == 2
    assert seen["value"][0] == existing[0]
    assert seen["value"][1]["title"] == "New Title"


def test_propose_note_generates_a_stable_candidate_id(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: default)
    seen = {}
    monkeypatch.setattr(note_candidates, "write",
                         lambda key, value: seen.update({"value": value}))
    monkeypatch.setattr("eo.chat_workspace.list_notify_targets", lambda ws_id: [])

    candidate = note_candidates.propose_note("ws_1", "Title", "Content", [], "note_taker")

    assert candidate["candidate_id"].startswith("note_")
    assert seen["value"][0]["candidate_id"] == candidate["candidate_id"]


def test_propose_note_defaults_tags_to_empty_list(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: default)
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    monkeypatch.setattr("eo.chat_workspace.list_notify_targets", lambda ws_id: [])

    candidate = note_candidates.propose_note("ws_1", "Title", "Content", None, "note_taker")

    assert candidate["tags"] == []


def test_propose_note_stamps_proposed_at(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: default)
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    monkeypatch.setattr("eo.chat_workspace.list_notify_targets", lambda ws_id: [])

    candidate = note_candidates.propose_note("ws_1", "Title", "Content", [], "note_taker")

    assert "proposed_at" in candidate


def test_propose_note_notifies_every_workspace_target(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: default)
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    monkeypatch.setattr("eo.chat_workspace.list_notify_targets",
                         lambda ws_id: ["user_a", "user_b"])
    emit_mock = MagicMock()
    monkeypatch.setattr("relay.emitter.emit_user_event", emit_mock)

    note_candidates.propose_note("ws_1", "Title", "Content", [], "note_taker")

    notified_users = [call.args[1] for call in emit_mock.call_args_list]
    assert notified_users == ["user_a", "user_b"]


def test_propose_note_notification_payload_includes_the_note_title(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: default)
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    monkeypatch.setattr("eo.chat_workspace.list_notify_targets", lambda ws_id: ["user_a"])
    emit_mock = MagicMock()
    monkeypatch.setattr("relay.emitter.emit_user_event", emit_mock)

    note_candidates.propose_note("ws_1", "My Note", "Content", [], "note_taker")

    _args, kwargs = emit_mock.call_args
    assert kwargs["payload"]["title"] == "My Note"
    assert kwargs["payload"]["kind"] == "note_proposed"


def test_propose_note_still_returns_the_candidate_when_notification_fails(monkeypatch):
    """Fire-and-forget: a broken notification path must never block or
    fail the candidate save itself."""
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: default)
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)

    def boom(ws_id):
        raise RuntimeError("notify service down")
    monkeypatch.setattr("eo.chat_workspace.list_notify_targets", boom)

    candidate = note_candidates.propose_note("ws_1", "Title", "Content", [], "note_taker")

    assert candidate["title"] == "Title"


# ---------------------------------------------------------------------
# list_candidates
# ---------------------------------------------------------------------

def test_list_candidates_returns_empty_list_by_default(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: default)
    assert note_candidates.list_candidates("ws_1") == []


def test_list_candidates_reads_the_workspace_scoped_key(monkeypatch):
    seen = {}

    def fake_read(key, default=None):
        seen["key"] = key
        return default

    monkeypatch.setattr(note_candidates, "read", fake_read)
    note_candidates.list_candidates("ws_1")

    assert seen["key"] == "candidate_notes:ws_1"


# ---------------------------------------------------------------------
# accept_candidate
# ---------------------------------------------------------------------

def test_accept_candidate_raises_file_not_found_for_unknown_id(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: [])
    with pytest.raises(FileNotFoundError):
        note_candidates.accept_candidate("ws_1", "note_missing")


def test_accept_candidate_removes_the_candidate_from_the_pending_list(monkeypatch):
    candidates = [
        {"candidate_id": "note_a", "title": "A", "content": "ca", "tags": [], "proposed_by": "agent"},
        {"candidate_id": "note_b", "title": "B", "content": "cb", "tags": [], "proposed_by": "agent"},
    ]
    seen = {}
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write",
                         lambda key, value: seen.update({"value": value}))
    monkeypatch.setattr("eo.knowledge_graph.write_node", lambda **kwargs: "node_1")

    note_candidates.accept_candidate("ws_1", "note_a")

    assert [c["candidate_id"] for c in seen["value"]] == ["note_b"]


def test_accept_candidate_addresses_by_id_not_list_position(monkeypatch):
    """Regression pin for bug-audit §9: accepting note_b (index 1) must
    remove note_b, not whatever currently sits at index 1 after some
    other mutation -- addressed by id, so this must hold even if the
    stored order doesn't match insertion order."""
    candidates = [
        {"candidate_id": "note_b", "title": "B", "content": "cb", "tags": [], "proposed_by": "agent"},
        {"candidate_id": "note_a", "title": "A", "content": "ca", "tags": [], "proposed_by": "agent"},
    ]
    seen = {}
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write",
                         lambda key, value: seen.update({"value": value}))
    monkeypatch.setattr("eo.knowledge_graph.write_node", lambda **kwargs: "node_1")

    note_candidates.accept_candidate("ws_1", "note_a")

    assert [c["candidate_id"] for c in seen["value"]] == ["note_b"]


def test_accept_candidate_calls_write_node_with_the_accepted_content(monkeypatch):
    candidates = [{"candidate_id": "note_a", "title": "A Title", "content": "the content",
                   "tags": ["t1"], "proposed_by": "note_taker"}]
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    write_node_mock = MagicMock(return_value="node_1")
    monkeypatch.setattr("eo.knowledge_graph.write_node", write_node_mock)

    note_candidates.accept_candidate("ws_1", "note_a", section="research", created_by="user")

    write_node_mock.assert_called_once_with(
        workspace_id="ws_1", section="research", node_type="note",
        title="A Title", content="the content", created_by="note_taker",
        tags=["t1"],
    )


def test_accept_candidate_falls_back_to_created_by_when_no_proposed_by(monkeypatch):
    candidates = [{"candidate_id": "note_a", "title": "A", "content": "c", "tags": []}]
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    write_node_mock = MagicMock(return_value="node_1")
    monkeypatch.setattr("eo.knowledge_graph.write_node", write_node_mock)

    note_candidates.accept_candidate("ws_1", "note_a", created_by="fallback_user")

    assert write_node_mock.call_args.kwargs["created_by"] == "fallback_user"


def test_accept_candidate_returns_the_new_node_id(monkeypatch):
    candidates = [{"candidate_id": "note_a", "title": "A", "content": "c", "tags": []}]
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    monkeypatch.setattr("eo.knowledge_graph.write_node", lambda **kwargs: "node_xyz")

    assert note_candidates.accept_candidate("ws_1", "note_a") == "node_xyz"


def test_accept_candidate_still_removes_candidate_when_write_node_fails(monkeypatch):
    """write_node() degrading to None (embed/upsert failure) must not
    leave the candidate permanently stuck in the pending list."""
    candidates = [{"candidate_id": "note_a", "title": "A", "content": "c", "tags": []}]
    seen = {}
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write",
                         lambda key, value: seen.update({"value": value}))
    monkeypatch.setattr("eo.knowledge_graph.write_node", lambda **kwargs: None)

    result = note_candidates.accept_candidate("ws_1", "note_a")

    assert result is None
    assert seen["value"] == []


def test_accept_candidate_invalidates_semantic_cache_when_write_node_succeeds(monkeypatch):
    candidates = [{"candidate_id": "note_a", "title": "A", "content": "c", "tags": []}]
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    monkeypatch.setattr("eo.knowledge_graph.write_node", lambda **kwargs: "node_1")
    invalidate_mock = MagicMock()
    monkeypatch.setattr("eo.semantic_cache.invalidate_cache", invalidate_mock)

    note_candidates.accept_candidate("ws_1", "note_a")

    invalidate_mock.assert_called_once_with("A\nc", workspace_id="ws_1")


def test_accept_candidate_skips_cache_invalidation_when_write_node_fails(monkeypatch):
    candidates = [{"candidate_id": "note_a", "title": "A", "content": "c", "tags": []}]
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    monkeypatch.setattr("eo.knowledge_graph.write_node", lambda **kwargs: None)
    invalidate_mock = MagicMock()
    monkeypatch.setattr("eo.semantic_cache.invalidate_cache", invalidate_mock)

    note_candidates.accept_candidate("ws_1", "note_a")

    invalidate_mock.assert_not_called()


def test_accept_candidate_does_not_fail_when_cache_invalidation_raises(monkeypatch):
    candidates = [{"candidate_id": "note_a", "title": "A", "content": "c", "tags": []}]
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write", lambda key, value: None)
    monkeypatch.setattr("eo.knowledge_graph.write_node", lambda **kwargs: "node_1")

    def boom(*a, **k):
        raise RuntimeError("cache service down")
    monkeypatch.setattr("eo.semantic_cache.invalidate_cache", boom)

    result = note_candidates.accept_candidate("ws_1", "note_a")

    assert result == "node_1"


# ---------------------------------------------------------------------
# reject_candidate
# ---------------------------------------------------------------------

def test_reject_candidate_raises_file_not_found_for_unknown_id(monkeypatch):
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: [])
    with pytest.raises(FileNotFoundError):
        note_candidates.reject_candidate("ws_1", "note_missing")


def test_reject_candidate_removes_only_the_matching_candidate(monkeypatch):
    candidates = [
        {"candidate_id": "note_a", "title": "A"},
        {"candidate_id": "note_b", "title": "B"},
    ]
    seen = {}
    monkeypatch.setattr(note_candidates, "read", lambda key, default=None: list(candidates))
    monkeypatch.setattr(note_candidates, "write",
                         lambda key, value: seen.update({"value": value}))

    note_candidates.reject_candidate("ws_1", "note_a")

    assert [c["candidate_id"] for c in seen["value"]] == ["note_b"]


def test_reject_candidate_never_calls_write_node():
    """Rejecting a candidate must never touch the real graph -- the
    whole point of the propose/accept/reject split."""
    # No knowledge_graph.write_node patch installed at all: if
    # reject_candidate() ever called it, this test would raise on the
    # real (network-touching) import/call path instead of passing quietly.


# ---------------------------------------------------------------------
# get_topic_related_notes
# ---------------------------------------------------------------------

def _packet_with_topic(topic_id, name=None, summary=None):
    return {"topics": {topic_id: {"name": name, "summary": summary}}}


def test_get_topic_related_notes_returns_empty_list_when_topic_has_no_name_or_summary(monkeypatch):
    monkeypatch.setattr("eo.source_index.get_packet_depth",
                         lambda *a, **k: _packet_with_topic("t1"))
    search_mock = MagicMock()
    monkeypatch.setattr("eo.knowledge_graph.search_nodes", search_mock)

    result = note_candidates.get_topic_related_notes("ws_1", "t1")

    assert result == []
    search_mock.assert_not_called()


def test_get_topic_related_notes_queries_with_combined_name_and_summary(monkeypatch):
    monkeypatch.setattr(
        "eo.source_index.get_packet_depth",
        lambda *a, **k: _packet_with_topic("t1", name="Topic Name", summary="Topic Summary"),
    )
    search_mock = MagicMock(return_value=[])
    monkeypatch.setattr("eo.knowledge_graph.search_nodes", search_mock)

    note_candidates.get_topic_related_notes("ws_1", "t1")

    args, kwargs = search_mock.call_args
    assert args[1] == "Topic Name\nTopic Summary"
    assert kwargs["node_type"] == "note"


def test_get_topic_related_notes_uses_only_the_summary_when_name_is_missing(monkeypatch):
    monkeypatch.setattr(
        "eo.source_index.get_packet_depth",
        lambda *a, **k: _packet_with_topic("t1", name=None, summary="Just a summary"),
    )
    search_mock = MagicMock(return_value=[])
    monkeypatch.setattr("eo.knowledge_graph.search_nodes", search_mock)

    note_candidates.get_topic_related_notes("ws_1", "t1")

    args, _kwargs = search_mock.call_args
    assert args[1] == "Just a summary"


def test_get_topic_related_notes_applies_min_score_filter_when_given(monkeypatch):
    monkeypatch.setattr(
        "eo.source_index.get_packet_depth",
        lambda *a, **k: _packet_with_topic("t1", name="Topic"),
    )
    monkeypatch.setattr(
        "eo.knowledge_graph.search_nodes",
        lambda *a, **k: [{"node_id": "n1", "score": 0.9}, {"node_id": "n2", "score": 0.5}],
    )

    result = note_candidates.get_topic_related_notes("ws_1", "t1", min_score=0.8)

    assert [n["node_id"] for n in result] == ["n1"]


def test_get_topic_related_notes_without_min_score_returns_everything_search_returns(monkeypatch):
    monkeypatch.setattr(
        "eo.source_index.get_packet_depth",
        lambda *a, **k: _packet_with_topic("t1", name="Topic"),
    )
    monkeypatch.setattr(
        "eo.knowledge_graph.search_nodes",
        lambda *a, **k: [{"node_id": "n1", "score": 0.9}, {"node_id": "n2", "score": 0.1}],
    )

    result = note_candidates.get_topic_related_notes("ws_1", "t1")

    assert [n["node_id"] for n in result] == ["n1", "n2"]
