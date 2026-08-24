"""
tests/unit/test_agent_citation_graph_builder.py — Patch 7f-2.

Covers agents/citation_graph_builder.py -- a read-only, zero-LLM,
zero-HTTP tool agent (see its own docstring). list_edges/get_node are
monkeypatched directly on the module (same posture as
test_agent_backlink_detector.py takes with eo.graph_edges/
eo.knowledge_graph) rather than exercised against the real JSON-file
store. KEYS["academic_search_report"]/KEYS["citation_graph"] go through
memory.bus, so those are exercised for real via the autouse `fake_bus`.
"""
import pytest

import agents.citation_graph_builder as citation_graph_builder
from memory.bus import write, read, KEYS
from eo.errors import MissingDependencyError


def _seed_report(papers):
    write(KEYS["academic_search_report"], {"papers": papers})


# ---------------------------------------------------------------------------
# 1. run() — guards and aggregation
# ---------------------------------------------------------------------------

class TestRun:
    def test_raises_missing_dependency_when_no_report(self):
        with pytest.raises(MissingDependencyError) as exc_info:
            citation_graph_builder.run()
        assert exc_info.value.required_role == "academic_search"

    def test_raises_missing_dependency_when_report_has_no_papers(self):
        write(KEYS["academic_search_report"], {"papers": []})
        with pytest.raises(MissingDependencyError):
            citation_graph_builder.run()

    def test_nodes_seeded_from_report_with_zero_edges(self, monkeypatch):
        _seed_report([{"node_id": "p1", "title": "Paper One", "year": 2020}])
        monkeypatch.setattr(citation_graph_builder, "list_edges", lambda ws: [])

        result = citation_graph_builder.run()
        assert len(result["nodes"]) == 1
        assert result["nodes"][0]["node_id"] == "p1"
        assert result["nodes"][0]["in_degree"] == 0
        assert result["isolated_count"] == 1
        assert result["edges"] == []

    def test_cites_edges_computed_and_bare_ids_resolved(self, monkeypatch):
        _seed_report([
            {"node_id": "p1", "title": "Paper One", "year": 2020},
            {"node_id": "p2", "title": "Paper Two", "year": 2021},
        ])
        edges = [
            {"from_node_id": "node:ws:p1", "to_node_id": "node:ws:p2", "relation": "cites"},
            {"from_node_id": "node:ws:p1", "to_node_id": "node:ws:p2", "relation": "references"},  # not "cites" -- ignored
        ]
        monkeypatch.setattr(citation_graph_builder, "list_edges", lambda ws: edges)

        result = citation_graph_builder.run()
        assert len(result["edges"]) == 1
        assert result["edges"][0] == {"from_node_id": "p1", "to_node_id": "p2"}
        p2 = next(n for n in result["nodes"] if n["node_id"] == "p2")
        assert p2["in_degree"] == 1
        p1 = next(n for n in result["nodes"] if n["node_id"] == "p1")
        assert p1["out_degree"] == 1
        assert result["isolated_count"] == 0

    def test_hub_requires_min_in_degree(self, monkeypatch):
        papers = [{"node_id": f"p{i}", "title": f"P{i}", "year": 2020} for i in range(1, 5)]
        _seed_report(papers)
        # p1 cited by p2 and p3 -- meets MIN_IN_DEGREE_FOR_HUB (2); p4 cites nobody
        edges = [
            {"from_node_id": "node:ws:p2", "to_node_id": "node:ws:p1", "relation": "cites"},
            {"from_node_id": "node:ws:p3", "to_node_id": "node:ws:p1", "relation": "cites"},
        ]
        monkeypatch.setattr(citation_graph_builder, "list_edges", lambda ws: edges)

        result = citation_graph_builder.run()
        hub_ids = {h["node_id"] for h in result["hubs"]}
        assert hub_ids == {"p1"}

    def test_hubs_capped_at_max_hubs_and_sorted_descending(self, monkeypatch):
        n_papers = citation_graph_builder.MAX_HUBS + 3
        papers = [{"node_id": f"p{i}", "title": f"P{i}", "year": 2020} for i in range(n_papers)]
        # one extra "citer" paper per hub-candidate, each hub paper cited
        # by a DIFFERENT number of citers (>= MIN_IN_DEGREE_FOR_HUB) so
        # sort order is unambiguous
        citer_papers = [{"node_id": f"c{i}", "title": f"C{i}", "year": 2020} for i in range(n_papers)]
        _seed_report(papers + citer_papers)

        edges = []
        for i, p in enumerate(papers):
            # paper i cited by (i % 5) + 2 distinct citers -> in_degree >= 2
            n_citers = (i % 5) + 2
            for c in range(n_citers):
                edges.append({
                    "from_node_id": f"node:ws:extra_{i}_{c}",
                    "to_node_id": f"node:ws:{p['node_id']}",
                    "relation": "cites",
                })
        monkeypatch.setattr(citation_graph_builder, "list_edges", lambda ws: edges)
        monkeypatch.setattr(citation_graph_builder, "get_node", lambda ws, nid: {"title": nid})

        result = citation_graph_builder.run()
        assert len(result["hubs"]) == citation_graph_builder.MAX_HUBS
        in_degrees = [h["in_degree"] for h in result["hubs"]]
        assert in_degrees == sorted(in_degrees, reverse=True)

    def test_edge_referencing_paper_outside_report_is_resolved_via_get_node(self, monkeypatch):
        _seed_report([{"node_id": "p1", "title": "Paper One", "year": 2020}])
        edges = [{"from_node_id": "node:ws:p1", "to_node_id": "node:ws:p_old", "relation": "cites"}]
        monkeypatch.setattr(citation_graph_builder, "list_edges", lambda ws: edges)

        resolved_calls = []

        def fake_get_node(workspace_id, node_id):
            resolved_calls.append(node_id)
            return {"title": "An Older Paper"}

        monkeypatch.setattr(citation_graph_builder, "get_node", fake_get_node)
        result = citation_graph_builder.run()
        assert "p_old" in resolved_calls
        old_paper = next(n for n in result["nodes"] if n["node_id"] == "p_old")
        assert old_paper["title"] == "An Older Paper"
        assert old_paper["year"] is None  # resolved-on-demand papers have no year info

    def test_result_written_to_citation_graph_key(self, monkeypatch):
        _seed_report([{"node_id": "p1", "title": "Paper One", "year": 2020}])
        monkeypatch.setattr(citation_graph_builder, "list_edges", lambda ws: [])
        result = citation_graph_builder.run()
        assert read(KEYS["citation_graph"]) == result

    def test_stage_output_written_only_when_session_id_given(self, monkeypatch):
        _seed_report([{"node_id": "p1", "title": "Paper One", "year": 2020}])
        monkeypatch.setattr(citation_graph_builder, "list_edges", lambda ws: [])

        assert read("stage_output:s1:citation_graph_builder") is None
        citation_graph_builder.run()  # no session_id -- must not write anything for s1
        assert read("stage_output:s1:citation_graph_builder") is None

        citation_graph_builder.run(session_id="s1")
        stage_text = read("stage_output:s1:citation_graph_builder")
        assert stage_text is not None
        assert "paper(s)" in stage_text


