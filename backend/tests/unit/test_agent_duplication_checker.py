"""
tests/unit/test_agent_duplication_checker.py — Patch 7f-3.

Covers agents/duplication_checker.py's run(): embeds each submitted
module's code (truncated to 4000 chars), queries the shared Vector
index scoped to this app_slug, flags anything scoring >=
SIMILARITY_THRESHOLD (except a self-match in the same cycle), and
always upserts every embedded module regardless of whether it was
flagged.

vector_index() and embed_text_with_fallback() are faked directly
(module-level monkeypatches) rather than through mock_llm, since this
module calls embeddings, not generate_text -- it never imports
generate_text at all.
"""
from unittest.mock import MagicMock

import pytest

import agents.duplication_checker as duplication_checker


class _Match:
    def __init__(self, score, metadata):
        self.score = score
        self.metadata = metadata


def _fake_embed(vector=(0.1, 0.2, 0.3), key_env="HUGGINGFACE_API_KEY_1"):
    return lambda text: (list(vector), key_env)


@pytest.fixture
def fake_vector_index(monkeypatch):
    index = MagicMock()
    index.query.return_value = []
    monkeypatch.setattr(duplication_checker, "vector_index", lambda: index)
    return index


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    monkeypatch.setattr(duplication_checker, "embed_text_with_fallback", _fake_embed())


@pytest.fixture(autouse=True)
def _fake_app_slug(monkeypatch):
    monkeypatch.setattr(duplication_checker, "_app_slug", lambda: "my-app")


def _seed_submitted_code(monkeypatch, submitted, cycle_num=2):
    monkeypatch.setattr(
        duplication_checker, "read_many",
        lambda keys, default=None: {
            duplication_checker.KEYS["submitted_code"]: submitted,
            duplication_checker.KEYS["cycle_count"]: cycle_num,
        },
    )


# ---------------------------------------------------------------------------
# 1. No modules / empty code
# ---------------------------------------------------------------------------
class TestEmptyInput:
    def test_no_submitted_code_returns_empty_flagged(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {})
        result = duplication_checker.run()
        assert result["flagged"] == []
        assert "No likely duplicates" in result["summary"]

    def test_module_with_blank_code_is_skipped_entirely(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {"empty_mod": {"code": "   "}})
        result = duplication_checker.run()
        assert result["flagged"] == []
        fake_vector_index.upsert.assert_not_called()

    def test_cycle_num_defaults_to_1_when_missing(self, fake_bus, fake_vector_index, monkeypatch):
        monkeypatch.setattr(
            duplication_checker, "read_many",
            lambda keys, default=None: {
                duplication_checker.KEYS["submitted_code"]: {},
                duplication_checker.KEYS["cycle_count"]: None,
            },
        )
        # Should not raise, and should not error out on None cycle_num.
        result = duplication_checker.run()
        assert result["flagged"] == []


# ---------------------------------------------------------------------------
# 2. Flagging above threshold
# ---------------------------------------------------------------------------
class TestFlagging:
    def test_score_above_threshold_flags_module(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {"task_validator": {"code": "def validate(x): return x"}})
        fake_vector_index.query.return_value = [
            _Match(0.94, {"module": "validator_service", "cycle_num": 1}),
        ]
        result = duplication_checker.run(session_id="sess-1")
        assert len(result["flagged"]) == 1
        flag = result["flagged"][0]
        assert flag["module"] == "task_validator"
        assert flag["similar_to"] == "validator_service"
        assert flag["cycle"] == 1
        assert flag["score"] == 0.94

    def test_score_below_threshold_does_not_flag(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {"task_validator": {"code": "def validate(x): return x"}})
        fake_vector_index.query.return_value = [
            _Match(0.5, {"module": "validator_service", "cycle_num": 1}),
        ]
        result = duplication_checker.run()
        assert result["flagged"] == []

    def test_self_match_same_cycle_is_never_flagged(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {"task_validator": {"code": "def validate(x): return x"}}, cycle_num=2)
        fake_vector_index.query.return_value = [
            _Match(0.99, {"module": "task_validator", "cycle_num": 2}),
        ]
        result = duplication_checker.run()
        assert result["flagged"] == []

    def test_same_module_different_cycle_can_be_flagged(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {"task_validator": {"code": "def validate(x): return x"}}, cycle_num=2)
        fake_vector_index.query.return_value = [
            _Match(0.99, {"module": "task_validator", "cycle_num": 1}),
        ]
        result = duplication_checker.run()
        assert len(result["flagged"]) == 1
        assert result["flagged"][0]["cycle"] == 1

    def test_only_one_flag_per_module_even_with_multiple_matches_above_threshold(
        self, fake_bus, fake_vector_index, monkeypatch
    ):
        _seed_submitted_code(monkeypatch, {"task_validator": {"code": "def validate(x): return x"}})
        fake_vector_index.query.return_value = [
            _Match(0.95, {"module": "validator_service", "cycle_num": 1}),
            _Match(0.96, {"module": "other_service", "cycle_num": 1}),
        ]
        result = duplication_checker.run()
        assert len(result["flagged"]) == 1
        assert result["flagged"][0]["similar_to"] == "validator_service"

    def test_score_is_rounded_to_4_decimal_places(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {"m": {"code": "x = 1"}})
        fake_vector_index.query.return_value = [
            _Match(0.999999, {"module": "other", "cycle_num": 1}),
        ]
        result = duplication_checker.run()
        assert result["flagged"][0]["score"] == 1.0

    def test_summary_reflects_flagged_count(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {"m": {"code": "x = 1"}})
        fake_vector_index.query.return_value = [
            _Match(0.95, {"module": "other", "cycle_num": 1}),
        ]
        result = duplication_checker.run()
        assert "1 likely-duplicate module(s) found." == result["summary"]


