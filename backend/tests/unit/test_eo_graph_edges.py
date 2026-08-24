"""
tests/unit/test_eo_graph_edges.py — Patch 7e (content/knowledge group).

eo/graph_edges.py had zero test coverage before this. It's a small,
single-JSON-file store (same shape as eo/chat_workspace.py's
_workspaces.json), so the highest-value things to pin down are:

  1. create_edge()'s three guard rails (missing endpoint, self-edge,
     cross-workspace edge) -- a bug letting any of these through would
     let the graph view silently draw a nonsensical or unsafe link.
  2. _workspace_of()'s parsing of the `node:{workspace_id}:{node_id}`
     id shape, since every scoping function (list_edges, and indirectly
     create_edge's cross-workspace check) is built on top of it.
  3. delete_edge()/get_edge() raising FileNotFoundError for an unknown
     id rather than silently no-op'ing, since callers (e.g. a "delete
     this edge" endpoint) need to be able to tell the difference between
     "deleted" and "was never there."

Isolation: graph_edges.py does its own file I/O directly against
EDGES_PATH (no memory.bus involved), so tests monkeypatch
graph_edges.EDGES_PATH to point at a tmp_path file instead of the real
data/graph/_edges.json -- same tmp_path-redirection approach
test_eo_project_registry.py uses for its own on-disk marker file, just
applied to this module's path constant instead of a function param.
"""
import json
import os

import pytest

from eo import graph_edges


@pytest.fixture(autouse=True)
def _isolated_edges_path(tmp_path, monkeypatch):
    """Every test gets its own empty edges file -- never the real
    data/graph/_edges.json, and never state left over from a previous
    test."""
    monkeypatch.setattr(graph_edges, "EDGES_PATH", str(tmp_path / "_edges.json"))
    yield


def _node(workspace_id, node_id):
    return f"node:{workspace_id}:{node_id}"


# ---------------------------------------------------------------------
# _workspace_of
# ---------------------------------------------------------------------

def test_workspace_of_extracts_the_workspace_segment():
    assert graph_edges._workspace_of("node:ws_1:abc123") == "ws_1"


def test_workspace_of_returns_none_for_a_non_node_prefixed_id():
    assert graph_edges._workspace_of("something:ws_1:abc123") is None


def test_workspace_of_returns_none_for_an_id_missing_the_workspace_segment():
    assert graph_edges._workspace_of("node") is None


def test_workspace_of_returns_none_for_none_or_empty_input():
    assert graph_edges._workspace_of(None) is None
    assert graph_edges._workspace_of("") is None


# ---------------------------------------------------------------------
# create_edge
# ---------------------------------------------------------------------

def test_create_edge_persists_a_record_with_expected_fields():
    from_id, to_id = _node("ws_1", "n1"), _node("ws_1", "n2")

    edge = graph_edges.create_edge(from_id, to_id, "supports", "plan_writer")

    assert edge["from_node_id"] == from_id
    assert edge["to_node_id"] == to_id
    assert edge["relation"] == "supports"
    assert edge["created_by"] == "plan_writer"
    assert edge["edge_id"].startswith("edge_")
    assert "created_at" in edge


def test_create_edge_is_retrievable_afterwards():
    from_id, to_id = _node("ws_1", "n1"), _node("ws_1", "n2")
    edge = graph_edges.create_edge(from_id, to_id, "cites", "user")

    assert graph_edges.get_edge(edge["edge_id"]) == edge


def test_create_edge_defaults_a_blank_relation_to_related():
    from_id, to_id = _node("ws_1", "n1"), _node("ws_1", "n2")
    edge = graph_edges.create_edge(from_id, to_id, "   ", "user")
    assert edge["relation"] == "related"


def test_create_edge_raises_when_from_node_id_is_missing():
    with pytest.raises(ValueError):
        graph_edges.create_edge("", _node("ws_1", "n2"), "supports", "user")


def test_create_edge_raises_when_to_node_id_is_missing():
    with pytest.raises(ValueError):
        graph_edges.create_edge(_node("ws_1", "n1"), "", "supports", "user")


def test_create_edge_raises_when_endpoints_are_the_same_node():
    node_id = _node("ws_1", "n1")
    with pytest.raises(ValueError):
        graph_edges.create_edge(node_id, node_id, "supports", "user")


def test_create_edge_raises_when_endpoints_are_in_different_workspaces():
    with pytest.raises(ValueError):
        graph_edges.create_edge(_node("ws_1", "n1"), _node("ws_2", "n2"), "supports", "user")


