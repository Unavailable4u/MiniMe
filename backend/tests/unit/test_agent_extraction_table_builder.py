"""
tests/unit/test_agent_extraction_table_builder.py — Patch 7f-4d-2.

Covers agents/extraction_table_builder.py: the MissingDependencyError
raised when academic_search hasn't run, exact-ID dedup (DOI wins, title
fallback, no fuzzy collapsing of distinct papers), the fairness-rotation
worker pool (_eligible_pool / _select_workers, all three key_override
shapes), the per-paper extraction worker (JSON parse, fence stripping,
the RuntimeError/JSONDecodeError/AttributeError -> null-fields-with-
extraction_error degrade), and run()'s keyed-union re-assembly back into
the deduped input's original order regardless of thread completion
order.

generate_text is faked via the shared `mock_llm` fixture (bound-name
import). AGENT_CAPABILITIES and get_quota_snapshot are both bound names
in this module's own namespace (`from eo.registry import
AGENT_CAPABILITIES`, `from eo.quota_sentinel import
get_quota_snapshot`), so they're monkeypatched directly on the module,
same as test_eo_worker_pool.py does for the identical pattern in
eo/worker_pool.py. emit_event is monkeypatched directly too.

Concurrency itself isn't meaningfully testable through a mock (no real
network latency to overlap) -- same position test_code_writers_pool.py
takes for the analogous ThreadPoolExecutor-based pool. This instead
covers the shape/logic contract: one row per deduped paper, in the
deduped input's original order, not as_completed() order.
"""
import json

import pytest

import agents.extraction_table_builder as etb
from eo.errors import MissingDependencyError


@pytest.fixture(autouse=True)
def _fake_emit_event(monkeypatch):
    calls = []
    monkeypatch.setattr(
        etb, "emit_event",
        lambda event, **kwargs: calls.append((event, kwargs)),
    )
    return calls


@pytest.fixture(autouse=True)
def _fixed_pool(monkeypatch):
    """A deterministic 5-account pool with a flat (no-preference) quota
    snapshot, so _select_workers()'s ranking is stable across runs
    without depending on the real registry/quota_sentinel contents."""
    pool = {
        f"FAKE_KEY_{i}": {"provider": "groq", "natural_roles": ["extraction_table_builder"]}
        for i in range(1, 6)
    }
    monkeypatch.setattr(etb, "AGENT_CAPABILITIES", pool)
    monkeypatch.setattr(etb, "get_quota_snapshot", lambda: {})
    return pool


def _report(papers):
    return {"papers": papers}


