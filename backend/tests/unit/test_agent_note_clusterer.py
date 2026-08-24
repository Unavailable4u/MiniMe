"""
tests/unit/test_agent_note_clusterer.py — Patch 7f-7-2b.

Covers agents/note_clusterer.py — Part 4 §4.3's deterministic,
no-LLM-call auto-clustering: propose_clusters() reads every node's
embedding via eo/knowledge_graph.py's list_nodes(include_vectors=True),
runs scikit-learn KMeans locally (skipped entirely, empty-candidates
result, below MIN_NODES_TO_CLUSTER), and replaces the workspace's whole
pending-candidate list in the module's own single-file JSON store.
accept_candidate() connects every cluster member to the cluster's first
node with a star-topology "clustered_with" edge via
eo/graph_edges.py's create_edge() and drops the candidate; reject_candidate()
just drops it. Both raise FileNotFoundError for an unknown candidate_id.

No generate_text call exists anywhere in this module — nothing here
touches mock_llm. list_nodes and create_edge are both bound names in
the module's own namespace and are monkeypatched directly. The JSON
candidate store itself is real (not mocked) but redirected to a tmp_path
file via CANDIDATES_PATH, so each test gets a clean, isolated store —
same "swap the constant, not the I/O" approach
test_agent_documentation_agent.py takes with APPS_ROOT.
"""
import json
from unittest.mock import MagicMock

import pytest

from agents import note_clusterer


@pytest.fixture(autouse=True)
def _isolated_store(monkeypatch, tmp_path):
    """Every test gets its own empty candidates file, so cluster state
    from one test can never leak into another (this module's on-disk
    store is otherwise a fixed, repo-relative path)."""
    monkeypatch.setattr(note_clusterer, "CANDIDATES_PATH", str(tmp_path / "_cluster_candidates.json"))


def _node(node_id, vector, title="Untitled", tags=None, vector_id=None):
    return {
        "node_id": node_id, "vector_id": vector_id or f"vec:{node_id}",
        "title": title, "tags": tags or [], "vector": vector,
    }


# ---------------------------------------------------------------------------
# 1. propose_clusters(): below the minimum node count -> no KMeans call
# ---------------------------------------------------------------------------
class TestBelowMinimum:
    def test_fewer_than_min_nodes_returns_empty_candidates_without_clustering(self, monkeypatch):
        nodes = [_node(f"n{i}", [float(i), 0.0]) for i in range(note_clusterer.MIN_NODES_TO_CLUSTER - 1)]
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: nodes)
        result = note_clusterer.propose_clusters("ws1")
        assert result == []

    def test_nodes_without_a_vector_are_excluded_from_the_count(self, monkeypatch):
        nodes = [_node(f"n{i}", [1.0, 1.0]) for i in range(note_clusterer.MIN_NODES_TO_CLUSTER)]
        nodes[0]["vector"] = None
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: nodes)
        result = note_clusterer.propose_clusters("ws1")
        # one node lost its vector, dropping the effective count below the
        # minimum -- so this must degrade to the empty-candidates path.
        assert result == []

    def test_below_minimum_still_overwrites_stored_candidates(self, monkeypatch):
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: [])
        note_clusterer._write({"ws1": [{"candidate_id": "stale"}]})
        note_clusterer.propose_clusters("ws1")
        assert note_clusterer.list_candidates("ws1") == []


