"""
tests/unit/test_eo_source_index.py — Patch 7e (content/knowledge group).

eo/source_index.py had zero test coverage before this. It's Mode C's
one serving path (get_packet / get_packet_depth / get_topic_covered_
sources) -- no LLM call anywhere in the module, so a bug here is a
pure-logic bug in the topic-skeleton trim, the parent-tree walk, or the
covers-edge derivation, not a flaky-provider issue. Worth pinning down,
in order of blast radius:

  1. _topic_skeleton()'s field trim + `covers` derivation from
     `source_section_ids`, and its `user_corrected` pass-through-only-
     when-true behavior (the module's own "no padding on the common
     case" comment).
  2. get_packet_depth()'s depth-adaptive walk: depth 0 is just the
     starting topic, the walk stops the moment a level comes back
     empty (not necessarily at requested_depth), and `exhausted` is
     the caller-facing signal for "the tree ran dry early."
  3. get_packet()/get_packet_depth()'s validation (missing
     workspace_id, bad scope, negative depth, unknown starting topic).

Isolation: source_index.py does `from eo.secondary_data import
get_secondary_data_scoped, SCOPES` (a bound name in its own
namespace), so tests patch `get_secondary_data_scoped` on the
source_index module object, not on eo.secondary_data -- same bound-name
gotcha as every other module in this batch. `SCOPES` itself is left
un-patched and used for real, since it's a plain constant.
"""
import pytest

from eo import source_index


def _topic(name=None, summary=None, parent=None, content_hint=None,
           source_section_ids=None, user_corrected=None):
    topic = {"name": name, "summary": summary, "parent": parent, "content_hint": content_hint}
    if source_section_ids is not None:
        topic["source_section_ids"] = source_section_ids
    if user_corrected is not None:
        topic["user_corrected"] = user_corrected
    return topic


def _doc(topics, connections=None):
    return {"topics": topics, "connections": connections or []}


# ---------------------------------------------------------------------
# _topic_skeleton
# ---------------------------------------------------------------------

def test_topic_skeleton_keeps_only_the_skeleton_fields_plus_covers():
    topics = {"t1": _topic(name="Topic 1", summary="S", parent=None,
                            content_hint="text", source_section_ids=["n1", "n2"])}

    result = source_index._topic_skeleton(topics)

    assert result["t1"] == {
        "name": "Topic 1", "summary": "S", "parent": None, "content_hint": "text",
        "covers": ["n1", "n2"],
    }


def test_topic_skeleton_defaults_covers_to_empty_list_when_no_source_section_ids():
    topics = {"t1": _topic(name="Topic 1")}
    result = source_index._topic_skeleton(topics)
    assert result["t1"]["covers"] == []


def test_topic_skeleton_missing_fields_become_none_not_a_key_error():
    topics = {"t1": {}}  # no name/summary/parent/content_hint at all
    result = source_index._topic_skeleton(topics)
    assert result["t1"]["name"] is None
    assert result["t1"]["summary"] is None
    assert result["t1"]["parent"] is None
    assert result["t1"]["content_hint"] is None


def test_topic_skeleton_omits_user_corrected_when_falsy():
    topics = {"t1": _topic(name="Topic 1", user_corrected=False)}
    result = source_index._topic_skeleton(topics)
    assert "user_corrected" not in result["t1"]


def test_topic_skeleton_includes_user_corrected_only_when_true():
    topics = {"t1": _topic(name="Topic 1", user_corrected=True)}
    result = source_index._topic_skeleton(topics)
    assert result["t1"]["user_corrected"] is True


def test_topic_skeleton_processes_every_topic_independently():
    topics = {
        "t1": _topic(name="One"),
        "t2": _topic(name="Two", source_section_ids=["n1"]),
    }
    result = source_index._topic_skeleton(topics)
    assert set(result.keys()) == {"t1", "t2"}
    assert result["t2"]["covers"] == ["n1"]


# ---------------------------------------------------------------------
# _children_index
# ---------------------------------------------------------------------

def test_children_index_groups_by_parent():
    topics = {
        "root": _topic(parent=None),
        "c1": _topic(parent="root"),
        "c2": _topic(parent="root"),
    }
    result = source_index._children_index(topics)
    assert set(result["root"]) == {"c1", "c2"}


def test_children_index_skips_topics_with_no_parent():
    topics = {"root": _topic(parent=None)}
    result = source_index._children_index(topics)
    assert result == {}


def test_children_index_handles_multi_level_trees():
    topics = {
        "root": _topic(parent=None),
        "c1": _topic(parent="root"),
        "gc1": _topic(parent="c1"),
    }
    result = source_index._children_index(topics)
    assert result["root"] == ["c1"]
    assert result["c1"] == ["gc1"]


# ---------------------------------------------------------------------
# get_packet
# ---------------------------------------------------------------------

def test_get_packet_raises_when_workspace_id_missing(monkeypatch):
    with pytest.raises(ValueError):
        source_index.get_packet("", scope="project")


def test_get_packet_raises_on_unknown_scope(monkeypatch):
    with pytest.raises(ValueError):
        source_index.get_packet("ws_1", scope="bogus")


