"""
tests/unit/test_agent_source_ingestor.py — Patch 7f-4b-2.

Covers agents/source_ingestor.py: write_ingested_source() (the single
"write ingested source as node(s)" step every Capture ingestor feeds
into) and ingest_pdf_to_graph() (its parse+write convenience wrapper).

eo.knowledge_graph.write_node and eo.graph_edges.create_edge are
patched directly on agents.source_ingestor (the module holds bound
references via `from ... import ...`, same reasoning tests/conftest.py
gives for generate_text) rather than on their origin modules, so every
test controls node persistence/edge creation without touching real
Upstash Vector or the fake bus.
"""
from unittest.mock import MagicMock, call

import pytest

import agents.source_ingestor as source_ingestor


def _artifact(title="Doc Title", sections=None, tags=None):
    a = {"title": title, "sections": sections if sections is not None else []}
    if tags is not None:
        a["metadata"] = {"tags": tags}
    return a


def _section(content="body text", heading=None):
    s = {"content": content}
    if heading is not None:
        s["heading"] = heading
    return s


@pytest.fixture
def fake_write_node(monkeypatch):
    """Defaults to returning a fresh incrementing node_id per call so
    multi-section tests can tell nodes apart without each test wiring
    its own side_effect."""
    counter = {"n": 0}

    def _default(*args, **kwargs):
        counter["n"] += 1
        return f"node{counter['n']}"

    mock = MagicMock(side_effect=_default)
    monkeypatch.setattr(source_ingestor, "write_node", mock)
    return mock


@pytest.fixture
def fake_create_edge(monkeypatch):
    mock = MagicMock(return_value={"edge_id": "edge_1"})
    monkeypatch.setattr(source_ingestor, "create_edge", mock)
    return mock


# ---------------------------------------------------------------------------
# 1. Section filtering — empty/whitespace-only sections dropped up front
# ---------------------------------------------------------------------------
class TestSectionFiltering:
    def test_no_sections_key_writes_single_empty_node(self, fake_write_node, fake_create_edge):
        result = source_ingestor.write_ingested_source(
            _artifact(sections=None), "ws1", "user1"
        )
        assert result == ["node1"]
        assert fake_write_node.call_args.kwargs["content"] == ""
        fake_create_edge.assert_not_called()

    def test_all_whitespace_sections_filtered_to_single_empty_node(
        self, fake_write_node, fake_create_edge
    ):
        artifact = _artifact(sections=[_section(content="   "), _section(content="")])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == ["node1"]
        assert fake_write_node.call_args.kwargs["content"] == ""

    def test_mixed_empty_and_real_sections_only_real_ones_kept(
        self, fake_write_node, fake_create_edge
    ):
        artifact = _artifact(sections=[
            _section(content="  "),
            _section(content="real one", heading="H1"),
            _section(content="real two", heading="H2"),
        ])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == ["node1", "node2"]
        assert fake_write_node.call_count == 2