# ---------------------------------------------------------------------------
# 2. propose_clusters(): real KMeans run over well-separated groups
# ---------------------------------------------------------------------------
class TestClustering:
    def _two_tight_groups(self):
        # Two obviously-separated 2D blobs, four points each — comfortably
        # above MIN_NODES_TO_CLUSTER (4) and far enough apart that KMeans
        # with k>=2 reliably recovers the two groups regardless of the
        # random_state=0 seed.
        group_a = [_node(f"a{i}", [0.0 + i * 0.01, 0.0 + i * 0.01], title=f"A{i}") for i in range(4)]
        group_b = [_node(f"b{i}", [100.0 + i * 0.01, 100.0 + i * 0.01], title=f"B{i}") for i in range(4)]
        return group_a + group_b

    def test_produces_at_least_one_candidate_for_a_clear_two_group_split(self, monkeypatch):
        nodes = self._two_tight_groups()
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: nodes)
        result = note_clusterer.propose_clusters("ws1", max_clusters=2)
        assert len(result) >= 1
        for candidate in result:
            assert len(candidate["node_ids"]) >= 2

    def test_candidate_shape_has_required_fields(self, monkeypatch):
        nodes = self._two_tight_groups()
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: nodes)
        result = note_clusterer.propose_clusters("ws1", max_clusters=2)
        for candidate in result:
            assert candidate["candidate_id"].startswith("cluster_")
            assert "suggested_label" in candidate
            assert isinstance(candidate["node_ids"], list)
            assert isinstance(candidate["titles"], list)
            assert "created_at" in candidate

    def test_node_ids_use_vector_id_not_node_id(self, monkeypatch):
        nodes = self._two_tight_groups()
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: nodes)
        result = note_clusterer.propose_clusters("ws1", max_clusters=2)
        all_ids = {nid for c in result for nid in c["node_ids"]}
        assert all_ids <= {n["vector_id"] for n in nodes}

    def test_singleton_clusters_are_dropped(self, monkeypatch):
        # k is forced high enough (via max_clusters) that with only 4
        # points some KMeans clusters could come back with a single
        # member -- those must never appear as a "suggestion".
        nodes = [_node(f"n{i}", [float(i) * 50, float(i) * 50]) for i in range(4)]
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: nodes)
        result = note_clusterer.propose_clusters("ws1", max_clusters=6)
        for candidate in result:
            assert len(candidate["node_ids"]) >= 2

    def test_result_is_persisted_and_replaces_prior_candidates(self, monkeypatch):
        nodes = self._two_tight_groups()
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: nodes)
        note_clusterer._write({"ws1": [{"candidate_id": "stale_one"}]})
        result = note_clusterer.propose_clusters("ws1", max_clusters=2)
        stored = note_clusterer.list_candidates("ws1")
        assert stored == result
        assert all(c["candidate_id"] != "stale_one" for c in stored)

    def test_other_workspaces_candidates_are_untouched(self, monkeypatch):
        nodes = self._two_tight_groups()
        monkeypatch.setattr(note_clusterer, "list_nodes", lambda ws, include_vectors=False: nodes)
        note_clusterer._write({"other_ws": [{"candidate_id": "keep_me"}]})
        note_clusterer.propose_clusters("ws1", max_clusters=2)
        assert note_clusterer.list_candidates("other_ws") == [{"candidate_id": "keep_me"}]


# ---------------------------------------------------------------------------
# 3. _label_for(): most-common-tag, falling back to first member's title
# ---------------------------------------------------------------------------
class TestLabelFor:
    def test_most_common_tag_wins(self):
        members = [
            {"title": "M1", "tags": ["motors"]},
            {"title": "M2", "tags": ["motors", "electrical"]},
            {"title": "M3", "tags": ["electrical"]},
            {"title": "M4", "tags": ["motors"]},
        ]
        assert note_clusterer._label_for(members) == "motors"

    def test_falls_back_to_first_member_title_when_no_tags_at_all(self):
        members = [{"title": "First Note", "tags": []}, {"title": "Second", "tags": []}]
        assert note_clusterer._label_for(members) == "First Note"

    def test_missing_title_falls_back_to_untitled(self):
        members = [{"tags": []}]
        assert note_clusterer._label_for(members) == "Untitled cluster"

    def test_missing_tags_key_does_not_raise(self):
        members = [{"title": "No Tags Key"}]
        assert note_clusterer._label_for(members) == "No Tags Key"


