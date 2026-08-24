"""
tests/unit/test_agent_source_quality_flagger.py — Patch 7f-4c-2.

Covers agents/source_quality_flagger.py's run(): two independent
deterministic checks over KEYS["academic_search_report"]'s papers --
(1) plain rule-based quality flags (venue, stale zero-citation count,
unreviewed arxiv preprint), each optionally written as a "finding" node
+ "flags" edge back to the paper; (2) a near-duplicate/plagiarism pass
that reuses agents/duplication_checker.py's own SIMILARITY_THRESHOLD
and embedding-index shape, writing a "possible_duplicate_source" edge
per flagged pair -- then writes KEYS["source_quality_report"] and,
when a session_id is given, the module's own
stage_output:{session_id}:source_quality_flagger entry directly.

vector_index()/embed_text()/log_usage()/write_node()/create_edge()/
write()/read() are all faked at the module level (bound-name imports),
same posture test_agent_duplication_checker.py takes with its own
sibling functions -- no real Upstash Vector or knowledge-graph call
under test.
"""
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from agents import source_quality_flagger
from eo.errors import MissingDependencyError


class _Match:
    def __init__(self, score, metadata):
        self.score = score
        self.metadata = metadata


def _paper(paper_id="p1", title="Some Paper", node_id="n1", abstract="an abstract",
           venue="ACM", citation_count=5, year=None, source="semantic_scholar", doi="10.1/x"):
    if year is None:
        year = datetime.now(UTC).year
    return {
        "paper_id": paper_id, "title": title, "node_id": node_id, "abstract": abstract,
        "venue": venue, "citation_count": citation_count, "year": year,
        "source": source, "doi": doi,
    }


@pytest.fixture(autouse=True)
def _fake_app_slug(monkeypatch):
    monkeypatch.setattr(source_quality_flagger, "get_current_app_slug", lambda: "ws-1")


@pytest.fixture
def fake_vector_index(monkeypatch):
    index = MagicMock()
    index.query.return_value = []
    monkeypatch.setattr(source_quality_flagger, "vector_index", lambda: index)
    return index


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    monkeypatch.setattr(source_quality_flagger, "embed_text", lambda text: [0.1, 0.2, 0.3])


@pytest.fixture(autouse=True)
def _fake_write_node(monkeypatch):
    mock = MagicMock(return_value="finding1")
    monkeypatch.setattr(source_quality_flagger, "write_node", mock)
    return mock


@pytest.fixture(autouse=True)
def _fake_create_edge(monkeypatch):
    mock = MagicMock(return_value={"edge_id": "e1"})
    monkeypatch.setattr(source_quality_flagger, "create_edge", mock)
    return mock


def _seed_report(monkeypatch, papers):
    monkeypatch.setattr(
        source_quality_flagger, "read",
        lambda key, default=None: {"papers": papers} if key == source_quality_flagger.KEYS[
            "academic_search_report"] else default,
    )


CURRENT_YEAR = datetime.now(UTC).year