# ---------------------------------------------------------------------------
# 2. Single-section path — one node, no edges, no split
# ---------------------------------------------------------------------------
class TestSingleSection:
    def test_single_section_writes_exactly_one_node(self, fake_write_node, fake_create_edge):
        artifact = _artifact(title="My Doc", sections=[_section(content="only section")])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == ["node1"]
        kwargs = fake_write_node.call_args.kwargs
        assert kwargs["title"] == "My Doc"
        assert kwargs["content"] == "only section"
        assert kwargs["workspace_id"] == "ws1"
        assert kwargs["created_by"] == "user1"
        fake_create_edge.assert_not_called()

    def test_single_section_no_edge_even_if_it_had_a_heading(
        self, fake_write_node, fake_create_edge
    ):
        artifact = _artifact(sections=[_section(content="text", heading="Ignored")])
        source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        # title stays the artifact title, not "title — heading", since the
        # heading-suffix behavior is only for the multi-section split path
        assert fake_write_node.call_args.kwargs["title"] == "Doc Title"

    def test_single_section_write_node_failure_returns_empty_list(
        self, fake_write_node, fake_create_edge
    ):
        fake_write_node.side_effect = None
        fake_write_node.return_value = None
        artifact = _artifact(sections=[_section(content="text")])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == []
        fake_create_edge.assert_not_called()

    def test_default_title_used_when_missing(self, fake_write_node, fake_create_edge):
        artifact = {"sections": [_section(content="text")]}
        source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert fake_write_node.call_args.kwargs["title"] == "Untitled"

    def test_default_section_and_tags_and_session_id(self, fake_write_node, fake_create_edge):
        artifact = _artifact(sections=[_section(content="text")])
        source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        kwargs = fake_write_node.call_args.kwargs
        assert kwargs["section"] == "notes"
        assert kwargs["tags"] == []
        assert kwargs["session_id"] is None

    def test_custom_section_tags_and_session_id_forwarded(self, fake_write_node, fake_create_edge):
        artifact = _artifact(sections=[_section(content="text")], tags=["a", "b"])
        source_ingestor.write_ingested_source(
            artifact, "ws1", "user1", section="pdfs", session_id="sess1"
        )
        kwargs = fake_write_node.call_args.kwargs
        assert kwargs["section"] == "pdfs"
        assert kwargs["tags"] == ["a", "b"]
        assert kwargs["session_id"] == "sess1"

    def test_metadata_present_but_tags_missing_defaults_to_empty_list(
        self, fake_write_node, fake_create_edge
    ):
        artifact = {"title": "T", "sections": [_section(content="text")], "metadata": {}}
        source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert fake_write_node.call_args.kwargs["tags"] == []

    def test_metadata_none_does_not_raise(self, fake_write_node, fake_create_edge):
        artifact = {"title": "T", "sections": [_section(content="text")], "metadata": None}
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == ["node1"]
        assert fake_write_node.call_args.kwargs["tags"] == []


# ---------------------------------------------------------------------------
# 3. Multi-section path — one node per section, headings, chaining edges
# ---------------------------------------------------------------------------
class TestMultiSection:
    def test_two_sections_writes_two_nodes_with_heading_suffixed_titles(
        self, fake_write_node, fake_create_edge
    ):
        artifact = _artifact(title="Report", sections=[
            _section(content="intro", heading="Intro"),
            _section(content="body", heading="Body"),
        ])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == ["node1", "node2"]
        titles = [c.kwargs["title"] for c in fake_write_node.call_args_list]
        assert titles == ["Report — Intro", "Report — Body"]

    def test_section_missing_heading_falls_back_to_artifact_title(
        self, fake_write_node, fake_create_edge
    ):
        artifact = _artifact(title="Report", sections=[
            _section(content="intro", heading="Intro"),
            _section(content="body"),  # no heading
        ])
        source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        titles = [c.kwargs["title"] for c in fake_write_node.call_args_list]
        assert titles[1] == "Report — Report"

    def test_multi_section_chains_every_node_back_to_the_first(
        self, fake_write_node, fake_create_edge
    ):
        artifact = _artifact(sections=[
            _section(content="s1"), _section(content="s2"), _section(content="s3"),
        ])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == ["node1", "node2", "node3"]
        assert fake_create_edge.call_count == 2
        expected_first = "node:ws1:node1"
        for c in fake_create_edge.call_args_list:
            assert c.kwargs["to_node_id"] == expected_first
            assert c.kwargs["relation"] == "same_source"
            assert c.kwargs["created_by"] == "user1"
        from_ids = [c.kwargs["from_node_id"] for c in fake_create_edge.call_args_list]
        assert from_ids == ["node:ws1:node2", "node:ws1:node3"]

    def test_one_failed_write_node_among_several_is_skipped_from_result_and_chain(
        self, fake_write_node, fake_create_edge
    ):
        fake_write_node.side_effect = ["node1", None, "node3"]
        artifact = _artifact(sections=[
            _section(content="s1"), _section(content="s2"), _section(content="s3"),
        ])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == ["node1", "node3"]
        # only one edge: node3 -> node1 (node2 never made it into node_ids)
        fake_create_edge.assert_called_once_with(
            from_node_id="node:ws1:node3",
            to_node_id="node:ws1:node1",
            relation="same_source",
            created_by="user1",
        )

    def test_all_but_one_write_node_fail_no_edge_created(
        self, fake_write_node, fake_create_edge
    ):
        fake_write_node.side_effect = ["node1", None]
        artifact = _artifact(sections=[_section(content="s1"), _section(content="s2")])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == ["node1"]
        fake_create_edge.assert_not_called()

    def test_all_write_node_calls_fail_returns_empty_list_no_edges(
        self, fake_write_node, fake_create_edge
    ):
        fake_write_node.side_effect = [None, None]
        artifact = _artifact(sections=[_section(content="s1"), _section(content="s2")])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        assert result == []
        fake_create_edge.assert_not_called()

    def test_create_edge_exception_is_swallowed_and_other_edges_still_attempted(
        self, fake_write_node, fake_create_edge, capsys
    ):
        fake_create_edge.side_effect = [Exception("boom"), {"edge_id": "edge_2"}]
        artifact = _artifact(sections=[
            _section(content="s1"), _section(content="s2"), _section(content="s3"),
        ])
        result = source_ingestor.write_ingested_source(artifact, "ws1", "user1")
        # node_ids/return value unaffected by an edge failure
        assert result == ["node1", "node2", "node3"]
        assert fake_create_edge.call_count == 2
        out = capsys.readouterr().out
        assert "edge creation skipped" in out
        assert "node2" in out

    def test_multi_section_tags_and_session_id_forwarded_to_every_node(
        self, fake_write_node, fake_create_edge
    ):
        artifact = _artifact(
            sections=[_section(content="s1"), _section(content="s2")], tags=["x"]
        )
        source_ingestor.write_ingested_source(
            artifact, "ws1", "user1", section="clips", session_id="sess9"
        )
        for c in fake_write_node.call_args_list:
            assert c.kwargs["tags"] == ["x"]
            assert c.kwargs["session_id"] == "sess9"
            assert c.kwargs["section"] == "clips"
            assert c.kwargs["workspace_id"] == "ws1"
            assert c.kwargs["created_by"] == "user1"


