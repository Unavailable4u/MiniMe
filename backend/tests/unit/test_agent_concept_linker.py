"""
tests/unit/test_agent_concept_linker.py — Patch 7f-2.

Covers agents/concept_linker.py -- Data Layer architecture §6a's
deterministic (no LLM) materialization pass: reads a Mode C packet
(eo/source_index.py:get_packet) and projects it onto real graph edges
(eo/graph_edges.py) and per-node summaries (eo/node_summaries.py).
get_packet/create_edge/edges_between/node_summaries.set_summaries are
monkeypatched directly on the module (real disk-backed stores, same
posture test_agent_backlink_detector.py takes with the same
eo.graph_edges functions). eo.workspace_facts goes through memory.bus,
so the regeneration-skip logic (_needs_regeneration()) is exercised
against the real store via the autouse `fake_bus` fixture.
"""
import pytest

from agents import concept_linker


def _packet(topics=None, connections=None):
    return {"topics": topics or {}, "connections": connections or []}


# ---------------------------------------------------------------------------
# 1. _anchor_node_id() / _anchor_vector_id()
# ---------------------------------------------------------------------------

class TestAnchorHelpers:
    def test_anchor_node_id_is_first_covers_entry(self):
        topic = {"covers": ["node-a", "node-b"]}
        assert concept_linker._anchor_node_id("ws-1", topic) == "node-a"

    def test_anchor_node_id_none_when_covers_empty(self):
        assert concept_linker._anchor_node_id("ws-1", {"covers": []}) is None
        assert concept_linker._anchor_node_id("ws-1", {}) is None

    def test_anchor_vector_id_builds_prefixed_id(self):
        topic = {"covers": ["node-a"]}
        assert concept_linker._anchor_vector_id("ws-1", topic) == "node:ws-1:node-a"

    def test_anchor_vector_id_none_when_no_anchor(self):
        assert concept_linker._anchor_vector_id("ws-1", {"covers": []}) is None


# ---------------------------------------------------------------------------
# 2. _connections_signature() / _needs_regeneration()
# ---------------------------------------------------------------------------

class TestSignatureAndRegeneration:
    def test_signature_is_order_independent(self):
        a = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"},
             {"from_topic": "t2", "to_topic": "t3", "relation": "elaborates-on"}]
        b = list(reversed(a))
        assert concept_linker._connections_signature(a) == concept_linker._connections_signature(b)

    def test_signature_changes_when_relation_changes(self):
        a = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"}]
        b = [{"from_topic": "t1", "to_topic": "t2", "relation": "contradicts"}]
        assert concept_linker._connections_signature(a) != concept_linker._connections_signature(b)

    def test_needs_regeneration_true_when_never_run_before(self):
        assert concept_linker._needs_regeneration("ws-never-run", []) is True

    def test_needs_regeneration_false_when_signature_matches(self):
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"}]
        from eo import workspace_facts
        workspace_facts.update_custom_fact(
            "ws-1", concept_linker.CONNECTIONS_SIGNATURE_KEY,
            concept_linker._connections_signature(connections),
        )
        assert concept_linker._needs_regeneration("ws-1", connections) is False

    def test_needs_regeneration_true_when_signature_differs(self):
        from eo import workspace_facts
        workspace_facts.update_custom_fact(
            "ws-1", concept_linker.CONNECTIONS_SIGNATURE_KEY, "some-stale-signature",
        )
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"}]
        assert concept_linker._needs_regeneration("ws-1", connections) is True


# ---------------------------------------------------------------------------
# 3. link_concepts() — status branches and write behavior
# ---------------------------------------------------------------------------