def test_create_edge_does_not_persist_a_rejected_edge():
    """A guard-rail rejection must not leave a partial record behind --
    the whole point of validating before touching the store."""
    node_id = _node("ws_1", "n1")
    with pytest.raises(ValueError):
        graph_edges.create_edge(node_id, node_id, "supports", "user")

    assert graph_edges.list_edges() == []


# ---------------------------------------------------------------------
# delete_edge
# ---------------------------------------------------------------------

def test_delete_edge_removes_the_matching_record():
    edge = graph_edges.create_edge(_node("ws_1", "n1"), _node("ws_1", "n2"), "supports", "user")
    graph_edges.delete_edge(edge["edge_id"])
    assert graph_edges.list_edges() == []


def test_delete_edge_leaves_other_edges_untouched():
    keep = graph_edges.create_edge(_node("ws_1", "n1"), _node("ws_1", "n2"), "supports", "user")
    remove = graph_edges.create_edge(_node("ws_1", "n1"), _node("ws_1", "n3"), "cites", "user")

    graph_edges.delete_edge(remove["edge_id"])

    assert graph_edges.list_edges() == [keep]


def test_delete_edge_raises_file_not_found_for_an_unknown_id():
    with pytest.raises(FileNotFoundError):
        graph_edges.delete_edge("edge_doesnotexist")


# ---------------------------------------------------------------------
# get_edge
# ---------------------------------------------------------------------

def test_get_edge_raises_file_not_found_for_an_unknown_id():
    with pytest.raises(FileNotFoundError):
        graph_edges.get_edge("edge_doesnotexist")


# ---------------------------------------------------------------------
# list_edges
# ---------------------------------------------------------------------

def test_list_edges_with_no_workspace_id_returns_everything():
    a = graph_edges.create_edge(_node("ws_1", "n1"), _node("ws_1", "n2"), "supports", "user")
    b = graph_edges.create_edge(_node("ws_2", "n1"), _node("ws_2", "n2"), "cites", "user")

    result = graph_edges.list_edges()

    assert a in result and b in result
    assert len(result) == 2


def test_list_edges_scoped_to_a_workspace_excludes_other_workspaces():
    ws1_edge = graph_edges.create_edge(_node("ws_1", "n1"), _node("ws_1", "n2"), "supports", "user")
    graph_edges.create_edge(_node("ws_2", "n1"), _node("ws_2", "n2"), "cites", "user")

    result = graph_edges.list_edges(workspace_id="ws_1")

    assert result == [ws1_edge]


def test_list_edges_returns_empty_list_when_the_file_does_not_exist_yet():
    assert graph_edges.list_edges() == []


# ---------------------------------------------------------------------
# edges_for_node
# ---------------------------------------------------------------------

def test_edges_for_node_matches_either_direction():
    n1, n2, n3 = _node("ws_1", "n1"), _node("ws_1", "n2"), _node("ws_1", "n3")
    as_from = graph_edges.create_edge(n1, n2, "supports", "user")
    as_to = graph_edges.create_edge(n3, n1, "cites", "user")
    unrelated = graph_edges.create_edge(n2, n3, "related", "user")

    result = graph_edges.edges_for_node(n1)

    assert as_from in result
    assert as_to in result
    assert unrelated not in result
    assert len(result) == 2


def test_edges_for_node_with_no_matches_returns_empty_list():
    graph_edges.create_edge(_node("ws_1", "n1"), _node("ws_1", "n2"), "supports", "user")
    assert graph_edges.edges_for_node(_node("ws_1", "nX")) == []


# ---------------------------------------------------------------------
# edges_between
# ---------------------------------------------------------------------

def test_edges_between_matches_regardless_of_direction():
    n1, n2 = _node("ws_1", "n1"), _node("ws_1", "n2")
    edge = graph_edges.create_edge(n1, n2, "supports", "user")

    assert graph_edges.edges_between(n1, n2) == [edge]
    assert graph_edges.edges_between(n2, n1) == [edge]


def test_edges_between_excludes_edges_touching_only_one_of_the_pair():
    n1, n2, n3 = _node("ws_1", "n1"), _node("ws_1", "n2"), _node("ws_1", "n3")
    graph_edges.create_edge(n1, n3, "supports", "user")

    assert graph_edges.edges_between(n1, n2) == []


# ---------------------------------------------------------------------
# on-disk shape (light sanity check the store is really JSON, not just
# an in-memory illusion produced by the fixtures above)
# ---------------------------------------------------------------------

def test_edges_are_actually_persisted_to_disk_as_json():
    graph_edges.create_edge(_node("ws_1", "n1"), _node("ws_1", "n2"), "supports", "user")

    assert os.path.exists(graph_edges.EDGES_PATH)
    with open(graph_edges.EDGES_PATH) as f:
        on_disk = json.load(f)
    assert len(on_disk["edges"]) == 1