# ---------------------------------------------------------------------------
# 2. _render_graph_svg() / _svg_to_data_uri()
# ---------------------------------------------------------------------------

class TestRenderGraphSvg:
    def test_no_nodes_returns_none(self):
        assert citation_graph_builder._render_graph_svg([], []) is None

    def test_too_many_nodes_returns_none(self):
        nodes = [{"node_id": f"p{i}", "title": f"P{i}", "in_degree": 0} for i in range(citation_graph_builder.MAX_GRAPH_IMAGE_NODES + 1)]
        assert citation_graph_builder._render_graph_svg(nodes, []) is None

    def test_within_cap_returns_svg_string(self):
        nodes = [
            {"node_id": "p1", "title": "Paper One", "in_degree": 0},
            {"node_id": "p2", "title": "Paper Two", "in_degree": 3},
        ]
        edges = [{"from_node_id": "p1", "to_node_id": "p2"}]
        svg = citation_graph_builder._render_graph_svg(nodes, edges)
        assert svg.startswith("<svg")
        assert svg.endswith("</svg>")
        assert "<line" in svg  # the one edge

    def test_hub_node_gets_a_text_label_non_hub_does_not(self):
        # Every node gets an SVG <title> tooltip regardless of hub status
        # (module comment: "labels only on hubs" refers to the visible
        # <text> element, not the always-present tooltip).
        nodes = [
            {"node_id": "p1", "title": "Low Degree", "in_degree": 0},
            {"node_id": "p2", "title": "High Degree Hub", "in_degree": citation_graph_builder.MIN_IN_DEGREE_FOR_HUB},
        ]
        svg = citation_graph_builder._render_graph_svg(nodes, [])
        assert "<text" in svg
        assert svg.count("<title>") == 2  # both nodes get a tooltip
        assert "High Degree Hub" in svg.split("<text", 1)[1]  # only the hub reaches the <text> block
        assert "Low Degree" not in svg.split("<text", 1)[1]

    def test_edge_referencing_unknown_node_id_is_skipped_without_crashing(self):
        nodes = [{"node_id": "p1", "title": "P1", "in_degree": 0}]
        edges = [{"from_node_id": "p1", "to_node_id": "does-not-exist"}]
        svg = citation_graph_builder._render_graph_svg(nodes, edges)
        assert svg is not None
        assert "<line" not in svg

    def test_svg_to_data_uri_shape(self):
        uri = citation_graph_builder._svg_to_data_uri("<svg></svg>")
        assert uri.startswith("data:image/svg+xml;base64,")


# ---------------------------------------------------------------------------
# 3. small helpers
# ---------------------------------------------------------------------------

class TestSmallHelpers:
    def test_truncate_title_under_limit_unchanged(self):
        assert citation_graph_builder._truncate_title("Short") == "Short"

    def test_truncate_title_over_limit_gets_ellipsis(self):
        long_title = "x" * 50
        result = citation_graph_builder._truncate_title(long_title, max_len=10)
        assert len(result) == 10
        assert result.endswith("…")

    def test_truncate_title_none_defaults_to_untitled(self):
        assert citation_graph_builder._truncate_title(None) == "Untitled"

    def test_escape_xml_escapes_special_characters(self):
        assert citation_graph_builder._escape_xml('<a> & "b"') == "&lt;a&gt; &amp; &quot;b&quot;"

    def test_bare_node_id_strips_workspace_prefix(self):
        assert citation_graph_builder._bare_node_id("node:ws-1:p1") == "p1"

    def test_bare_node_id_passes_through_falsy(self):
        assert citation_graph_builder._bare_node_id("") == ""
        assert citation_graph_builder._bare_node_id(None) is None
