"""
tests/unit/test_agent_backlink_detector.py — Patch 7f-2.

Covers agents/backlink_detector.py, which is really two independent
features sharing one filename (see the module's own docstring):

  1. detect_backlinks() / cleanup_for_removed_source() — the ORIGINAL,
     deterministic, no-LLM substring-match pass over eo/graph_edges.py.
  2. run_after_source_manager() and its helpers (§3a/§3b/§4) — the
     LLM-backed Secondary Data reconciliation pass, with its two
     sub-paths: the normal incremental pass (existing_topics non-empty)
     and the self-connect fallback (_run_self_connect_and_apply(), for
     a workspace's first source).

Dependencies (eo.knowledge_graph, eo.graph_edges, eo.secondary_data,
eo.registry, agents.generic_worker, relay.emitter, eo.notify) are
monkeypatched directly on the module under test rather than exercised
for real -- same posture test_agent_workflow_suggester.py takes with
`plan` and `agents.generic_worker.run`. get_role_prompt/add_role_prompt
are the one exception: they go through memory.bus, which the autouse
`fake_bus` fixture already isolates, so they're exercised for real.
"""
import json

import pytest

from agents import backlink_detector

# ---------------------------------------------------------------------------
# 1. detect_backlinks() — deterministic substring pass
# ---------------------------------------------------------------------------

class TestDetectBacklinks:
    def _node(self, node_id, title, content, vector_id=None):
        return {
            "node_id": node_id,
            "vector_id": vector_id or f"node:ws-1:{node_id}",
            "title": title,
            "content": content,
        }

    def test_creates_edge_when_content_mentions_another_titles(self, monkeypatch):
        nodes = [
            self._node("n1", "DC Motors", "some notes with nothing"),
            self._node("n2", "Notes on Starting", "See DC Motors for background"),
        ]
        monkeypatch.setattr(backlink_detector, "list_nodes", lambda ws: nodes)
        monkeypatch.setattr(backlink_detector, "edges_between", lambda a, b: [])
        created_calls = []

        def fake_create_edge(from_node_id, to_node_id, relation, created_by):
            edge = {
                "from_node_id": from_node_id, "to_node_id": to_node_id,
                "relation": relation, "created_by": created_by,
            }
            created_calls.append(edge)
            return edge

        monkeypatch.setattr(backlink_detector, "create_edge", fake_create_edge)
        result = backlink_detector.detect_backlinks("ws-1")

        assert len(result) == 1
        assert result[0]["from_node_id"] == "node:ws-1:n2"
        assert result[0]["to_node_id"] == "node:ws-1:n1"
        assert result[0]["relation"] == backlink_detector.RELATION

    def test_titles_below_min_length_are_never_match_targets(self, monkeypatch):
        nodes = [
            self._node("n1", "Q3", "a short generic title"),
            self._node("n2", "Some Notes", "talks about q3 a lot"),
        ]
        monkeypatch.setattr(backlink_detector, "list_nodes", lambda ws: nodes)
        monkeypatch.setattr(backlink_detector, "edges_between", lambda a, b: [])
        monkeypatch.setattr(backlink_detector, "create_edge", lambda **kw: kw)

        result = backlink_detector.detect_backlinks("ws-1")
        assert result == []

    def test_case_insensitive_match(self, monkeypatch):
        nodes = [
            self._node("n1", "Shunt Motors", "irrelevant"),
            self._node("n2", "Other", "mentions SHUNT MOTORS in caps"),
        ]
        monkeypatch.setattr(backlink_detector, "list_nodes", lambda ws: nodes)
        monkeypatch.setattr(backlink_detector, "edges_between", lambda a, b: [])
        monkeypatch.setattr(backlink_detector, "create_edge", lambda **kw: kw)

        result = backlink_detector.detect_backlinks("ws-1")
        assert len(result) == 1

    def test_skips_self_reference(self, monkeypatch):
        nodes = [self._node("n1", "Loopback Notes", "talks about Loopback Notes itself")]
        monkeypatch.setattr(backlink_detector, "list_nodes", lambda ws: nodes)
        monkeypatch.setattr(backlink_detector, "edges_between", lambda a, b: [])
        monkeypatch.setattr(backlink_detector, "create_edge", lambda **kw: kw)

        result = backlink_detector.detect_backlinks("ws-1")
        assert result == []

    def test_skips_when_edge_already_exists_either_direction(self, monkeypatch):
        nodes = [
            self._node("n1", "DC Motors", "x"),
            self._node("n2", "Notes", "See DC Motors"),
        ]
        monkeypatch.setattr(backlink_detector, "list_nodes", lambda ws: nodes)
        monkeypatch.setattr(backlink_detector, "edges_between", lambda a, b: [{"edge_id": "existing"}])
        monkeypatch.setattr(backlink_detector, "create_edge", lambda **kw: pytest.fail("should not be called"))

        result = backlink_detector.detect_backlinks("ws-1")
        assert result == []

    def test_empty_content_node_is_skipped_as_source(self, monkeypatch):
        nodes = [
            self._node("n1", "DC Motors", ""),
            self._node("n2", "Other", "x"),
        ]
        monkeypatch.setattr(backlink_detector, "list_nodes", lambda ws: nodes)
        monkeypatch.setattr(backlink_detector, "edges_between", lambda a, b: [])
        monkeypatch.setattr(backlink_detector, "create_edge", lambda **kw: kw)

        result = backlink_detector.detect_backlinks("ws-1")
        assert result == []

    def test_no_nodes_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(backlink_detector, "list_nodes", lambda ws: [])
        result = backlink_detector.detect_backlinks("ws-1")
        assert result == []