def _paper(**overrides):
    base = {
        "paper_id": "p1", "node_id": "n1", "title": "A Study of Things",
        "authors": ["A. Author"], "year": 2020, "doi": "10.1/abc",
        "abstract": "We studied 100 things.",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. run(): missing/empty upstream report -> MissingDependencyError
# ---------------------------------------------------------------------------
class TestMissingDependency:
    def test_no_report_at_all_raises_missing_dependency(self, fake_bus, mock_llm):
        with pytest.raises(MissingDependencyError) as exc_info:
            etb.run()
        assert exc_info.value.required_role == "academic_search"
        assert mock_llm.mock.call_count == 0

    def test_report_with_no_papers_key_raises(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        write(KEYS["academic_search_report"], {})
        with pytest.raises(MissingDependencyError):
            etb.run()

    def test_report_with_empty_papers_list_raises(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        write(KEYS["academic_search_report"], _report([]))
        with pytest.raises(MissingDependencyError):
            etb.run()


# ---------------------------------------------------------------------------
# 2. _paper_key(): exact-ID dedup rule (DOI wins, title fallback)
# ---------------------------------------------------------------------------
class TestPaperKey:
    def test_doi_key_is_lowercased(self):
        assert etb._paper_key(_paper(doi="10.1/ABC")) == "doi:10.1/abc"

    def test_falls_back_to_title_when_no_doi(self):
        assert etb._paper_key(_paper(doi=None, title="  My Title  ")) == "title:my title"

    def test_missing_title_and_doi_does_not_raise(self):
        assert etb._paper_key({}) == "title:"


# ---------------------------------------------------------------------------
# 3. _eligible_pool() / _select_workers(): fairness rotation
# ---------------------------------------------------------------------------
class TestSelectWorkers:
    def test_eligible_pool_only_returns_tagged_accounts(self, _fixed_pool, monkeypatch):
        mixed = dict(_fixed_pool)
        mixed["OTHER_KEY"] = {"provider": "groq", "natural_roles": ["verifier"]}
        monkeypatch.setattr(etb, "AGENT_CAPABILITIES", mixed)
        assert "OTHER_KEY" not in etb._eligible_pool()
        assert set(etb._eligible_pool()) == set(_fixed_pool.keys())

    def test_no_eligible_accounts_raises_runtime_error(self, monkeypatch):
        monkeypatch.setattr(etb, "AGENT_CAPABILITIES", {})
        with pytest.raises(RuntimeError, match="no accounts tagged"):
            etb._select_workers(5)

    def test_ranks_by_ascending_quota_pct(self, monkeypatch):
        snapshot = {
            "FAKE_KEY_1": {"pct": 0.9}, "FAKE_KEY_2": {"pct": 0.1},
            "FAKE_KEY_3": {"pct": 0.5}, "FAKE_KEY_4": {"pct": 0.3},
            "FAKE_KEY_5": {"pct": 0.7},
        }
        monkeypatch.setattr(etb, "get_quota_snapshot", lambda: snapshot)
        result = etb._select_workers(3)
        assert result == ["FAKE_KEY_2", "FAKE_KEY_4", "FAKE_KEY_3"]

    def test_missing_snapshot_entries_treated_as_zero_usage(self, monkeypatch):
        monkeypatch.setattr(etb, "get_quota_snapshot", lambda: {"FAKE_KEY_3": {"pct": 0.5}})
        result = etb._select_workers(5)
        assert result[-1] == "FAKE_KEY_3"

    def test_string_key_override_wraps_to_single_item_list(self):
        assert etb._select_workers(5, key_override="SOME_KEY") == ["SOME_KEY"]

    def test_list_key_override_used_as_is(self):
        override = ["KEY_A", "KEY_B"]
        assert etb._select_workers(5, key_override=override) == override


# ---------------------------------------------------------------------------
# 4. run(): dedup + keyed-union re-assembly
# ---------------------------------------------------------------------------
class TestRunDedupAndAssembly:
    def test_exact_doi_duplicate_is_collapsed_to_one_row(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        write(KEYS["academic_search_report"], _report([
            _paper(paper_id="p1", doi="10.1/abc", title="First"),
            _paper(paper_id="p2", doi="10.1/abc", title="First (dup)"),
        ]))
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})

        result = etb.run(key_override="ONE_KEY")

        assert len(result["papers"]) == 1

    def test_distinct_papers_are_never_collapsed(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        write(KEYS["academic_search_report"], _report([
            _paper(paper_id="p1", doi="10.1/aaa", title="One"),
            _paper(paper_id="p2", doi="10.1/bbb", title="Two"),
        ]))
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})

        result = etb.run(key_override="ONE_KEY")

        assert len(result["papers"]) == 2

    def test_rows_preserve_deduped_input_order_not_completion_order(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        papers = [_paper(paper_id=f"p{i}", doi=f"10.1/{i}", title=f"Paper {i}") for i in range(6)]
        write(KEYS["academic_search_report"], _report(papers))
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})

        result = etb.run(key_override="ONE_KEY")

        assert [row["paper_id"] for row in result["papers"]] == [f"p{i}" for i in range(6)]

    def test_row_carries_paper_metadata_plus_extracted_fields(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        write(KEYS["academic_search_report"], _report([
            _paper(paper_id="p1", node_id="n1", title="T", authors=["X"], year=2021, doi="10.1/x"),
        ]))
        mock_llm.set_json_response({
            "sample_size": "n=50", "methodology": "RCT", "population": "adults",
            "outcome": "improved", "effect_size": "d=0.3",
        })

        result = etb.run(key_override="ONE_KEY")

        row = result["papers"][0]
        assert row["paper_id"] == "p1"
        assert row["node_id"] == "n1"
        assert row["authors"] == ["X"]
        assert row["year"] == 2021
        assert row["doi"] == "10.1/x"
        assert row["sample_size"] == "n=50"
        assert row["effect_size"] == "d=0.3"

    def test_field_names_and_summary_shape(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        write(KEYS["academic_search_report"], _report([_paper()]))
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})

        result = etb.run(key_override="ONE_KEY")

        assert result["field_names"] == etb.FIELD_NAMES
        assert "1 paper" in result["summary"]

    def test_result_written_to_bus(self, fake_bus, mock_llm):
        from memory.bus import write, read, KEYS
        write(KEYS["academic_search_report"], _report([_paper()]))
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})

        result = etb.run(key_override="ONE_KEY")

        assert read(KEYS["extraction_table"]) == result

    def test_expanded_flag_does_not_error_with_more_workers_than_papers(self, fake_bus, mock_llm):
        from memory.bus import write, KEYS
        write(KEYS["academic_search_report"], _report([_paper()]))
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})

        result = etb.run(expanded=True, key_override="ONE_KEY")

        assert len(result["papers"]) == 1

    def test_worker_count_over_paper_count_reuses_keys_round_robin(self, fake_bus, mock_llm):
        """More papers than workers is the exact code_writers.py round-robin
        shape this module's docstring says it copies -- confirmed here by
        just checking it completes cleanly with a 2-key pool and 5 papers."""
        from memory.bus import write, KEYS
        papers = [_paper(paper_id=f"p{i}", doi=f"10.1/{i}") for i in range(5)]
        write(KEYS["academic_search_report"], _report(papers))
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})

        result = etb.run(key_override=["KEY_A", "KEY_B"])

        assert len(result["papers"]) == 5


