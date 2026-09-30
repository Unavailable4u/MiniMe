"""
tests/unit/test_agent_contradiction_prefilter.py — Patch 7f-3.

Covers agents/contradiction_prefilter.py's deterministic (no-LLM)
narrowing pass, per its own module docstring:

  1. run() requires KEYS["extraction_table"] to have papers -- raises
     MissingDependencyError(required_role="extraction_table_builder")
     otherwise, so eo/executor.py's adaptive-path auto-recovery can hire
     extraction_table_builder and retry.
  2. _polarity(): matches POSITIVE_TERMS/NEGATIVE_TERMS, returns None
     (not a guess) when both or neither match.
  3. _find_candidate_pairs(): only compares papers within the same
     normalized population, requires >= 2 papers in that group, and
     only pairs opposite polarities -- same-polarity and unknown-polarity
     papers never pair up. Papers with no stated population are excluded
     entirely (not flagged as a gap).
  4. _find_candidate_gaps(): skipped below MIN_PAPERS_FOR_GAP_CHECK;
     flags a field missing in > half of papers; flags a
     methodology/population monoculture (single distinct value) only
     when NOT already flagged for missingness (no double-flag).
  5. run()'s edge/node side effects: one "possible_contradiction" edge
     per pair (skipped silently on missing node_id or ValueError from
     create_edge), one "finding" node per gap, and the final bus writes.

generate_text is never called by this module at all -- it's a pure
Python pass over the extraction table, so no mock_llm fixture is needed
anywhere in this file.
"""
import pytest

from agents import contradiction_prefilter
from eo.errors import MissingDependencyError


def _row(title, population, outcome, node_id=None, **extra):
    row = {"title": title, "population": population, "outcome": outcome}
    if node_id is not None:
        row["node_id"] = node_id
    row.update(extra)
    return row


# ---------------------------------------------------------------------------
# 1. run() requires extraction_table papers
# ---------------------------------------------------------------------------
class TestRequiresExtractionTable:
    def test_missing_table_raises_missing_dependency_error(self, fake_bus):
        with pytest.raises(MissingDependencyError) as exc_info:
            contradiction_prefilter.run()
        assert exc_info.value.required_role == "extraction_table_builder"

    def test_empty_papers_list_raises_missing_dependency_error(self, fake_bus, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "read", lambda *a, **k: {"papers": []})
        with pytest.raises(MissingDependencyError):
            contradiction_prefilter.run()

    def test_table_with_no_papers_key_raises(self, fake_bus, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "read", lambda *a, **k: {})
        with pytest.raises(MissingDependencyError):
            contradiction_prefilter.run()


# ---------------------------------------------------------------------------
# 2. _polarity()
# ---------------------------------------------------------------------------
class TestPolarity:
    def test_positive_term_returns_positive(self):
        assert contradiction_prefilter._polarity("showed a significant increase") == "positive"

    def test_negative_term_returns_negative(self):
        assert contradiction_prefilter._polarity("the treatment failed to help") == "negative"

    def test_no_matching_terms_returns_none(self):
        assert contradiction_prefilter._polarity("participants completed a survey") is None

    def test_both_positive_and_negative_terms_returns_none(self):
        # "improv" (positive) and "did not" (negative) both present --
        # genuinely ambiguous, never forced into a verdict.
        assert contradiction_prefilter._polarity("did not improve outcomes") is None

    def test_empty_text_returns_none(self):
        assert contradiction_prefilter._polarity("") is None

    def test_none_text_returns_none(self):
        assert contradiction_prefilter._polarity(None) is None


