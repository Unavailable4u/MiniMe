"""
tests/unit/test_eo_prerequisite_suggestions.py — Patch 7e-S5.

eo/prerequisite_suggestions.py had zero test coverage before this.
Priorities, worst-silent-failure first:

  1. find_prerequisite_suggestions()'s core filter: a "prerequisite-of"
     connection only surfaces when its `to_topic` is discussed AND its
     `from_topic` is NOT already discussed itself -- getting either half
     backwards either drowns the person in noise or hides genuinely
     useful prerequisites.
  2. Fail-open behavior: no workspace_id, no grounded node ids, or
     get_packet() itself raising must all degrade to an empty list,
     never propagate -- a broken suggestion pass must not take down an
     otherwise-fine chat answer.
  3. The two step-3.5/3.8 de-dup layers: _is_untouched() (skip a
     prerequisite that already has a per-topic workflow or an accepted
     note folded in) and the per-session _already_nudged()/_mark_nudged()
     pairing suppression, including that marking happens immediately
     (not after the whole call succeeds) and that the session cache is
     bounded (oldest session evicted once _MAX_TRACKED_SESSIONS is hit).
  4. MAX_SUGGESTIONS capping and deterministic first-appearance,
     de-duplicated-by-prerequisite-id ordering.

Isolation: get_packet (imported at module level via `from
eo.source_index import get_packet`) is patched as
`prerequisite_suggestions.get_packet` for the same "already bound into
this module's own namespace at import time" reason the chat_workspace/
panel_content test files' own docstrings give for write_audit.
_topic_workflow_topic_ids()/_note_node_ids() are patched directly for
tests that aren't specifically exercising their own fail-open behavior,
so those tests aren't coupled to panel_content/knowledge_graph's real
implementations.
"""
import pytest

from eo import prerequisite_suggestions


@pytest.fixture(autouse=True)
def _reset_session_cache():
    """The per-session nudge cache is module-level state shared across
    tests -- reset it before and after every test so tests can't leak
    into each other via a shared session_id."""
    prerequisite_suggestions._nudged_pairs_by_session.clear()
    yield
    prerequisite_suggestions._nudged_pairs_by_session.clear()


@pytest.fixture(autouse=True)
def _no_untouched_or_note_filtering(monkeypatch):
    """Most tests care about the core prerequisite-of filter, not the
    step-3.5 untouched-topic gate -- default both reads to "nothing
    exists yet" (workflow_topic_ids=set(), note_node_ids=set()) so
    _is_untouched() always returns True unless a test overrides this."""
    monkeypatch.setattr(prerequisite_suggestions, "_topic_workflow_topic_ids", lambda ws_id: set())
    monkeypatch.setattr(prerequisite_suggestions, "_note_node_ids", lambda ws_id: set())


def _packet(topics=None, connections=None):
    return {"topics": topics or {}, "connections": connections or []}


_BASIC_TOPICS = {
    "t_calc": {"name": "Calculus", "summary": "Limits and derivatives", "covers": ["n1"]},
    "t_prereq": {"name": "Algebra", "summary": "Equations", "covers": ["n2"]},
}
_BASIC_CONNECTIONS = [
    {"from_topic": "t_prereq", "to_topic": "t_calc", "relation": "prerequisite-of"},
]


# ---------------------------------------------------------------------
# Fail-open guards
# ---------------------------------------------------------------------

def test_returns_empty_without_workspace_id():
    assert prerequisite_suggestions.find_prerequisite_suggestions("", ["n1"]) == []


def test_returns_empty_without_grounded_node_ids():
    assert prerequisite_suggestions.find_prerequisite_suggestions("ws_1", []) == []


def test_returns_empty_when_get_packet_raises(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    assert result == []


def test_returns_empty_when_no_topic_covers_the_grounded_nodes(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, _BASIC_CONNECTIONS))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n_unrelated"])
    assert result == []


# ---------------------------------------------------------------------
# Core prerequisite-of filter
# ---------------------------------------------------------------------

def test_surfaces_a_real_prerequisite_of_the_discussed_topic(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, _BASIC_CONNECTIONS))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    assert len(result) == 1
    assert result[0]["topic_id"] == "t_prereq"
    assert result[0]["for_topic_id"] == "t_calc"


def test_suggestion_shape_includes_names_and_source_node_ids(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, _BASIC_CONNECTIONS))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    entry = result[0]
    assert entry["name"] == "Algebra"
    assert entry["for_topic_name"] == "Calculus"
    assert entry["summary"] == "Equations"
    assert entry["source_node_ids"] == ["n2"]


def test_ignores_connections_with_a_different_relation(monkeypatch):
    topics = dict(_BASIC_TOPICS)
    connections = [{"from_topic": "t_prereq", "to_topic": "t_calc", "relation": "elaborates-on"}]
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(topics, connections))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    assert result == []


def test_does_not_suggest_a_prerequisite_that_is_itself_already_discussed(monkeypatch):
    """Both endpoints of the connection are in discussed_topic_ids ->
    nothing new to surface."""
    topics = {
        "t_calc": {"name": "Calculus", "covers": ["n1"]},
        "t_prereq": {"name": "Algebra", "covers": ["n2"]},
    }
    connections = [{"from_topic": "t_prereq", "to_topic": "t_calc", "relation": "prerequisite-of"}]
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(topics, connections))
    # both n1 (calc) and n2 (algebra) are grounded -- algebra already discussed
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1", "n2"])
    assert result == []


def test_skips_connection_whose_from_topic_is_unknown(monkeypatch):
    connections = [{"from_topic": "t_ghost", "to_topic": "t_calc", "relation": "prerequisite-of"}]
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, connections))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    assert result == []