# ---------------------------------------------------------------------------
# 4. ingest_pdf_to_graph(): parse-then-write convenience wrapper
# ---------------------------------------------------------------------------
class TestIngestPdfToGraph:
    def test_parses_then_writes_with_all_args_forwarded(
        self, fake_write_node, fake_create_edge, monkeypatch
    ):
        fake_artifact = _artifact(title="PDF Doc", sections=[_section(content="page text")])
        fake_ingest_pdf = MagicMock(return_value=fake_artifact)
        import agents.pdf_ingestor as pdf_ingestor
        monkeypatch.setattr(pdf_ingestor, "ingest_pdf", fake_ingest_pdf)

        result = source_ingestor.ingest_pdf_to_graph(
            "/tmp/doc.pdf", "ws1", "user1", section="uploads", session_id="sess5"
        )

        fake_ingest_pdf.assert_called_once_with("/tmp/doc.pdf")
        assert result == ["node1"]
        kwargs = fake_write_node.call_args.kwargs
        assert kwargs["title"] == "PDF Doc"
        assert kwargs["content"] == "page text"
        assert kwargs["section"] == "uploads"
        assert kwargs["session_id"] == "sess5"

    def test_defaults_section_and_session_id_when_omitted(
        self, fake_write_node, fake_create_edge, monkeypatch
    ):
        fake_artifact = _artifact(sections=[_section(content="text")])
        monkeypatch.setattr(
            "agents.pdf_ingestor.ingest_pdf", MagicMock(return_value=fake_artifact)
        )
        source_ingestor.ingest_pdf_to_graph("/tmp/doc.pdf", "ws1", "user1")
        kwargs = fake_write_node.call_args.kwargs
        assert kwargs["section"] == "notes"
        assert kwargs["session_id"] is None

    def test_propagates_file_not_found_from_ingest_pdf(self, fake_write_node, monkeypatch):
        monkeypatch.setattr(
            "agents.pdf_ingestor.ingest_pdf",
            MagicMock(side_effect=FileNotFoundError("/tmp/missing.pdf")),
        )
        with pytest.raises(FileNotFoundError):
            source_ingestor.ingest_pdf_to_graph("/tmp/missing.pdf", "ws1", "user1")
        fake_write_node.assert_not_called()

    def test_multi_section_pdf_artifact_still_splits_and_chains(
        self, fake_write_node, fake_create_edge, monkeypatch
    ):
        fake_artifact = _artifact(title="Multi", sections=[
            _section(content="p1", heading="Page 1"),
            _section(content="p2", heading="Page 2"),
        ])
        monkeypatch.setattr(
            "agents.pdf_ingestor.ingest_pdf", MagicMock(return_value=fake_artifact)
        )
        result = source_ingestor.ingest_pdf_to_graph("/tmp/doc.pdf", "ws1", "user1")
        assert result == ["node1", "node2"]
        fake_create_edge.assert_called_once()