# ---------------------------------------------------------------------------
# 3. _find_candidate_pairs()
# ---------------------------------------------------------------------------
class TestFindCandidatePairs:
    def test_opposite_polarity_same_population_pairs(self):
        rows = [
            _row("Paper A", "adults with insomnia", "a clear increase in sleep"),
            _row("Paper B", "adults with insomnia", "showed no effect"),
        ]
        pairs = contradiction_prefilter._find_candidate_pairs(rows)
        assert len(pairs) == 1
        assert pairs[0]["population"] == "adults with insomnia"

    def test_same_polarity_same_population_does_not_pair(self):
        rows = [
            _row("Paper A", "adults with insomnia", "a clear increase in sleep"),
            _row("Paper B", "adults with insomnia", "clear improvement observed"),
        ]
        assert contradiction_prefilter._find_candidate_pairs(rows) == []

    def test_different_population_does_not_pair_even_if_opposite(self):
        rows = [
            _row("Paper A", "adults with insomnia", "a clear increase in sleep"),
            _row("Paper B", "children with ADHD", "showed no effect"),
        ]
        assert contradiction_prefilter._find_candidate_pairs(rows) == []

    def test_papers_with_no_population_are_excluded(self):
        rows = [
            _row("Paper A", "", "a clear increase"),
            _row("Paper B", None, "showed no effect"),
        ]
        assert contradiction_prefilter._find_candidate_pairs(rows) == []

    def test_ambiguous_polarity_never_pairs(self):
        rows = [
            _row("Paper A", "adults", "participants completed a survey"),
            _row("Paper B", "adults", "a clear increase"),
        ]
        assert contradiction_prefilter._find_candidate_pairs(rows) == []

    def test_population_normalization_is_case_and_whitespace_insensitive(self):
        rows = [
            _row("Paper A", "  Adults With   Insomnia ", "a clear increase"),
            _row("Paper B", "adults with insomnia", "showed no effect"),
        ]
        pairs = contradiction_prefilter._find_candidate_pairs(rows)
        assert len(pairs) == 1

    def test_single_paper_in_population_group_produces_no_pairs(self):
        rows = [_row("Paper A", "adults", "a clear increase")]
        assert contradiction_prefilter._find_candidate_pairs(rows) == []

    def test_three_papers_same_population_pairs_each_opposite_combination(self):
        rows = [
            _row("Paper A", "adults", "a clear increase"),
            _row("Paper B", "adults", "showed no effect"),
            _row("Paper C", "adults", "clear improvement"),
        ]
        pairs = contradiction_prefilter._find_candidate_pairs(rows)
        # A-vs-B and B-vs-C are opposite polarity; A-vs-C are both positive.
        assert len(pairs) == 2


# ---------------------------------------------------------------------------
# 4. _find_candidate_gaps()
# ---------------------------------------------------------------------------
class TestFindCandidateGaps:
    def test_below_min_papers_returns_no_gaps(self):
        rows = [_row("A", "adults", "increase"), _row("B", "adults", "decrease")]
        assert contradiction_prefilter._find_candidate_gaps(rows) == []

    def test_field_missing_in_majority_is_flagged(self):
        rows = [
            {"title": "A", "population": "adults", "outcome": "increase", "sample_size": 30},
            {"title": "B", "population": "adults", "outcome": "decrease", "sample_size": None},
            {"title": "C", "population": "adults", "outcome": "increase", "sample_size": None},
        ]
        gaps = contradiction_prefilter._find_candidate_gaps(rows)
        fields = [g["field"] for g in gaps]
        assert "sample_size" in fields

    def test_field_present_in_majority_is_not_flagged_missing(self):
        rows = [
            {"title": "A", "population": "adults", "outcome": "increase", "sample_size": 30},
            {"title": "B", "population": "adults", "outcome": "decrease", "sample_size": 40},
            {"title": "C", "population": "adults", "outcome": "increase", "sample_size": None},
        ]
        gaps = contradiction_prefilter._find_candidate_gaps(rows)
        assert "sample_size" not in [g["field"] for g in gaps]

    def test_methodology_monoculture_is_flagged(self):
        rows = [
            {"title": "A", "population": "adults", "outcome": "increase", "methodology": "RCT"},
            {"title": "B", "population": "children", "outcome": "decrease", "methodology": "RCT"},
            {"title": "C", "population": "elderly", "outcome": "increase", "methodology": "RCT"},
        ]
        gaps = contradiction_prefilter._find_candidate_gaps(rows)
        assert "methodology" in [g["field"] for g in gaps]

    def test_methodology_diversity_is_not_flagged(self):
        rows = [
            {"title": "A", "population": "adults", "outcome": "increase", "methodology": "RCT"},
            {"title": "B", "population": "children", "outcome": "decrease", "methodology": "Survey"},
            {"title": "C", "population": "elderly", "outcome": "increase", "methodology": "Cohort"},
        ]
        gaps = contradiction_prefilter._find_candidate_gaps(rows)
        assert "methodology" not in [g["field"] for g in gaps]

    def test_effect_size_monoculture_is_not_a_flagged_field(self):
        # Only "methodology" and "population" get the monoculture check
        # per FIELD_NAMES handling -- effect_size having one distinct
        # value across all papers is not itself flagged as a gap.
        rows = [
            {"title": "A", "population": "adults", "outcome": "increase", "effect_size": "0.5",
             "sample_size": 10, "methodology": "RCT"},
            {"title": "B", "population": "children", "outcome": "decrease", "effect_size": "0.5",
             "sample_size": 20, "methodology": "Survey"},
            {"title": "C", "population": "elderly", "outcome": "increase", "effect_size": "0.5",
             "sample_size": 30, "methodology": "Cohort"},
        ]
        gaps = contradiction_prefilter._find_candidate_gaps(rows)
        assert "effect_size" not in [g["field"] for g in gaps]

    def test_missing_field_is_not_double_flagged_for_monoculture(self):
        # sample_size missing in majority -> flagged for missingness and
        # the loop `continue`s before checking monoculture on the same field.
        rows = [
            {"title": "A", "population": "adults", "outcome": "increase", "sample_size": None},
            {"title": "B", "population": "children", "outcome": "decrease", "sample_size": None},
            {"title": "C", "population": "elderly", "outcome": "increase", "sample_size": 10},
        ]
        gaps = contradiction_prefilter._find_candidate_gaps(rows)
        sample_size_gaps = [g for g in gaps if g["field"] == "sample_size"]
        assert len(sample_size_gaps) == 1
        assert "missing/unstated" in sample_size_gaps[0]["reason"]