# ---------------------------------------------------------------------------
# 5. _extract_one_paper(): parsing, fence-stripping, degrade path
# ---------------------------------------------------------------------------
class TestExtractOnePaper:
    def test_parses_clean_json_response(self, mock_llm):
        mock_llm.set_json_response({
            "sample_size": "n=10", "methodology": "survey", "population": "students",
            "outcome": "positive", "effect_size": None,
        })
        key, fields = etb._extract_one_paper(_paper(), "SOME_KEY", 1)
        assert fields["sample_size"] == "n=10"
        assert fields["methodology"] == "survey"

    def test_strips_fenced_json_before_parsing(self, mock_llm):
        mock_llm.set_response(
            "```json\n" + json.dumps({name: None for name in etb.FIELD_NAMES}) + "\n```"
        )
        key, fields = etb._extract_one_paper(_paper(), "SOME_KEY", 1)
        assert fields["sample_size"] is None

    def test_extra_keys_in_response_are_dropped(self, mock_llm):
        mock_llm.set_json_response({
            **{name: None for name in etb.FIELD_NAMES}, "confidence": "high",
        })
        key, fields = etb._extract_one_paper(_paper(), "SOME_KEY", 1)
        assert set(fields.keys()) == set(etb.FIELD_NAMES)

    def test_runtime_error_degrades_to_null_fields_with_error_flag(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise RuntimeError("all providers exhausted")
        monkeypatch.setattr(etb, "generate_text", _raise)

        key, fields = etb._extract_one_paper(_paper(), "SOME_KEY", 1)

        assert fields["extraction_error"] is True
        for name in etb.FIELD_NAMES:
            assert fields[name] is None

    def test_unparseable_json_degrades_to_null_fields_with_error_flag(self, mock_llm):
        mock_llm.set_response("not json at all")

        key, fields = etb._extract_one_paper(_paper(), "SOME_KEY", 1)

        assert fields["extraction_error"] is True

    def test_non_dict_json_degrades_to_null_fields_with_error_flag(self, mock_llm):
        # valid JSON (a list), but .get() on it raises AttributeError
        mock_llm.set_response("[1, 2, 3]")

        key, fields = etb._extract_one_paper(_paper(), "SOME_KEY", 1)

        assert fields["extraction_error"] is True

    def test_paper_key_returned_matches_paper_key_helper(self, mock_llm):
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})
        paper = _paper(doi="10.1/xyz")
        key, _ = etb._extract_one_paper(paper, "SOME_KEY", 1)
        assert key == etb._paper_key(paper)

    def test_agent_start_and_done_emitted_around_each_worker(self, mock_llm, _fake_emit_event):
        mock_llm.set_json_response({name: None for name in etb.FIELD_NAMES})
        etb._extract_one_paper(_paper(title="Widget Study"), "SOME_KEY", 3)

        start_event, start_kwargs = _fake_emit_event[0]
        done_event, done_kwargs = _fake_emit_event[1]
        assert start_event == "agent_start"
        assert start_kwargs["agent"] == "extraction_worker_3"
        assert "Widget Study" in start_kwargs["payload"]["label"]
        assert done_event == "agent_done"
        assert "duration_ms" in done_kwargs["payload"]
