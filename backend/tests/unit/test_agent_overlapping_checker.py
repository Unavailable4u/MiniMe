"""
tests/unit/test_agent_overlapping_checker.py — Patch 7f-3.

Covers agents/overlapping_checker.py's check_topic()/check_batch():
embed a new topic, query the shared Vector index, cheap-similarity
pre-filter candidates, then either tag directly (below LOW_THRESHOLD:
"new"; above HIGH_THRESHOLD: "duplicate") or LLM-arbitrate the
ambiguous band. Always upserts the topic's own embedding, and never
raises on embed/query/upsert failure (module docstring: "an upload
succeeding shouldn't fail... because the overlap pass couldn't run").

generate_text isn't called here at all -- the LLM path goes through
agents.generic_worker.run() (deferred import), which this file
patches directly rather than via mock_llm.
"""
from unittest.mock import MagicMock

import pytest

from agents import overlapping_checker


class _Match:
    def __init__(self, metadata):
        self.metadata = metadata


@pytest.fixture
def fake_vector_index(monkeypatch):
    index = MagicMock()
    index.query.return_value = []
    monkeypatch.setattr(overlapping_checker, "vector_index", lambda: index)
    return index


@pytest.fixture(autouse=True)
def _fake_embed(monkeypatch):
    monkeypatch.setattr(overlapping_checker, "embed_text", lambda text: [0.1, 0.2, 0.3])


@pytest.fixture(autouse=True)
def _fake_log_usage(monkeypatch):
    monkeypatch.setattr(overlapping_checker, "log_usage", lambda *a, **k: None)


# ---------------------------------------------------------------------------
# 1. No candidates -> "new" without any LLM call
# ---------------------------------------------------------------------------
class TestNoCandidates:
    def test_no_matches_at_all_tags_new(self, fake_vector_index):
        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result == {"tag": "new", "target_topic_id": None}

    def test_matches_below_low_threshold_are_filtered_out_tags_new(self, fake_vector_index, monkeypatch):
        fake_vector_index.query.return_value = [
            _Match({"topic_id": "t2", "name": "Totally Different", "summary": "unrelated content entirely"}),
        ]
        monkeypatch.setattr(overlapping_checker, "similarity", lambda *a, **k: 0.1)
        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result["tag"] == "new"

    def test_self_match_is_excluded_from_candidates(self, fake_vector_index, monkeypatch):
        fake_vector_index.query.return_value = [
            _Match({"topic_id": "t1", "name": "Topic A", "summary": "summary A"}),
        ]
        monkeypatch.setattr(overlapping_checker, "similarity", lambda *a, **k: 0.99)
        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result["tag"] == "new"


# ---------------------------------------------------------------------------
# 2. High-confidence direct tagging (no LLM call)
# ---------------------------------------------------------------------------
class TestDirectTagging:
    def test_score_above_high_threshold_tags_duplicate_without_llm_call(self, fake_vector_index, monkeypatch):
        fake_vector_index.query.return_value = [
            _Match({"topic_id": "t2", "name": "Topic A dup", "summary": "summary A restated"}),
        ]
        monkeypatch.setattr(overlapping_checker, "similarity", lambda *a, **k: 0.95)

        called = []
        monkeypatch.setattr(overlapping_checker, "_llm_arbitrate", lambda *a, **k: called.append(1))

        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result == {"tag": "duplicate", "target_topic_id": "t2"}
        assert called == []

    def test_score_in_ambiguous_band_calls_llm_arbitrate(self, fake_vector_index, monkeypatch):
        fake_vector_index.query.return_value = [
            _Match({"topic_id": "t2", "name": "Related Topic", "summary": "somewhat related summary"}),
        ]
        monkeypatch.setattr(overlapping_checker, "similarity", lambda *a, **k: 0.7)
        monkeypatch.setattr(
            overlapping_checker, "_llm_arbitrate",
            lambda new_topic, candidates, session_id=None: {"tag": "merge", "target_topic_id": "t2"},
        )
        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result == {"tag": "merge", "target_topic_id": "t2"}

    def test_highest_scoring_candidate_is_the_one_tagged_duplicate(self, fake_vector_index, monkeypatch):
        fake_vector_index.query.return_value = [
            _Match({"topic_id": "low", "name": "n1", "summary": "s1"}),
            _Match({"topic_id": "high", "name": "n2", "summary": "s2"}),
        ]
        scores = {"low": 0.93, "high": 0.98}

        def _sim(a, b, stopwords):
            for tid, sc in scores.items():
                if tid in b:
                    return sc
            return 0.0

        # similarity() is called with (new_text, candidate_text) -- encode
        # candidate identity into candidate_text via name so this fake can
        # look it up.
        monkeypatch.setattr(
            overlapping_checker, "similarity",
            lambda new_text, cand_text, stopwords: scores["low"] if "n1" in cand_text else scores["high"],
        )
        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result["target_topic_id"] == "high"