# ---------------------------------------------------------------------------
# 5. run()'s side effects: edges, gap nodes, and bus writes
# ---------------------------------------------------------------------------
class TestRunSideEffects:
    def _seed_table(self, monkeypatch, rows):
        monkeypatch.setattr(
            contradiction_prefilter, "read",
            lambda key, **k: {"papers": rows} if key == contradiction_prefilter.KEYS["extraction_table"]
            else None,
        )

    def test_writes_candidates_to_bus(self, fake_bus, monkeypatch):
        rows = [
            _row("A", "adults", "a clear increase", node_id="n1"),
            _row("B", "adults", "showed no effect", node_id="n2"),
        ]
        self._seed_table(monkeypatch, rows)
        monkeypatch.setattr(contradiction_prefilter, "create_edge", lambda *a, **k: {"edge_id": "e1"})
        monkeypatch.setattr(contradiction_prefilter, "write_node", lambda **k: "node_g1")
        writes = {}
        monkeypatch.setattr(contradiction_prefilter, "write", lambda key, val: writes.__setitem__(key, val))

        result = contradiction_prefilter.run()

        assert writes[contradiction_prefilter.KEYS["contradiction_candidates"]] == result
        assert len(result["candidate_pairs"]) == 1
        assert result["edges_written"] == 1

    def test_missing_node_id_skips_edge_creation(self, fake_bus, monkeypatch):
        rows = [
            _row("A", "adults", "a clear increase"),   # no node_id
            _row("B", "adults", "showed no effect", node_id="n2"),
        ]
        self._seed_table(monkeypatch, rows)
        calls = []
        monkeypatch.setattr(contradiction_prefilter, "create_edge",
                             lambda *a, **k: calls.append(1) or {"edge_id": "e1"})
        monkeypatch.setattr(contradiction_prefilter, "write_node", lambda **k: "node_g1")
        monkeypatch.setattr(contradiction_prefilter, "write", lambda *a, **k: None)

        result = contradiction_prefilter.run()
        assert calls == []
        assert result["edges_written"] == 0

    def test_create_edge_value_error_is_swallowed(self, fake_bus, monkeypatch):
        rows = [
            _row("A", "adults", "a clear increase", node_id="n1"),
            _row("B", "adults", "showed no effect", node_id="n2"),
        ]
        self._seed_table(monkeypatch, rows)

        def _raise(*a, **k):
            raise ValueError("cannot create edge across workspaces")

        monkeypatch.setattr(contradiction_prefilter, "create_edge", _raise)
        monkeypatch.setattr(contradiction_prefilter, "write_node", lambda **k: "node_g1")
        monkeypatch.setattr(contradiction_prefilter, "write", lambda *a, **k: None)

        result = contradiction_prefilter.run()
        assert result["edges_written"] == 0

    def test_gap_nodes_written_one_per_gap(self, fake_bus, monkeypatch):
        rows = [
            {"title": "A", "population": "adults", "outcome": "increase", "sample_size": None},
            {"title": "B", "population": "children", "outcome": "decrease", "sample_size": None},
            {"title": "C", "population": "elderly", "outcome": "increase", "sample_size": 5},
        ]
        self._seed_table(monkeypatch, rows)
        monkeypatch.setattr(contradiction_prefilter, "create_edge", lambda *a, **k: {"edge_id": "e1"})
        node_calls = []
        monkeypatch.setattr(
            contradiction_prefilter, "write_node",
            lambda **k: node_calls.append(k) or f"node_{len(node_calls)}",
        )
        monkeypatch.setattr(contradiction_prefilter, "write", lambda *a, **k: None)

        result = contradiction_prefilter.run()
        assert len(node_calls) == len(result["candidate_gaps"])
        assert result["gap_node_ids"] == [f"node_{i+1}" for i in range(len(node_calls))]

    def test_write_node_returning_none_is_not_added_to_gap_node_ids(self, fake_bus, monkeypatch):
        rows = [
            {"title": "A", "population": "adults", "outcome": "increase", "sample_size": None},
            {"title": "B", "population": "children", "outcome": "decrease", "sample_size": None},
            {"title": "C", "population": "elderly", "outcome": "increase", "sample_size": 5},
        ]
        self._seed_table(monkeypatch, rows)
        monkeypatch.setattr(contradiction_prefilter, "create_edge", lambda *a, **k: {"edge_id": "e1"})
        monkeypatch.setattr(contradiction_prefilter, "write_node", lambda **k: None)
        monkeypatch.setattr(contradiction_prefilter, "write", lambda *a, **k: None)

        result = contradiction_prefilter.run()
        assert result["gap_node_ids"] == []

    def test_session_id_writes_stage_output_summary(self, fake_bus, monkeypatch):
        rows = [
            _row("A", "adults", "a clear increase", node_id="n1"),
            _row("B", "adults", "showed no effect", node_id="n2"),
        ]
        self._seed_table(monkeypatch, rows)
        monkeypatch.setattr(contradiction_prefilter, "create_edge", lambda *a, **k: {"edge_id": "e1"})
        monkeypatch.setattr(contradiction_prefilter, "write_node", lambda **k: "node_g1")
        writes = {}
        monkeypatch.setattr(contradiction_prefilter, "write", lambda key, val: writes.__setitem__(key, val))

        contradiction_prefilter.run(session_id="sess-1")
        assert "stage_output:sess-1:contradiction_prefilter" in writes
        assert isinstance(writes["stage_output:sess-1:contradiction_prefilter"], str)

    def test_no_session_id_does_not_write_stage_output(self, fake_bus, monkeypatch):
        rows = [_row("A", "adults", "a clear increase", node_id="n1")]
        self._seed_table(monkeypatch, rows)
        monkeypatch.setattr(contradiction_prefilter, "create_edge", lambda *a, **k: {"edge_id": "e1"})
        monkeypatch.setattr(contradiction_prefilter, "write_node", lambda **k: "node_g1")
        writes = {}
        monkeypatch.setattr(contradiction_prefilter, "write", lambda key, val: writes.__setitem__(key, val))

        contradiction_prefilter.run()
        assert not any(k.startswith("stage_output:") for k in writes)