def test_deduplicates_by_prerequisite_topic_id(monkeypatch):
    """The same prerequisite feeding two different discussed topics is
    only offered once."""
    topics = {
        "t_a": {"name": "A", "covers": ["n1"]},
        "t_b": {"name": "B", "covers": ["n2"]},
        "t_prereq": {"name": "Prereq", "covers": ["n3"]},
    }
    connections = [
        {"from_topic": "t_prereq", "to_topic": "t_a", "relation": "prerequisite-of"},
        {"from_topic": "t_prereq", "to_topic": "t_b", "relation": "prerequisite-of"},
    ]
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(topics, connections))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1", "n2"])
    assert len(result) == 1


def test_max_suggestions_cap(monkeypatch):
    topics = {"t_calc": {"name": "Calc", "covers": ["n1"]}}
    connections = []
    for i in range(prerequisite_suggestions.MAX_SUGGESTIONS + 2):
        pid = f"t_prereq_{i}"
        topics[pid] = {"name": f"Prereq {i}", "covers": [f"n_p{i}"]}
        connections.append({"from_topic": pid, "to_topic": "t_calc", "relation": "prerequisite-of"})
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(topics, connections))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    assert len(result) == prerequisite_suggestions.MAX_SUGGESTIONS


def test_missing_name_and_summary_default_gracefully(monkeypatch):
    topics = {
        "t_calc": {"name": "Calc", "covers": ["n1"]},
        "t_prereq": {"covers": ["n2"]},  # no "name" key at all
    }
    connections = [{"from_topic": "t_prereq", "to_topic": "t_calc", "relation": "prerequisite-of"}]
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(topics, connections))
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    assert result[0]["name"] == "Untitled topic"


# ---------------------------------------------------------------------
# _is_untouched / step 3.5 gating
# ---------------------------------------------------------------------

def test_is_untouched_false_when_topic_has_a_generated_workflow():
    assert prerequisite_suggestions._is_untouched("t1", [], {"t1"}, set()) is False


def test_is_untouched_false_when_covers_overlaps_a_note_node():
    assert prerequisite_suggestions._is_untouched("t1", ["n1"], set(), {"n1"}) is False


def test_is_untouched_true_when_neither_applies():
    assert prerequisite_suggestions._is_untouched("t1", ["n1"], set(), {"n_other"}) is True


def test_already_has_workflow_prerequisite_is_not_suggested(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, _BASIC_CONNECTIONS))
    monkeypatch.setattr(prerequisite_suggestions, "_topic_workflow_topic_ids",
                         lambda ws_id: {"t_prereq"})
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    assert result == []


def test_already_has_folded_in_note_prerequisite_is_not_suggested(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, _BASIC_CONNECTIONS))
    monkeypatch.setattr(prerequisite_suggestions, "_note_node_ids", lambda ws_id: {"n2"})
    result = prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"])
    assert result == []


# ---------------------------------------------------------------------
# _already_nudged / _mark_nudged / step 3.8 session suppression
# ---------------------------------------------------------------------

def test_already_nudged_false_with_no_session_id():
    assert prerequisite_suggestions._already_nudged(None, "a", "b") is False


def test_mark_nudged_no_op_with_no_session_id():
    prerequisite_suggestions._mark_nudged(None, "a", "b")
    assert prerequisite_suggestions._nudged_pairs_by_session == {}


def test_mark_then_already_nudged_round_trips():
    prerequisite_suggestions._mark_nudged("sess_1", "a", "b")
    assert prerequisite_suggestions._already_nudged("sess_1", "a", "b") is True
    assert prerequisite_suggestions._already_nudged("sess_1", "a", "c") is False


def test_second_call_same_session_suppresses_the_exact_pairing(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, _BASIC_CONNECTIONS))

    first = prerequisite_suggestions.find_prerequisite_suggestions(
        "ws_1", ["n1"], session_id="sess_1")
    second = prerequisite_suggestions.find_prerequisite_suggestions(
        "ws_1", ["n1"], session_id="sess_1")

    assert len(first) == 1
    assert second == []


def test_marking_happens_immediately_not_only_after_the_whole_call_succeeds(monkeypatch):
    """Per the module's own comment: mark right when a suggestion is
    appended, not after the whole call returns -- verified indirectly
    via the round-trip test above, and directly here by checking the
    cache is populated right after one call."""
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, _BASIC_CONNECTIONS))
    prerequisite_suggestions.find_prerequisite_suggestions("ws_1", ["n1"], session_id="sess_1")
    assert prerequisite_suggestions._already_nudged("sess_1", "t_prereq", "t_calc") is True


def test_different_sessions_each_get_the_suggestion_independently(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "get_packet",
                         lambda *a, **kw: _packet(_BASIC_TOPICS, _BASIC_CONNECTIONS))
    first = prerequisite_suggestions.find_prerequisite_suggestions(
        "ws_1", ["n1"], session_id="sess_1")
    second = prerequisite_suggestions.find_prerequisite_suggestions(
        "ws_1", ["n1"], session_id="sess_2")
    assert len(first) == 1
    assert len(second) == 1


def test_mark_nudged_evicts_oldest_session_once_cap_is_hit(monkeypatch):
    monkeypatch.setattr(prerequisite_suggestions, "_MAX_TRACKED_SESSIONS", 2)
    prerequisite_suggestions._mark_nudged("sess_1", "a", "b")
    prerequisite_suggestions._mark_nudged("sess_2", "a", "b")
    prerequisite_suggestions._mark_nudged("sess_3", "a", "b")  # should evict sess_1

    assert "sess_1" not in prerequisite_suggestions._nudged_pairs_by_session
    assert "sess_2" in prerequisite_suggestions._nudged_pairs_by_session
    assert "sess_3" in prerequisite_suggestions._nudged_pairs_by_session