# ---------------------------------------------------------------------------
# 1. Missing/empty prerequisite
# ---------------------------------------------------------------------------
class TestMissingDependency:
    def test_no_report_at_all_raises_missing_dependency(self, fake_bus, monkeypatch, fake_vector_index):
        monkeypatch.setattr(source_quality_flagger, "read", lambda key, default=None: default)
        with pytest.raises(MissingDependencyError) as exc_info:
            source_quality_flagger.run()
        assert exc_info.value.required_role == "academic_search"

    def test_report_with_empty_papers_raises_missing_dependency(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [])
        with pytest.raises(MissingDependencyError):
            source_quality_flagger.run()

    def test_report_with_papers_key_missing_raises_missing_dependency(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        monkeypatch.setattr(
            source_quality_flagger, "read",
            lambda key, default=None: {} if key == source_quality_flagger.KEYS[
                "academic_search_report"] else default,
        )
        with pytest.raises(MissingDependencyError):
            source_quality_flagger.run()


# ---------------------------------------------------------------------------
# 2. _quality_flags(): the three rule checks
# ---------------------------------------------------------------------------
class TestQualityFlagsRules:
    def test_no_venue_is_flagged(self):
        paper = _paper(venue=None)
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert "no publication venue listed" in flags

    def test_venue_present_is_not_flagged_for_venue(self):
        paper = _paper(venue="NeurIPS")
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert not any("venue" in f for f in flags)

    def test_zero_citations_old_enough_is_flagged(self):
        paper = _paper(citation_count=0, year=CURRENT_YEAR - 2, venue="X")
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert any("zero citations" in f for f in flags)

    def test_zero_citations_too_recent_is_not_flagged(self):
        paper = _paper(citation_count=0, year=CURRENT_YEAR, venue="X")
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert not any("zero citations" in f for f in flags)

    def test_zero_citations_exactly_at_min_age_is_flagged(self):
        paper = _paper(
            citation_count=0,
            year=CURRENT_YEAR - source_quality_flagger.MIN_AGE_FOR_ZERO_CITATION_FLAG,
            venue="X",
        )
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert any("zero citations" in f for f in flags)

    def test_nonzero_citations_never_flagged_regardless_of_age(self):
        paper = _paper(citation_count=3, year=2000, venue="X")
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert not any("zero citations" in f for f in flags)

    def test_missing_year_does_not_trigger_zero_citation_flag(self):
        paper = _paper(citation_count=0, year=None, venue="X")
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert not any("zero citations" in f for f in flags)

    def test_unreviewed_arxiv_preprint_is_flagged(self):
        paper = _paper(source="arxiv", doi=None, venue=None)
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert any("unreviewed preprint" in f for f in flags)

    def test_arxiv_with_doi_is_not_flagged_as_preprint(self):
        paper = _paper(source="arxiv", doi="10.1/y", venue=None)
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert not any("unreviewed preprint" in f for f in flags)

    def test_arxiv_with_venue_is_not_flagged_as_preprint(self):
        paper = _paper(source="arxiv", doi=None, venue="ICLR")
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert not any("unreviewed preprint" in f for f in flags)

    def test_non_arxiv_source_never_flagged_as_preprint(self):
        paper = _paper(source="crossref", doi=None, venue=None)
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert not any("unreviewed preprint" in f for f in flags)

    def test_clean_paper_has_no_flags(self):
        paper = _paper(venue="ACM", citation_count=10, year=2015, source="crossref", doi="x")
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert flags == []

    def test_paper_can_accumulate_multiple_flags(self):
        paper = _paper(venue=None, citation_count=0, year=CURRENT_YEAR - 5, source="arxiv", doi=None)
        flags = source_quality_flagger._quality_flags(paper, CURRENT_YEAR)
        assert len(flags) >= 2


# ---------------------------------------------------------------------------
# 3. run(): quality-flag path end to end
# ---------------------------------------------------------------------------
class TestRunQualityFlagPath:
    def test_flagged_paper_appears_in_result(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(venue=None)])
        result = source_quality_flagger.run()
        assert len(result["quality_flags"]) == 1
        assert result["quality_flags"][0]["title"] == "Some Paper"

    def test_clean_paper_does_not_appear_in_quality_flags(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(venue="ACM", citation_count=10, year=2015)])
        result = source_quality_flagger.run()
        assert result["quality_flags"] == []

    def test_flagged_paper_with_node_id_writes_finding_node_and_edge(
        self, fake_bus, monkeypatch, fake_vector_index, _fake_write_node, _fake_create_edge
    ):
        _seed_report(monkeypatch, [_paper(venue=None, node_id="n42")])
        source_quality_flagger.run(session_id="sess1", tier=1)
        _fake_write_node.assert_called_once()
        kwargs = _fake_write_node.call_args.kwargs
        assert kwargs["node_type"] == "finding"
        assert kwargs["tags"] == ["quality_flag"]
        assert kwargs["session_id"] == "sess1"
        assert kwargs["tier"] == 1
        _fake_create_edge.assert_called_once_with(
            "node:ws-1:finding1", "node:ws-1:n42",
            relation="flags", created_by="source_quality_flagger",
        )

    def test_flagged_paper_without_node_id_writes_no_finding(
        self, fake_bus, monkeypatch, fake_vector_index, _fake_write_node, _fake_create_edge
    ):
        _seed_report(monkeypatch, [_paper(venue=None, node_id=None)])
        source_quality_flagger.run()
        _fake_write_node.assert_not_called()
        _fake_create_edge.assert_not_called()

    def test_write_node_returning_none_skips_edge_creation(
        self, fake_bus, monkeypatch, fake_vector_index, _fake_write_node, _fake_create_edge
    ):
        _fake_write_node.return_value = None
        _seed_report(monkeypatch, [_paper(venue=None, node_id="n1")])
        source_quality_flagger.run()
        _fake_create_edge.assert_not_called()

    def test_create_edge_value_error_is_swallowed(
        self, fake_bus, monkeypatch, fake_vector_index, _fake_write_node, _fake_create_edge
    ):
        _fake_create_edge.side_effect = ValueError("dup edge")
        _seed_report(monkeypatch, [_paper(venue=None, node_id="n1")])
        result = source_quality_flagger.run()  # should not raise
        assert len(result["quality_flags"]) == 1