# ---------------------------------------------------------------------
# Laya pre-gate (Tier 4 of the Laya migration, 2026-09-29) -- a second,
# optional narrowing pass over the keyword pass's own candidate pairs.
# The suite-wide conftest.py autouse fixture forces
# LAYA_CONTRADICTION_MODE = "off"; each test below opts back in by
# monkeypatching it directly. laya_gate.predict is monkeypatched -- no
# model is loaded.
# ---------------------------------------------------------------------
def _pairs_from(rows):
    return contradiction_prefilter._find_candidate_pairs(rows)


# NOTE: "no significant change" would match BOTH POSITIVE_TERMS
# ("significant") and NEGATIVE_TERMS ("no significant") in
# contradiction_prefilter.py's own crude keyword lists, making
# _polarity() return None (ambiguous) rather than "negative" -- avoided
# here on purpose so this fixture reliably produces exactly two pairs.
TWO_PAIR_ROWS = [
    _row("Paper A", "adults with insomnia", "a clear increase in sleep", node_id="na"),
    _row("Paper B", "adults with insomnia", "showed no effect", node_id="nb"),
    _row("Paper C", "children with adhd", "a clear benefit was observed", node_id="nc"),
    _row("Paper D", "children with adhd", "no association was found", node_id="nd"),
]