class TestLinkConcepts:
    def test_empty_status_when_no_topics_in_scope(self, monkeypatch):
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": _packet())
        result = concept_linker.link_concepts("ws-1")
        assert result == {"status": "empty", "edges_created": [], "summaries": {}}

    def test_up_to_date_status_when_signature_unchanged(self, monkeypatch):
        topics = {"t1": {"name": "A", "summary": "s", "covers": ["node-a"]}}
        packet = _packet(topics)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)

        from eo import workspace_facts
        workspace_facts.update_custom_fact(
            "ws-1", concept_linker.CONNECTIONS_SIGNATURE_KEY,
            concept_linker._connections_signature(packet["connections"]),
        )
        result = concept_linker.link_concepts("ws-1")
        assert result["status"] == "up_to_date"
        assert result["edges_created"] == []

    def test_force_bypasses_up_to_date_check(self, monkeypatch):
        topics = {"t1": {"name": "A", "summary": "s", "covers": ["node-a"]}}
        packet = _packet(topics)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)

        from eo import workspace_facts
        workspace_facts.update_custom_fact(
            "ws-1", concept_linker.CONNECTIONS_SIGNATURE_KEY,
            concept_linker._connections_signature(packet["connections"]),
        )
        monkeypatch.setattr(concept_linker.node_summaries, "set_summaries", lambda ws, summaries: summaries)
        result = concept_linker.link_concepts("ws-1", force=True)
        assert result["status"] == "done"

    def test_done_status_creates_edges_and_summaries(self, monkeypatch):
        topics = {
            "t1": {"name": "A", "summary": "summary a", "covers": ["node-a"]},
            "t2": {"name": "B", "summary": "summary b", "covers": ["node-b"]},
        }
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "elaborates-on"}]
        packet = _packet(topics, connections)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)
        monkeypatch.setattr(concept_linker, "edges_between", lambda a, b: [])

        created_edges = []

        def fake_create_edge(from_node_id, to_node_id, relation, created_by):
            edge = {"from_node_id": from_node_id, "to_node_id": to_node_id, "relation": relation, "created_by": created_by}
            created_edges.append(edge)
            return edge

        monkeypatch.setattr(concept_linker, "create_edge", fake_create_edge)
        monkeypatch.setattr(concept_linker.node_summaries, "set_summaries", lambda ws, summaries: summaries)

        result = concept_linker.link_concepts("ws-1")
        assert result["status"] == "done"
        assert len(result["edges_created"]) == 1
        assert result["edges_created"][0]["from_node_id"] == "node:ws-1:node-a"
        assert result["edges_created"][0]["to_node_id"] == "node:ws-1:node-b"
        assert result["edges_created"][0]["created_by"] == "concept_linker"
        assert result["summaries"] == {"node-a": "summary a", "node-b": "summary b"}

    def test_skips_connection_when_already_linked(self, monkeypatch):
        topics = {
            "t1": {"name": "A", "summary": "s", "covers": ["node-a"]},
            "t2": {"name": "B", "summary": "s", "covers": ["node-b"]},
        }
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"}]
        packet = _packet(topics, connections)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)
        monkeypatch.setattr(concept_linker, "edges_between", lambda a, b: [{"edge_id": "already-there"}])
        monkeypatch.setattr(concept_linker, "create_edge", lambda **kw: pytest.fail("should not be called"))
        monkeypatch.setattr(concept_linker.node_summaries, "set_summaries", lambda ws, summaries: summaries)

        result = concept_linker.link_concepts("ws-1")
        assert result["edges_created"] == []

    def test_skips_connection_with_no_usable_anchor(self, monkeypatch):
        topics = {
            "t1": {"name": "A", "summary": "s", "covers": []},  # no anchor
            "t2": {"name": "B", "summary": "s", "covers": ["node-b"]},
        }
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"}]
        packet = _packet(topics, connections)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)
        monkeypatch.setattr(concept_linker, "edges_between", lambda a, b: [])
        monkeypatch.setattr(concept_linker, "create_edge", lambda **kw: pytest.fail("should not be called"))
        monkeypatch.setattr(concept_linker.node_summaries, "set_summaries", lambda ws, summaries: summaries)

        result = concept_linker.link_concepts("ws-1")
        assert result["edges_created"] == []

    def test_skips_connection_when_both_topics_share_the_same_anchor(self, monkeypatch):
        topics = {
            "t1": {"name": "A", "summary": "s", "covers": ["node-shared"]},
            "t2": {"name": "B", "summary": "s", "covers": ["node-shared"]},
        }
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"}]
        packet = _packet(topics, connections)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)
        monkeypatch.setattr(concept_linker, "edges_between", lambda a, b: [])
        monkeypatch.setattr(concept_linker, "create_edge", lambda **kw: pytest.fail("should not be called"))
        monkeypatch.setattr(concept_linker.node_summaries, "set_summaries", lambda ws, summaries: summaries)

        result = concept_linker.link_concepts("ws-1")
        assert result["edges_created"] == []

    def test_create_edge_value_error_is_caught_and_skipped(self, monkeypatch):
        topics = {
            "t1": {"name": "A", "summary": "s", "covers": ["node-a"]},
            "t2": {"name": "B", "summary": "s", "covers": ["node-b"]},
        }
        connections = [{"from_topic": "t1", "to_topic": "t2", "relation": "restates"}]
        packet = _packet(topics, connections)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)
        monkeypatch.setattr(concept_linker, "edges_between", lambda a, b: [])

        def raising_create_edge(**kwargs):
            raise ValueError("collapsed to same node")

        monkeypatch.setattr(concept_linker, "create_edge", raising_create_edge)
        monkeypatch.setattr(concept_linker.node_summaries, "set_summaries", lambda ws, summaries: summaries)

        result = concept_linker.link_concepts("ws-1")
        assert result["status"] == "done"
        assert result["edges_created"] == []

    def test_source_node_ids_scopes_topics_by_covers(self, monkeypatch):
        topics = {
            "t1": {"name": "In scope", "summary": "s", "covers": ["node-a"]},
            "t2": {"name": "Out of scope", "summary": "s", "covers": ["node-b"]},
        }
        packet = _packet(topics)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)
        monkeypatch.setattr(concept_linker.node_summaries, "set_summaries", lambda ws, summaries: summaries)

        result = concept_linker.link_concepts("ws-1", source_node_ids=["node-a"])
        assert result["status"] == "done"
        assert result["summaries"] == {"node-a": "s"}

    def test_high_water_mark_updated_after_a_done_run(self, monkeypatch):
        topics = {"t1": {"name": "A", "summary": "s", "covers": ["node-a"]}}
        packet = _packet(topics)
        monkeypatch.setattr(concept_linker, "get_packet", lambda ws, scope="project": packet)
        monkeypatch.setattr(concept_linker.node_summaries, "set_summaries", lambda ws, summaries: summaries)

        concept_linker.link_concepts("ws-1")

        from eo import workspace_facts
        stored = workspace_facts.get_facts("ws-1")["custom"][concept_linker.CONNECTIONS_SIGNATURE_KEY]
        assert stored == concept_linker._connections_signature(packet["connections"])
        assert concept_linker.LAST_RUN_AT_KEY in workspace_facts.get_facts("ws-1")["custom"]