# ---------------------------------------------------------------------------
# 3. _llm_arbitrate() parsing
# ---------------------------------------------------------------------------
class TestLlmArbitrateParsing:
    def test_valid_tag_and_target_returned(self, monkeypatch):
        monkeypatch.setattr(overlapping_checker, "_ensure_role_registered", lambda: None)
        fake_run_role = MagicMock(return_value={"text": '{"tag": "merge", "target_topic_id": "t2"}'})
        monkeypatch.setitem(
            __import__("sys").modules, "agents.generic_worker",
            type("M", (), {"run": fake_run_role})(),
        )
        result = overlapping_checker._llm_arbitrate(
            {"name": "A", "summary": "sa"}, [{"topic_id": "t2", "name": "B", "summary": "sb", "score": 0.7}],
        )
        assert result == {"tag": "merge", "target_topic_id": "t2"}

    def test_invalid_tag_value_defaults_to_new(self, monkeypatch):
        monkeypatch.setattr(overlapping_checker, "_ensure_role_registered", lambda: None)
        fake_run_role = MagicMock(return_value={"text": '{"tag": "banana", "target_topic_id": "t2"}'})
        monkeypatch.setitem(
            __import__("sys").modules, "agents.generic_worker",
            type("M", (), {"run": fake_run_role})(),
        )
        result = overlapping_checker._llm_arbitrate(
            {"name": "A", "summary": "sa"}, [{"topic_id": "t2", "name": "B", "summary": "sb", "score": 0.7}],
        )
        assert result == {"tag": "new", "target_topic_id": None}

    def test_malformed_json_defaults_to_new(self, monkeypatch):
        monkeypatch.setattr(overlapping_checker, "_ensure_role_registered", lambda: None)
        fake_run_role = MagicMock(return_value={"text": "not json"})
        monkeypatch.setitem(
            __import__("sys").modules, "agents.generic_worker",
            type("M", (), {"run": fake_run_role})(),
        )
        result = overlapping_checker._llm_arbitrate(
            {"name": "A", "summary": "sa"}, [{"topic_id": "t2", "name": "B", "summary": "sb", "score": 0.7}],
        )
        assert result == {"tag": "new", "target_topic_id": None}


# ---------------------------------------------------------------------------
# 4. Failure resilience -- never raises
# ---------------------------------------------------------------------------
class TestFailureResilience:
    def test_embed_failure_tags_new_and_does_not_raise(self, fake_vector_index, monkeypatch):
        def _raise(text):
            raise RuntimeError("HF down")

        monkeypatch.setattr(overlapping_checker, "embed_text", _raise)
        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result == {"tag": "new", "target_topic_id": None}
        fake_vector_index.upsert.assert_not_called()

    def test_query_failure_falls_through_to_new_but_still_upserts(self, fake_vector_index, monkeypatch):
        fake_vector_index.query.side_effect = RuntimeError("vector db down")
        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result["tag"] == "new"
        fake_vector_index.upsert.assert_called_once()

    def test_upsert_failure_does_not_raise_and_still_returns_result(self, fake_vector_index, monkeypatch):
        fake_vector_index.upsert.side_effect = RuntimeError("upsert failed")
        result = overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        assert result == {"tag": "new", "target_topic_id": None}


# ---------------------------------------------------------------------------
# 5. check_batch()
# ---------------------------------------------------------------------------
class TestCheckBatch:
    def test_returns_one_result_per_topic_keyed_by_topic_id(self, fake_vector_index, monkeypatch):
        topics = [
            {"topic_id": "t1", "name": "Topic A", "summary": "summary A"},
            {"topic_id": "t2", "name": "Topic B", "summary": "summary B"},
        ]
        result = overlapping_checker.check_batch("ws1", topics)
        assert set(result.keys()) == {"t1", "t2"}
        assert all(v["tag"] == "new" for v in result.values())

    def test_each_topic_is_upserted_sequentially(self, fake_vector_index, monkeypatch):
        topics = [
            {"topic_id": "t1", "name": "Topic A", "summary": "summary A"},
            {"topic_id": "t2", "name": "Topic B", "summary": "summary B"},
        ]
        overlapping_checker.check_batch("ws1", topics)
        assert fake_vector_index.upsert.call_count == 2


# ---------------------------------------------------------------------------
# 6. Always upserts the topic's own embedding
# ---------------------------------------------------------------------------
class TestAlwaysUpserts:
    def test_upsert_happens_even_when_tagged_duplicate(self, fake_vector_index, monkeypatch):
        fake_vector_index.query.return_value = [
            _Match({"topic_id": "t2", "name": "dup", "summary": "dup summary"}),
        ]
        monkeypatch.setattr(overlapping_checker, "similarity", lambda *a, **k: 0.95)
        overlapping_checker.check_topic("ws1", "t1", "Topic A", "summary A")
        fake_vector_index.upsert.assert_called_once()
        vectors = fake_vector_index.upsert.call_args.kwargs["vectors"]
        assert vectors[0][0] == "topic:ws1:t1"