def _genuine(p):
    return {"answers": {"genuine": {"type": "noul", "noul": p}}}


class TestLayaGateOff:
    def test_gate_off_returns_pairs_unchanged_and_makes_no_call(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "off")
        calls = []
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: calls.append(1))
        pairs = _pairs_from(TWO_PAIR_ROWS)
        result = contradiction_prefilter._laya_screen_pairs(pairs)
        assert result == pairs
        assert calls == []

    def test_empty_pairs_makes_no_call_even_when_mode_is_on(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "on")
        calls = []
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: calls.append(1))
        assert contradiction_prefilter._laya_screen_pairs([]) == []
        assert calls == []


class TestLayaGateObserve:
    def test_observe_mode_never_drops_a_pair(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "observe")
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: _genuine(0.01))
        pairs = _pairs_from(TWO_PAIR_ROWS)
        result = contradiction_prefilter._laya_screen_pairs(pairs)
        assert len(result) == len(pairs) == 2

    def test_observe_mode_counts_would_drop_but_does_not_drop(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "observe")
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_DROP_THRESHOLD", 0.10)
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: _genuine(0.02))
        pairs = _pairs_from(TWO_PAIR_ROWS)
        result = contradiction_prefilter._laya_screen_pairs(pairs)
        assert len(result) == 2
        stats = contradiction_prefilter.get_contradiction_prefilter_stats()
        assert stats["would_drop"] == 2 and stats["dropped"] == 0

    def test_observe_mode_high_confidence_genuine_is_not_counted_as_would_drop(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "observe")
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: _genuine(0.9))
        pairs = _pairs_from(TWO_PAIR_ROWS)
        contradiction_prefilter._laya_screen_pairs(pairs)
        assert contradiction_prefilter.get_contradiction_prefilter_stats()["would_drop"] == 0


class TestLayaGateOn:
    def test_on_mode_drops_confident_false_positives(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "on")
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_DROP_THRESHOLD", 0.10)
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: _genuine(0.02))
        pairs = _pairs_from(TWO_PAIR_ROWS)
        result = contradiction_prefilter._laya_screen_pairs(pairs)
        assert result == []
        assert contradiction_prefilter.get_contradiction_prefilter_stats()["dropped"] == 2

    def test_on_mode_keeps_pairs_above_the_drop_threshold(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "on")
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_DROP_THRESHOLD", 0.10)
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: _genuine(0.5))
        pairs = _pairs_from(TWO_PAIR_ROWS)
        result = contradiction_prefilter._laya_screen_pairs(pairs)
        assert result == pairs

    def test_on_mode_can_drop_only_some_pairs(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "on")
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_DROP_THRESHOLD", 0.10)
        replies = iter([_genuine(0.02), _genuine(0.8)])   # drop first pair, keep second
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: next(replies))
        pairs = _pairs_from(TWO_PAIR_ROWS)
        result = contradiction_prefilter._laya_screen_pairs(pairs)
        assert len(result) == 1
        assert result[0] == pairs[1]

    def test_on_mode_never_adds_a_pair_the_keyword_pass_did_not_find(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "on")
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: _genuine(0.99))
        assert contradiction_prefilter._laya_screen_pairs([]) == []

    def test_on_mode_keeps_a_pair_when_laya_is_unavailable(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "on")
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: None)
        pairs = _pairs_from(TWO_PAIR_ROWS)
        result = contradiction_prefilter._laya_screen_pairs(pairs)
        assert result == pairs   # fail open to "keep", same as a keyword false negative

    def test_on_mode_keeps_a_pair_when_laya_raises(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "on")

        def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", boom)
        pairs = _pairs_from(TWO_PAIR_ROWS)
        assert contradiction_prefilter._laya_screen_pairs(pairs) == pairs