# ---------------------------------------------------------------------------
# 4. accept_candidate(): star-topology edges + candidate removal
# ---------------------------------------------------------------------------
class TestAcceptCandidate:
    def _seed(self, workspace_id, candidate):
        note_clusterer._write({workspace_id: [candidate]})

    def test_creates_star_topology_edges_from_first_member(self, monkeypatch):
        candidate = {
            "candidate_id": "cluster_1", "suggested_label": "x",
            "node_ids": ["v1", "v2", "v3"], "titles": [], "created_at": "t",
        }
        self._seed("ws1", candidate)
        create_mock = MagicMock(side_effect=lambda **kw: dict(kw))
        monkeypatch.setattr(note_clusterer, "create_edge", create_mock)

        created = note_clusterer.accept_candidate("ws1", "cluster_1")

        assert create_mock.call_count == 2
        calls = [c.kwargs for c in create_mock.call_args_list]
        assert all(c["from_node_id"] == "v1" for c in calls)
        assert {c["to_node_id"] for c in calls} == {"v2", "v3"}
        assert all(c["relation"] == note_clusterer.RELATION for c in calls)
        assert all(c["created_by"] == "note_clusterer" for c in calls)
        assert len(created) == 2

    def test_removes_the_candidate_after_accepting(self, monkeypatch):
        candidate = {"candidate_id": "cluster_1", "node_ids": ["v1", "v2"], "titles": [], "created_at": "t"}
        self._seed("ws1", candidate)
        monkeypatch.setattr(note_clusterer, "create_edge", lambda **kw: kw)

        note_clusterer.accept_candidate("ws1", "cluster_1")

        assert note_clusterer.list_candidates("ws1") == []

    def test_unknown_candidate_id_raises_file_not_found(self):
        note_clusterer._write({"ws1": []})
        with pytest.raises(FileNotFoundError):
            note_clusterer.accept_candidate("ws1", "does_not_exist")

    def test_two_member_cluster_creates_exactly_one_edge(self, monkeypatch):
        candidate = {"candidate_id": "cluster_1", "node_ids": ["v1", "v2"], "titles": [], "created_at": "t"}
        self._seed("ws1", candidate)
        create_mock = MagicMock(side_effect=lambda **kw: dict(kw))
        monkeypatch.setattr(note_clusterer, "create_edge", create_mock)
        created = note_clusterer.accept_candidate("ws1", "cluster_1")
        assert len(created) == 1

    def test_only_the_accepted_candidate_is_removed(self, monkeypatch):
        c1 = {"candidate_id": "cluster_1", "node_ids": ["v1", "v2"], "titles": [], "created_at": "t"}
        c2 = {"candidate_id": "cluster_2", "node_ids": ["v3", "v4"], "titles": [], "created_at": "t"}
        note_clusterer._write({"ws1": [c1, c2]})
        monkeypatch.setattr(note_clusterer, "create_edge", lambda **kw: kw)

        note_clusterer.accept_candidate("ws1", "cluster_1")

        remaining = note_clusterer.list_candidates("ws1")
        assert [c["candidate_id"] for c in remaining] == ["cluster_2"]


# ---------------------------------------------------------------------------
# 5. reject_candidate(): drops without touching the graph
# ---------------------------------------------------------------------------
class TestRejectCandidate:
    def test_removes_the_candidate(self):
        note_clusterer._write({"ws1": [{"candidate_id": "cluster_1", "node_ids": []}]})
        note_clusterer.reject_candidate("ws1", "cluster_1")
        assert note_clusterer.list_candidates("ws1") == []

    def test_unknown_candidate_id_raises_file_not_found(self):
        note_clusterer._write({"ws1": [{"candidate_id": "cluster_1", "node_ids": []}]})
        with pytest.raises(FileNotFoundError):
            note_clusterer.reject_candidate("ws1", "does_not_exist")

    def test_other_candidates_in_the_same_workspace_are_untouched(self):
        note_clusterer._write({"ws1": [
            {"candidate_id": "cluster_1", "node_ids": []},
            {"candidate_id": "cluster_2", "node_ids": []},
        ]})
        note_clusterer.reject_candidate("ws1", "cluster_1")
        remaining = note_clusterer.list_candidates("ws1")
        assert [c["candidate_id"] for c in remaining] == ["cluster_2"]

    def test_empty_workspace_raises_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            note_clusterer.reject_candidate("ws_never_seen", "cluster_1")


# ---------------------------------------------------------------------------
# 6. list_candidates() / _read() / _write(): store round-trip
# ---------------------------------------------------------------------------
class TestStoreRoundTrip:
    def test_list_candidates_for_unknown_workspace_returns_empty_list(self):
        assert note_clusterer.list_candidates("never_seen") == []

    def test_read_returns_empty_dict_when_file_does_not_exist_yet(self):
        assert note_clusterer._read() == {}

    def test_write_then_read_round_trips(self):
        note_clusterer._write({"ws1": [{"candidate_id": "c1", "node_ids": ["v1"]}]})
        data = note_clusterer._read()
        assert data == {"ws1": [{"candidate_id": "c1", "node_ids": ["v1"]}]}

    def test_write_creates_parent_directory_if_missing(self, tmp_path, monkeypatch):
        nested_path = tmp_path / "does" / "not" / "exist" / "_cluster_candidates.json"
        monkeypatch.setattr(note_clusterer, "CANDIDATES_PATH", str(nested_path))
        note_clusterer._write({"ws1": []})
        assert nested_path.exists()
        assert json.loads(nested_path.read_text()) == {"ws1": []}