# ---------------------------------------------------------------------------
# 3. Upsert behavior
# ---------------------------------------------------------------------------
class TestUpsert:
    def test_every_embedded_module_is_upserted(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {
            "mod_a": {"code": "def a(): pass"},
            "mod_b": {"code": "def b(): pass"},
        }, cycle_num=3)
        duplication_checker.run()
        fake_vector_index.upsert.assert_called_once()
        vectors = fake_vector_index.upsert.call_args.kwargs["vectors"]
        ids = [v[0] for v in vectors]
        assert ids == ["codechunk:my-app:mod_a:3", "codechunk:my-app:mod_b:3"]

    def test_flagged_module_is_still_upserted(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {"m": {"code": "x = 1"}}, cycle_num=1)
        fake_vector_index.query.return_value = [_Match(0.95, {"module": "other", "cycle_num": 1})]
        duplication_checker.run()
        fake_vector_index.upsert.assert_called_once()

    def test_string_module_data_is_handled(self, fake_bus, fake_vector_index, monkeypatch):
        # module_data can be a raw string instead of {"code": ...}
        _seed_submitted_code(monkeypatch, {"m": "def raw(): pass"}, cycle_num=1)
        result = duplication_checker.run()
        assert result["flagged"] == []
        fake_vector_index.upsert.assert_called_once()


# ---------------------------------------------------------------------------
# 4. Resilience: embed/query/upsert failures never crash run()
# ---------------------------------------------------------------------------
class TestResilience:
    def test_embed_failure_for_one_module_skips_it_but_continues(self, fake_bus, fake_vector_index, monkeypatch):
        def _embed(text):
            if "bad" in text:
                raise RuntimeError("HF down")
            return [0.1, 0.2], "HUGGINGFACE_API_KEY_1"

        monkeypatch.setattr(duplication_checker, "embed_text_with_fallback", _embed)
        _seed_submitted_code(monkeypatch, {
            "bad_mod": {"code": "bad code"},
            "good_mod": {"code": "good code"},
        }, cycle_num=1)
        result = duplication_checker.run()
        assert result["flagged"] == []
        vectors = fake_vector_index.upsert.call_args.kwargs["vectors"]
        ids = [v[0] for v in vectors]
        assert "codechunk:my-app:bad_mod:1" not in ids
        assert "codechunk:my-app:good_mod:1" in ids

    def test_query_failure_does_not_crash_and_still_upserts(self, fake_bus, fake_vector_index, monkeypatch):
        fake_vector_index.query.side_effect = RuntimeError("vector db down")
        _seed_submitted_code(monkeypatch, {"m": {"code": "x = 1"}}, cycle_num=1)
        result = duplication_checker.run()
        assert result["flagged"] == []
        fake_vector_index.upsert.assert_called_once()

    def test_upsert_failure_does_not_raise(self, fake_bus, fake_vector_index, monkeypatch):
        fake_vector_index.upsert.side_effect = RuntimeError("upsert failed")
        _seed_submitted_code(monkeypatch, {"m": {"code": "x = 1"}}, cycle_num=1)
        result = duplication_checker.run()  # should not raise
        assert result["flagged"] == []


# ---------------------------------------------------------------------------
# 5. Bus write and usage logging
# ---------------------------------------------------------------------------
class TestBusWriteAndLogging:
    def test_writes_report_to_duplication_report_key(self, fake_bus, fake_vector_index, monkeypatch):
        _seed_submitted_code(monkeypatch, {}, cycle_num=1)
        writes = {}
        monkeypatch.setattr(duplication_checker, "write", lambda key, val: writes.__setitem__(key, val))
        result = duplication_checker.run()
        assert writes[duplication_checker.KEYS["duplication_report"]] == result

    def test_log_usage_called_with_returned_key_env_not_hardcoded(self, fake_bus, fake_vector_index, monkeypatch):
        monkeypatch.setattr(
            duplication_checker, "embed_text_with_fallback",
            lambda text: ([0.1, 0.2], "HUGGINGFACE_API_KEY_7"),
        )
        _seed_submitted_code(monkeypatch, {"m": {"code": "x = 1"}}, cycle_num=1)
        logged = []
        monkeypatch.setattr(
            duplication_checker, "log_usage",
            lambda provider, key_id, *a, **k: logged.append((provider, key_id)),
        )
        duplication_checker.run(session_id="sess-1")
        assert logged == [("huggingface", "HUGGINGFACE_API_KEY_7")]