class TestLayaScreenPairHelper:
    def test_sends_population_and_both_outcomes(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict",
                            lambda state, q, site="u": seen.update(state=state, site=site) or _genuine(0.5))
        pair = _pairs_from(TWO_PAIR_ROWS)[0]
        contradiction_prefilter._laya_screen_pair(pair)
        assert seen["state"]["population"] == "adults with insomnia"
        assert "increase" in seen["state"]["outcome_a"]
        assert "no effect" in seen["state"]["outcome_b"]
        assert seen["site"] == "contradiction_prefilter.screen_pair"

    def test_returns_none_on_malformed_result(self, monkeypatch):
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: {"answers": {}})
        pair = _pairs_from(TWO_PAIR_ROWS)[0]
        assert contradiction_prefilter._laya_screen_pair(pair) is None


class TestRunWiresInTheScreeningPass:
    """run() itself: with the gate off (conftest default), the screening
    pass must be a no-op so run()'s existing behavior (covered by
    TestRunSideEffects above) is unaffected. Same fixture pattern as
    TestRunSideEffects: monkeypatch `read`/`create_edge`/`write_node`/
    `write` directly rather than going through a real bus/graph backend.
    """

    def test_run_pair_count_unaffected_when_gate_is_off(self, fake_bus, monkeypatch):
        rows = [
            _row("A", "adults", "a clear increase", node_id="n1"),
            _row("B", "adults", "showed no effect", node_id="n2"),
        ]
        monkeypatch.setattr(
            contradiction_prefilter, "read",
            lambda key, **k: {"papers": rows} if key == contradiction_prefilter.KEYS["extraction_table"]
            else None,
        )
        monkeypatch.setattr(contradiction_prefilter, "create_edge", lambda *a, **k: {"edge_id": "e1"})
        monkeypatch.setattr(contradiction_prefilter, "write_node", lambda **k: "node_g1")
        monkeypatch.setattr(contradiction_prefilter, "write", lambda *a, **k: None)
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "off")
        calls = []
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: calls.append(1))

        result = contradiction_prefilter.run()

        assert len(result["candidate_pairs"]) == 1
        assert calls == []

    def test_run_drops_a_pair_when_gate_is_on_and_confident(self, fake_bus, monkeypatch):
        rows = [
            _row("A", "adults", "a clear increase", node_id="n1"),
            _row("B", "adults", "showed no effect", node_id="n2"),
        ]
        monkeypatch.setattr(
            contradiction_prefilter, "read",
            lambda key, **k: {"papers": rows} if key == contradiction_prefilter.KEYS["extraction_table"]
            else None,
        )
        monkeypatch.setattr(contradiction_prefilter, "create_edge", lambda *a, **k: {"edge_id": "e1"})
        monkeypatch.setattr(contradiction_prefilter, "write_node", lambda **k: "node_g1")
        monkeypatch.setattr(contradiction_prefilter, "write", lambda *a, **k: None)
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_MODE", "on")
        monkeypatch.setattr(contradiction_prefilter, "LAYA_CONTRADICTION_DROP_THRESHOLD", 0.10)
        monkeypatch.setattr(contradiction_prefilter.laya_gate, "predict", lambda *a, **k: _genuine(0.01))

        result = contradiction_prefilter.run()

        assert result["candidate_pairs"] == []
        assert result["edges_written"] == 0


class TestContradictionPrefilterStats:
    def test_stats_are_zero_with_no_data(self):
        stats = contradiction_prefilter.get_contradiction_prefilter_stats()
        assert stats == {"would_drop": 0, "dropped": 0}