def test_get_packet_returns_workspace_id_and_scope_passthrough(monkeypatch):
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _doc({}))

    result = source_index.get_packet("ws_1", scope="chat", session_id="sess-1")

    assert result["workspace_id"] == "ws_1"
    assert result["scope"] == "chat"


def test_get_packet_applies_topic_skeleton_and_passes_connections_through(monkeypatch):
    topics = {"t1": _topic(name="Topic 1", source_section_ids=["n1"])}
    connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "supports"}]
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _doc(topics, connections))

    result = source_index.get_packet("ws_1")

    assert result["topics"]["t1"]["covers"] == ["n1"]
    assert result["connections"] == connections


def test_get_packet_passes_session_id_through_to_get_secondary_data_scoped(monkeypatch):
    seen = {}

    def fake_get_secondary_data_scoped(workspace_id, scope, session_id=None):
        seen["session_id"] = session_id
        return _doc({})

    monkeypatch.setattr(source_index, "get_secondary_data_scoped", fake_get_secondary_data_scoped)

    source_index.get_packet("ws_1", scope="chat", session_id="sess-42")

    assert seen["session_id"] == "sess-42"


# ---------------------------------------------------------------------
# get_packet_depth
# ---------------------------------------------------------------------

def _linear_tree_doc():
    """root -> c1 -> gc1 (a simple 3-level chain)."""
    topics = {
        "root": _topic(name="Root", parent=None),
        "c1": _topic(name="Child", parent="root"),
        "gc1": _topic(name="Grandchild", parent="c1"),
    }
    connections = [
        {"from_topic": "root", "to_topic": "c1", "relation": "related"},
        {"from_topic": "c1", "to_topic": "gc1", "relation": "related"},
        {"from_topic": "root", "to_topic": "other_unrelated", "relation": "related"},
    ]
    return _doc(topics, connections)


def test_get_packet_depth_raises_on_negative_depth():
    with pytest.raises(ValueError):
        source_index.get_packet_depth("ws_1", "root", -1)


def test_get_packet_depth_raises_key_error_for_unknown_starting_topic(monkeypatch):
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _linear_tree_doc())
    with pytest.raises(KeyError):
        source_index.get_packet_depth("ws_1", "nonexistent", 1)


def test_get_packet_depth_zero_returns_only_the_starting_topic(monkeypatch):
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _linear_tree_doc())

    result = source_index.get_packet_depth("ws_1", "root", 0)

    assert set(result["topics"].keys()) == {"root"}
    assert result["reached_depth"] == 0
    assert result["exhausted"] is False


def test_get_packet_depth_one_includes_direct_children_only(monkeypatch):
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _linear_tree_doc())

    result = source_index.get_packet_depth("ws_1", "root", 1)

    assert set(result["topics"].keys()) == {"root", "c1"}
    assert result["reached_depth"] == 1
    assert result["exhausted"] is False


def test_get_packet_depth_stops_early_when_the_tree_runs_out_and_flags_exhausted(monkeypatch):
    """Requesting depth 5 on a tree that's only 2 levels deep from the
    starting topic must stop at reached_depth=2 and report exhausted,
    not raise or pad with nothing."""
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _linear_tree_doc())

    result = source_index.get_packet_depth("ws_1", "root", 5)

    assert set(result["topics"].keys()) == {"root", "c1", "gc1"}
    assert result["reached_depth"] == 2
    assert result["exhausted"] is True


def test_get_packet_depth_filters_connections_to_endpoints_within_the_collected_set(monkeypatch):
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _linear_tree_doc())

    result = source_index.get_packet_depth("ws_1", "root", 1)

    # root->c1 survives (both endpoints in {root, c1}); root->other_unrelated
    # and c1->gc1 do not (gc1/other_unrelated aren't collected at depth 1).
    assert result["connections"] == [{"from_topic": "root", "to_topic": "c1", "relation": "related"}]


def test_get_packet_depth_starting_partway_down_the_tree_only_walks_its_own_subtree(monkeypatch):
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _linear_tree_doc())

    result = source_index.get_packet_depth("ws_1", "c1", 5)

    assert set(result["topics"].keys()) == {"c1", "gc1"}
    assert "root" not in result["topics"]


# ---------------------------------------------------------------------
# get_topic_covered_sources
# ---------------------------------------------------------------------

def test_get_topic_covered_sources_returns_the_topics_covers_list(monkeypatch):
    topics = {"t1": _topic(name="Topic 1", source_section_ids=["n1", "n2"])}
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _doc(topics))

    result = source_index.get_topic_covered_sources("ws_1", "t1")

    assert result == ["n1", "n2"]


def test_get_topic_covered_sources_returns_empty_list_for_a_topic_with_no_covers(monkeypatch):
    topics = {"t1": _topic(name="Topic 1")}
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _doc(topics))

    assert source_index.get_topic_covered_sources("ws_1", "t1") == []


def test_get_topic_covered_sources_raises_key_error_for_unknown_topic(monkeypatch):
    monkeypatch.setattr(source_index, "get_secondary_data_scoped",
                         lambda workspace_id, scope, session_id=None: _doc({}))

    with pytest.raises(KeyError):
        source_index.get_topic_covered_sources("ws_1", "nonexistent")