# ---------------------------------------------------------------------------
# 4. run(): near-duplicate path end to end
# ---------------------------------------------------------------------------
class TestRunNearDuplicatePath:
    def test_paper_with_blank_abstract_is_skipped_entirely(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(abstract="   ")])
        result = source_quality_flagger.run()
        assert result["near_duplicates"] == []
        fake_vector_index.upsert.assert_not_called()

    def test_score_above_threshold_flags_near_duplicate(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(paper_id="p1", title="Paper A")])
        fake_vector_index.query.return_value = [
            _Match(0.95, {"paper_id": "p2", "title": "Paper B", "node_id": "n2"}),
        ]
        result = source_quality_flagger.run()
        assert len(result["near_duplicates"]) == 1
        dup = result["near_duplicates"][0]
        assert dup["paper_a"] == "Paper A"
        assert dup["paper_b"] == "Paper B"
        assert dup["score"] == 0.95

    def test_score_below_threshold_not_flagged(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(paper_id="p1")])
        fake_vector_index.query.return_value = [
            _Match(0.5, {"paper_id": "p2", "title": "Other", "node_id": "n2"}),
        ]
        result = source_quality_flagger.run()
        assert result["near_duplicates"] == []

    def test_self_match_is_never_flagged(self, fake_bus, monkeypatch, fake_vector_index):
        _seed_report(monkeypatch, [_paper(paper_id="p1", title="Paper A")])
        fake_vector_index.query.return_value = [
            _Match(0.99, {"paper_id": "p1", "title": "Paper A", "node_id": "n1"}),
        ]
        result = source_quality_flagger.run()
        assert result["near_duplicates"] == []

    def test_only_one_flag_per_paper_even_with_multiple_matches(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(paper_id="p1")])
        fake_vector_index.query.return_value = [
            _Match(0.95, {"paper_id": "p2", "title": "B", "node_id": "n2"}),
            _Match(0.96, {"paper_id": "p3", "title": "C", "node_id": "n3"}),
        ]
        result = source_quality_flagger.run()
        assert len(result["near_duplicates"]) == 1
        assert result["near_duplicates"][0]["paper_b"] == "B"

    def test_near_duplicate_edge_written_when_both_node_ids_present(
        self, fake_bus, monkeypatch, fake_vector_index, _fake_create_edge
    ):
        _seed_report(monkeypatch, [_paper(paper_id="p1", node_id="n1")])
        fake_vector_index.query.return_value = [
            _Match(0.95, {"paper_id": "p2", "title": "B", "node_id": "n2"}),
        ]
        source_quality_flagger.run()
        _fake_create_edge.assert_called_once_with(
            "node:ws-1:n1", "node:ws-1:n2",
            relation="possible_duplicate_source", created_by="source_quality_flagger",
        )

    def test_near_duplicate_edge_skipped_when_a_node_id_missing(
        self, fake_bus, monkeypatch, fake_vector_index, _fake_create_edge
    ):
        _seed_report(monkeypatch, [_paper(paper_id="p1", node_id=None)])
        fake_vector_index.query.return_value = [
            _Match(0.95, {"paper_id": "p2", "title": "B", "node_id": "n2"}),
        ]
        source_quality_flagger.run()
        _fake_create_edge.assert_not_called()

    def test_missing_title_in_match_metadata_defaults_to_unknown(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(paper_id="p1")])
        fake_vector_index.query.return_value = [
            _Match(0.95, {"paper_id": "p2", "node_id": "n2"}),
        ]
        result = source_quality_flagger.run()
        assert result["near_duplicates"][0]["paper_b"] == "unknown"

    def test_score_rounded_to_4_decimal_places(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(paper_id="p1")])
        fake_vector_index.query.return_value = [
            _Match(0.999999, {"paper_id": "p2", "title": "B", "node_id": "n2"}),
        ]
        result = source_quality_flagger.run()
        assert result["near_duplicates"][0]["score"] == 1.0

    def test_abstract_truncated_before_embedding(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        captured = {}
        monkeypatch.setattr(
            source_quality_flagger, "embed_text",
            lambda text: captured.setdefault("len", len(text)) or [0.1],
        )
        _seed_report(monkeypatch, [_paper(abstract="x" * 10000)])
        source_quality_flagger.run()
        assert captured["len"] == 4000


# ---------------------------------------------------------------------------
# 5. Upsert behavior + resilience
# ---------------------------------------------------------------------------
class TestUpsertAndResilience:
    def test_every_embedded_paper_is_upserted(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [
            _paper(paper_id="p1", title="A"), _paper(paper_id="p2", title="B"),
        ])
        source_quality_flagger.run()
        fake_vector_index.upsert.assert_called_once()
        vectors = fake_vector_index.upsert.call_args.kwargs["vectors"]
        ids = [v[0] for v in vectors]
        assert ids == ["sourcetext:ws-1:p1", "sourcetext:ws-1:p2"]

    def test_upsert_metadata_includes_workspace_paper_title_and_node(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(paper_id="p1", title="A", node_id="n1")])
        source_quality_flagger.run()
        vectors = fake_vector_index.upsert.call_args.kwargs["vectors"]
        _, _, meta = vectors[0]
        assert meta == {
            "workspace_id": "ws-1", "paper_id": "p1", "title": "A", "node_id": "n1",
        }

    def test_embed_failure_for_one_paper_skips_it_but_continues(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        def _embed(text):
            if "bad" in text:
                raise RuntimeError("HF down")
            return [0.1, 0.2]

        monkeypatch.setattr(source_quality_flagger, "embed_text", _embed)
        _seed_report(monkeypatch, [
            _paper(paper_id="p_bad", abstract="bad abstract"),
            _paper(paper_id="p_good", abstract="good abstract"),
        ])
        source_quality_flagger.run()
        vectors = fake_vector_index.upsert.call_args.kwargs["vectors"]
        ids = [v[0] for v in vectors]
        assert "sourcetext:ws-1:p_bad" not in ids
        assert "sourcetext:ws-1:p_good" in ids

    def test_query_failure_does_not_crash_and_still_upserts(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        fake_vector_index.query.side_effect = RuntimeError("vector db down")
        _seed_report(monkeypatch, [_paper(paper_id="p1")])
        result = source_quality_flagger.run()
        assert result["near_duplicates"] == []
        fake_vector_index.upsert.assert_called_once()

    def test_upsert_failure_does_not_raise(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        fake_vector_index.upsert.side_effect = RuntimeError("upsert failed")
        _seed_report(monkeypatch, [_paper(paper_id="p1")])
        result = source_quality_flagger.run()  # should not raise
        assert result["near_duplicates"] == []

    def test_no_upsert_call_when_nothing_had_a_usable_abstract(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper(abstract=""), _paper(abstract="   ")])
        source_quality_flagger.run()
        fake_vector_index.upsert.assert_not_called()


# ---------------------------------------------------------------------------
# 6. Bus writes + usage logging
# ---------------------------------------------------------------------------
class TestBusWriteAndLogging:
    def test_writes_report_to_source_quality_report_key(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper()])
        writes = {}
        monkeypatch.setattr(
            source_quality_flagger, "write",
            lambda key, val: writes.__setitem__(key, val),
        )
        result = source_quality_flagger.run()
        assert writes[source_quality_flagger.KEYS["source_quality_report"]] == result

    def test_stage_output_written_only_when_session_id_given(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper()])
        writes = {}
        monkeypatch.setattr(
            source_quality_flagger, "write",
            lambda key, val: writes.__setitem__(key, val),
        )
        source_quality_flagger.run(session_id="sess-9")
        assert "stage_output:sess-9:source_quality_flagger" in writes

    def test_no_stage_output_written_without_session_id(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        _seed_report(monkeypatch, [_paper()])
        writes = {}
        monkeypatch.setattr(
            source_quality_flagger, "write",
            lambda key, val: writes.__setitem__(key, val),
        )
        source_quality_flagger.run()
        assert not any(k.startswith("stage_output:") for k in writes)

    def test_result_summary_counts_match(self, fake_bus, monkeypatch, fake_vector_index):
        _seed_report(monkeypatch, [_paper(venue=None, paper_id="p1")])
        fake_vector_index.query.return_value = [
            _Match(0.95, {"paper_id": "p2", "title": "B", "node_id": "n2"}),
        ]
        result = source_quality_flagger.run()
        assert result["summary"] == "1 quality flag(s), 1 near-duplicate pair(s)."

    def test_log_usage_called_with_huggingface_provider(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        logged = []
        monkeypatch.setattr(
            source_quality_flagger, "log_usage",
            lambda provider, key_env, *a, **k: logged.append((provider, key_env)),
        )
        _seed_report(monkeypatch, [_paper(paper_id="p1")])
        source_quality_flagger.run(session_id="sess-1", domain="research")
        assert logged == [("huggingface", "HUGGINGFACE_API_KEY")]

    def test_log_usage_not_called_when_no_paper_has_an_abstract(
        self, fake_bus, monkeypatch, fake_vector_index
    ):
        logged = []
        monkeypatch.setattr(
            source_quality_flagger, "log_usage",
            lambda *a, **k: logged.append(1),
        )
        _seed_report(monkeypatch, [_paper(abstract="")])
        source_quality_flagger.run()
        assert logged == []


# ---------------------------------------------------------------------------
# 7. _format_summary(): human-readable text
# ---------------------------------------------------------------------------
class TestFormatSummary:
    def test_no_flags_and_no_duplicates(self):
        text = source_quality_flagger._format_summary([], [])
        assert "No quality flags found" in text
        assert "No near-duplicate sources found" in text

    def test_quality_flags_listed_with_title_and_reasons(self):
        text = source_quality_flagger._format_summary(
            [{"title": "My Paper", "flags": ["no publication venue listed"]}], [],
        )
        assert "My Paper" in text
        assert "no publication venue listed" in text

    def test_near_duplicates_listed_with_titles_and_score(self):
        text = source_quality_flagger._format_summary(
            [], [{"paper_a": "A", "paper_b": "B", "score": 0.93}],
        )
        assert "A" in text and "B" in text
        assert "0.93" in text

    def test_always_includes_heuristic_disclaimer(self):
        text = source_quality_flagger._format_summary([], [])
        assert "not confirmed verdicts" in text


# ---------------------------------------------------------------------------
# 8. _workspace_id(): app-slug-first fallback
# ---------------------------------------------------------------------------
class TestWorkspaceId:
    def test_uses_app_slug_when_present(self, fake_bus, monkeypatch):
        monkeypatch.setattr(source_quality_flagger, "get_current_app_slug", lambda: "slug-1")
        assert source_quality_flagger._workspace_id() == "slug-1"

    def test_falls_back_to_original_idea_when_no_app_slug(self, fake_bus, monkeypatch):
        monkeypatch.setattr(source_quality_flagger, "get_current_app_slug", lambda: None)
        monkeypatch.setattr(
            source_quality_flagger, "read",
            lambda key, default=None: "my idea" if key == source_quality_flagger.KEYS[
                "original_idea"] else default,
        )
        assert source_quality_flagger._workspace_id() == "my idea"

    def test_falls_back_to_untitled_when_neither_present(self, fake_bus, monkeypatch):
        monkeypatch.setattr(source_quality_flagger, "get_current_app_slug", lambda: None)
        monkeypatch.setattr(source_quality_flagger, "read", lambda key, default=None: default)
        assert source_quality_flagger._workspace_id() == "untitled"