# ---------------------------------------------------------------------------
# 2. _parse_backlink_result() — §3b LLM output validation
# ---------------------------------------------------------------------------

class TestParseBacklinkResult:
    NEW_IDS = {"n1", "n2"}
    EXISTING_IDS = {"e1", "e2"}

    def _fence(self, obj):
        return f"```json\n{json.dumps(obj)}\n```"

    def test_valid_reparent_and_connection_pass_through(self):
        raw = self._fence({
            "reparents": [{"topic_id": "n1", "new_parent_id": "e1"}],
            "connections": [{"new_topic_id": "n2", "existing_topic_id": "e2", "relation": "elaborates-on"}],
        })
        reparents, connections = backlink_detector._parse_backlink_result(raw, self.NEW_IDS, self.EXISTING_IDS)
        assert reparents == [{"topic_id": "n1", "new_parent_id": "e1"}]
        assert connections == [{"from_topic": "n2", "to_topic": "e2", "relation": "elaborates-on"}]

    def test_no_json_block_returns_empty_lists(self):
        reparents, connections = backlink_detector._parse_backlink_result("no fence here", self.NEW_IDS, self.EXISTING_IDS)
        assert reparents == []
        assert connections == []

    def test_malformed_json_returns_empty_lists(self):
        raw = "```json\nnot actually json\n```"
        reparents, connections = backlink_detector._parse_backlink_result(raw, self.NEW_IDS, self.EXISTING_IDS)
        assert reparents == []
        assert connections == []

    def test_reparent_with_unknown_topic_id_is_dropped(self):
        raw = self._fence({"reparents": [{"topic_id": "not-a-new-id", "new_parent_id": "e1"}]})
        reparents, _ = backlink_detector._parse_backlink_result(raw, self.NEW_IDS, self.EXISTING_IDS)
        assert reparents == []

    def test_reparent_with_unknown_parent_id_is_dropped(self):
        raw = self._fence({"reparents": [{"topic_id": "n1", "new_parent_id": "not-existing"}]})
        reparents, _ = backlink_detector._parse_backlink_result(raw, self.NEW_IDS, self.EXISTING_IDS)
        assert reparents == []

    def test_reparent_self_cycle_is_dropped(self):
        # topic_id can't literally be in EXISTING_IDS too under real ids,
        # but the guard still needs to reject topic_id == new_parent_id
        # defensively.
        ids = {"shared"}
        raw = self._fence({"reparents": [{"topic_id": "shared", "new_parent_id": "shared"}]})
        reparents, _ = backlink_detector._parse_backlink_result(raw, ids, ids)
        assert reparents == []

    def test_connection_with_bad_ids_is_dropped(self):
        raw = self._fence({"connections": [{"new_topic_id": "n1", "existing_topic_id": "bogus", "relation": "restates"}]})
        _, connections = backlink_detector._parse_backlink_result(raw, self.NEW_IDS, self.EXISTING_IDS)
        assert connections == []

    def test_connection_with_blank_relation_is_dropped(self):
        raw = self._fence({"connections": [{"new_topic_id": "n1", "existing_topic_id": "e1", "relation": "  "}]})
        _, connections = backlink_detector._parse_backlink_result(raw, self.NEW_IDS, self.EXISTING_IDS)
        assert connections == []

    def test_non_dict_items_are_skipped(self):
        raw = self._fence({"reparents": ["not a dict"], "connections": [42]})
        reparents, connections = backlink_detector._parse_backlink_result(raw, self.NEW_IDS, self.EXISTING_IDS)
        assert reparents == []
        assert connections == []

    def test_top_level_non_dict_returns_empty_lists(self):
        raw = "```json\n[1, 2, 3]\n```"
        reparents, connections = backlink_detector._parse_backlink_result(raw, self.NEW_IDS, self.EXISTING_IDS)
        assert reparents == []
        assert connections == []


# ---------------------------------------------------------------------------
# 3. _parse_self_connect_result() — §4 same-source connect-only pass
# ---------------------------------------------------------------------------

class TestParseSelfConnectResult:
    TOPIC_IDS = {"t1", "t2", "t3"}

    def _fence(self, obj):
        return f"```json\n{json.dumps(obj)}\n```"

    def test_valid_connection_passes_through(self):
        raw = self._fence({"connections": [{"topic_a_id": "t1", "topic_b_id": "t2", "relation": "prerequisite-of"}]})
        result = backlink_detector._parse_self_connect_result(raw, self.TOPIC_IDS)
        assert result == [{"from_topic": "t1", "to_topic": "t2", "relation": "prerequisite-of"}]

    def test_self_pair_is_dropped(self):
        raw = self._fence({"connections": [{"topic_a_id": "t1", "topic_b_id": "t1", "relation": "restates"}]})
        result = backlink_detector._parse_self_connect_result(raw, self.TOPIC_IDS)
        assert result == []

    def test_unknown_id_is_dropped(self):
        raw = self._fence({"connections": [{"topic_a_id": "t1", "topic_b_id": "not-real", "relation": "restates"}]})
        result = backlink_detector._parse_self_connect_result(raw, self.TOPIC_IDS)
        assert result == []

    def test_no_fence_returns_empty(self):
        assert backlink_detector._parse_self_connect_result("plain text", self.TOPIC_IDS) == []


# ---------------------------------------------------------------------------
# 4. _build_ops() — RFC 6902 op construction + de-dup
# ---------------------------------------------------------------------------

class TestBuildOps:
    def _doc(self, topics=None, connections=None):
        return {"topics": topics or {}, "connections": connections or []}

    def test_reparent_becomes_replace_op_with_only_parent_changed(self):
        doc = self._doc(topics={"n1": {"name": "Topic", "parent": None, "summary": "s"}})
        ops = backlink_detector._build_ops([{"topic_id": "n1", "new_parent_id": "e1"}], [], doc)
        assert ops == [{
            "op": "replace", "path": "/topics/n1",
            "value": {"name": "Topic", "parent": "e1", "summary": "s"},
        }]

    def test_connection_becomes_add_op(self):
        doc = self._doc()
        ops = backlink_detector._build_ops([], [{"from_topic": "n1", "to_topic": "e1", "relation": "restates"}], doc)
        assert ops == [{
            "op": "add", "path": "/connections/-",
            "value": {"from_topic": "n1", "to_topic": "e1", "relation": "restates"},
        }]

    def test_connection_deduped_against_existing_regardless_of_direction(self):
        doc = self._doc(connections=[{"from_topic": "e1", "to_topic": "n1", "relation": "restates"}])
        ops = backlink_detector._build_ops([], [{"from_topic": "n1", "to_topic": "e1", "relation": "elaborates-on"}], doc)
        assert ops == []

    def test_duplicate_connections_within_same_batch_only_written_once(self):
        doc = self._doc()
        connections = [
            {"from_topic": "n1", "to_topic": "e1", "relation": "restates"},
            {"from_topic": "e1", "to_topic": "n1", "relation": "contradicts"},
        ]
        ops = backlink_detector._build_ops([], connections, doc)
        assert len(ops) == 1


# ---------------------------------------------------------------------------
# 5. run_after_source_manager() — guards, incremental pass, self-connect
# ---------------------------------------------------------------------------

class TestRunAfterSourceManager:
    def test_empty_workspace_id_or_topic_ids_returns_empty_without_touching_anything(self):
        assert backlink_detector.run_after_source_manager("", ["t1"]) == []
        assert backlink_detector.run_after_source_manager("ws-1", []) == []

    def test_topic_ids_not_in_doc_returns_empty(self, monkeypatch):
        monkeypatch.setattr(backlink_detector, "get_secondary_data",
                             lambda ws: {"topics": {}, "connections": []})
        result = backlink_detector.run_after_source_manager("ws-1", ["missing-id"])
        assert result == []

    def test_first_source_in_workspace_uses_self_connect_path(self, monkeypatch):
        # existing_topics ends up empty -- only this call's own new_topics
        # exist in the doc.
        doc = {
            "topics": {"n1": {"name": "A", "summary": "s"}, "n2": {"name": "B", "summary": "s"}},
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        called = {}

        def fake_self_connect(workspace_id, new_topics, doc_arg, session_id=None):
            called["new_topics"] = new_topics
            return [{"op": "add", "path": "/connections/-", "value": {}}]

        monkeypatch.setattr(backlink_detector, "_run_self_connect_and_apply", fake_self_connect)
        result = backlink_detector.run_after_source_manager("ws-1", ["n1", "n2"])

        assert set(called["new_topics"]) == {"n1", "n2"}
        assert len(result) == 1

    def test_incremental_pass_applies_parsed_ops(self, monkeypatch):
        doc = {
            "topics": {
                "n1": {"name": "New Topic", "summary": "s", "parent": None},
                "e1": {"name": "Existing Topic", "summary": "s", "parent": None},
            },
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        monkeypatch.setattr(backlink_detector, "_run_incremental_pass", lambda *a, **k: "raw")
        monkeypatch.setattr(
            backlink_detector, "_parse_backlink_result",
            lambda raw, new_ids, existing_ids: (
                [],
                [{"from_topic": "n1", "to_topic": "e1", "relation": "elaborates-on"}],
            ),
        )
        applied_ops = {}
        monkeypatch.setattr(backlink_detector, "apply_patch",
                             lambda ws, ops: applied_ops.setdefault("ops", ops))

        result = backlink_detector.run_after_source_manager("ws-1", ["n1"])
        assert len(result) == 1
        assert result[0]["op"] == "add"
        assert applied_ops["ops"] == result

    def test_merge_tagged_topic_short_circuits_to_same_fact_as(self, monkeypatch):
        doc = {
            "topics": {
                "n1": {"name": "New Topic", "summary": "s", "parent": None},
                "e1": {"name": "Existing Topic", "summary": "s", "parent": None},
            },
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        applied_ops = {}
        monkeypatch.setattr(backlink_detector, "apply_patch",
                             lambda ws, ops: applied_ops.setdefault("ops", applied_ops.get("ops", []) + ops))
        # No reconcile_ids left, so the LLM pass must never be called.
        monkeypatch.setattr(
            backlink_detector, "_run_incremental_pass",
            lambda *a, **k: pytest.fail("LLM pass should be skipped for a fully-merged batch"),
        )

        result = backlink_detector.run_after_source_manager(
            "ws-1", ["n1"],
            overlap_tags={"n1": {"tag": "merge", "target_topic_id": "e1"}},
        )
        assert len(result) == 1
        assert result[0]["value"]["relation"] == "same_fact_as"
        assert result[0]["value"]["from_topic"] == "n1"
        assert result[0]["value"]["to_topic"] == "e1"

    def test_merge_tag_targeting_non_existing_topic_falls_through_to_llm_pass(self, monkeypatch):
        doc = {
            "topics": {
                "n1": {"name": "New Topic", "summary": "s", "parent": None},
                "e1": {"name": "Existing Topic", "summary": "s", "parent": None},
            },
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        monkeypatch.setattr(backlink_detector, "apply_patch", lambda ws, ops: None)
        called = {}

        def fake_incremental(new_topics, existing_topics, session_id=None):
            called["ran"] = True
            return "raw"

        monkeypatch.setattr(backlink_detector, "_run_incremental_pass", fake_incremental)
        monkeypatch.setattr(backlink_detector, "_parse_backlink_result", lambda *a, **k: ([], []))

        backlink_detector.run_after_source_manager(
            "ws-1", ["n1"],
            overlap_tags={"n1": {"tag": "merge", "target_topic_id": "does-not-exist"}},
        )
        assert called.get("ran") is True

    def test_never_raises_when_incremental_pass_blows_up(self, monkeypatch):
        doc = {
            "topics": {
                "n1": {"name": "New Topic", "summary": "s", "parent": None},
                "e1": {"name": "Existing Topic", "summary": "s", "parent": None},
            },
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)

        def raising_pass(*a, **k):
            raise RuntimeError("provider outage")

        monkeypatch.setattr(backlink_detector, "_run_incremental_pass", raising_pass)
        result = backlink_detector.run_after_source_manager("ws-1", ["n1"])
        assert result == []

    def test_existing_topics_over_cap_are_truncated(self, monkeypatch):
        many_existing = {f"e{i}": {"name": f"T{i}", "summary": "s", "parent": None}
                          for i in range(backlink_detector.BACKLINK_MAX_EXISTING_TOPICS + 5)}
        doc = {
            "topics": {**many_existing, "n1": {"name": "New", "summary": "s", "parent": None}},
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        captured = {}

        def fake_incremental(new_topics, existing_topics, session_id=None):
            captured["count"] = len(existing_topics)
            return ""

        monkeypatch.setattr(backlink_detector, "_run_incremental_pass", fake_incremental)
        monkeypatch.setattr(backlink_detector, "_parse_backlink_result", lambda *a, **k: ([], []))
        backlink_detector.run_after_source_manager("ws-1", ["n1"])
        assert captured["count"] == backlink_detector.BACKLINK_MAX_EXISTING_TOPICS


# ---------------------------------------------------------------------------
# 6. cleanup_for_removed_source() — §3c deletion cleanup
# ---------------------------------------------------------------------------

class TestCleanupForRemovedSource:
    def test_empty_workspace_id_or_node_ids_is_a_noop(self, monkeypatch):
        assert backlink_detector.cleanup_for_removed_source("", ["n1"]) == []
        assert backlink_detector.cleanup_for_removed_source("ws-1", []) == []

    def test_no_topics_reference_removed_nodes_returns_empty(self, monkeypatch):
        doc = {"topics": {"t1": {"source_section_ids": ["other-node"]}}, "connections": []}
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        result = backlink_detector.cleanup_for_removed_source("ws-1", ["removed-node"])
        assert result == []

    def test_removed_topic_generates_a_remove_op(self, monkeypatch):
        doc = {
            "topics": {"t1": {"source_section_ids": ["removed-node"], "parent": None}},
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        captured = {}
        monkeypatch.setattr(backlink_detector, "apply_patch",
                             lambda ws, ops: captured.setdefault("ops", ops))

        result = backlink_detector.cleanup_for_removed_source("ws-1", ["removed-node"])
        assert {"op": "remove", "path": "/topics/t1"} in result
        assert captured["ops"] == result

    def test_child_of_removed_topic_is_reparented_not_deleted(self, monkeypatch):
        doc = {
            "topics": {
                "t1": {"source_section_ids": ["removed-node"], "parent": None},
                "t2": {"source_section_ids": ["some-other-node"], "parent": "t1"},
            },
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        monkeypatch.setattr(backlink_detector, "apply_patch", lambda ws, ops: None)

        result = backlink_detector.cleanup_for_removed_source("ws-1", ["removed-node"])
        reparent_ops = [op for op in result if op["path"] == "/topics/t2"]
        assert len(reparent_ops) == 1
        assert reparent_ops[0]["op"] == "replace"
        assert reparent_ops[0]["value"]["parent"] is None

    def test_stale_connections_removed_in_descending_index_order(self, monkeypatch):
        doc = {
            "topics": {"t1": {"source_section_ids": ["removed-node"], "parent": None}},
            "connections": [
                {"from_topic": "t1", "to_topic": "t2"},
                {"from_topic": "t2", "to_topic": "t3"},
                {"from_topic": "t3", "to_topic": "t1"},
            ],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)
        monkeypatch.setattr(backlink_detector, "apply_patch", lambda ws, ops: None)

        result = backlink_detector.cleanup_for_removed_source("ws-1", ["removed-node"])
        connection_ops = [op for op in result if op["path"].startswith("/connections/")]
        indices = [int(op["path"].rsplit("/", 1)[1]) for op in connection_ops]
        assert indices == sorted(indices, reverse=True)
        assert set(indices) == {0, 2}  # touches t1: index 0 and index 2

    def test_apply_patch_failure_is_caught_and_returns_empty(self, monkeypatch):
        doc = {
            "topics": {"t1": {"source_section_ids": ["removed-node"], "parent": None}},
            "connections": [],
        }
        monkeypatch.setattr(backlink_detector, "get_secondary_data", lambda ws: doc)

        def raising_apply(ws, ops):
            raise ValueError("bad patch")

        monkeypatch.setattr(backlink_detector, "apply_patch", raising_apply)
        result = backlink_detector.cleanup_for_removed_source("ws-1", ["removed-node"])
        assert result == []
