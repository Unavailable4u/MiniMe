"""
tests/unit/test_agent_memory_search.py — Patch 7f-5.

Covers agents/memory_search.py: Cross-Cycle Memory Search.

  1. _app_slug() — the Migration Part B session-isolation fix: prefers
     memory.bus.get_current_app_slug() (session-scoped), falls back to
     KEYS["original_idea"] only when no app_slug context is active.
  2. store_cycle_memory() — no-ops on no report; builds the summary
     text from plan+report fields; embeds+logs+upserts on the happy
     path; fails soft (never raises) on either an embedding failure or
     a Vector upsert failure.
  3. retrieve_context() — fails soft to "" (writing KEYS["retrieved_
     context"]) on either an embedding failure or a Vector query
     failure; on the happy path, queries Vector scoped to this
     app_slug, and builds its returned text block only from matches
     that actually carry metadata.
  4. run() — the loop.py entrypoint: builds its query from
     KEYS["original_idea"] + KEYS["feature_status"] and delegates to
     retrieve_context().

vector_index()/log_usage()/embed_text_with_fallback() are faked at the
module level (bound-name imports), same posture
test_agent_duplication_checker.py already establishes for this exact
trio of dependencies. _app_slug() itself is faked away for every test
except its own dedicated section below, same convention that file's
`_fake_app_slug` fixture uses.
"""
from unittest.mock import MagicMock

import pytest

from agents import memory_search
from memory.bus import KEYS, read, write


class _Match:
    def __init__(self, metadata=None):
        self.metadata = metadata


def _fake_embed(vector=(0.1, 0.2, 0.3), key_env="HUGGINGFACE_API_KEY_1"):
    return lambda text: (list(vector), key_env)


@pytest.fixture
def fake_vector_index(monkeypatch):
    index = MagicMock()
    index.query.return_value = []
    monkeypatch.setattr(memory_search, "vector_index", lambda: index)
    return index


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    monkeypatch.setattr(memory_search, "embed_text_with_fallback", _fake_embed())


@pytest.fixture
def fake_log_usage(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(memory_search, "log_usage", mock)
    return mock


@pytest.fixture
def _fake_app_slug(monkeypatch):
    monkeypatch.setattr(memory_search, "_app_slug", lambda: "my-app")


# ---------------------------------------------------------------------------
# 1. _app_slug() — real implementation, not faked (see fixture above)
# ---------------------------------------------------------------------------

class TestAppSlug:
    def test_prefers_current_app_slug_context(self, monkeypatch):
        import memory.bus as bus_module
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: "scoped-app")
        write(KEYS["original_idea"], "some other idea")
        assert memory_search._app_slug() == "scoped-app"

    def test_falls_back_to_original_idea_when_no_context(self, monkeypatch):
        import memory.bus as bus_module
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: None)
        write(KEYS["original_idea"], "the fallback idea")
        assert memory_search._app_slug() == "the fallback idea"

    def test_falls_back_to_untitled_when_nothing_set(self, monkeypatch):
        import memory.bus as bus_module
        monkeypatch.setattr(bus_module, "get_current_app_slug", lambda: None)
        assert memory_search._app_slug() == "untitled"


# ---------------------------------------------------------------------------
# 2. store_cycle_memory()
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_fake_app_slug")
class TestStoreCycleMemory:
    def test_no_report_is_a_noop(self, fake_vector_index, fake_log_usage):
        write(KEYS["latest_report"], None)
        write(KEYS["current_plan"], {"cycle_goal": "goal"})
        memory_search.store_cycle_memory(1)
        fake_vector_index.upsert.assert_not_called()
        fake_log_usage.assert_not_called()

    def test_happy_path_embeds_logs_and_upserts(self, fake_vector_index, fake_log_usage):
        write(KEYS["latest_report"], {"all_tests_passed": True, "summary": "Shipped login."})
        write(KEYS["current_plan"], {"cycle_goal": "Ship login", "target_feature": "Login"})

        memory_search.store_cycle_memory(3, session_id="s1", tier=3, domain="coding")

        fake_log_usage.assert_called_once_with(
            "huggingface", "HUGGINGFACE_API_KEY_1", None,
            session_id="s1", tier=3, agent_name="Memory Search", domain="coding",
        )
        fake_vector_index.upsert.assert_called_once()
        upsert_kwargs = fake_vector_index.upsert.call_args.kwargs
        vec_id, vector, metadata = upsert_kwargs["vectors"][0]
        assert vec_id == "cyclemem:my-app:3"
        assert vector == [0.1, 0.2, 0.3]
        assert metadata["app_slug"] == "my-app"
        assert metadata["cycle_num"] == 3
        assert "cycle_goal: Ship login" in metadata["text"]
        assert "target_feature: Login" in metadata["text"]
        assert "all_tests_passed: True" in metadata["text"]
        assert "summary: Shipped login." in metadata["text"]

    def test_missing_plan_defaults_to_empty_dict(self, fake_vector_index, fake_log_usage):
        write(KEYS["latest_report"], {"all_tests_passed": False, "summary": "Partial."})
        write(KEYS["current_plan"], None)
        # Should not raise on plan.get(...) against a missing plan.
        memory_search.store_cycle_memory(1)
        fake_vector_index.upsert.assert_called_once()

    def test_embed_failure_skips_log_and_upsert_without_raising(self, fake_vector_index, fake_log_usage, monkeypatch):
        write(KEYS["latest_report"], {"summary": "x"})
        write(KEYS["current_plan"], {})
        monkeypatch.setattr(memory_search, "embed_text_with_fallback",
                             MagicMock(side_effect=RuntimeError("HF down")))
        memory_search.store_cycle_memory(1)  # must not raise
        fake_log_usage.assert_not_called()
        fake_vector_index.upsert.assert_not_called()

    def test_upsert_failure_swallowed_without_raising(self, fake_vector_index, fake_log_usage):
        write(KEYS["latest_report"], {"summary": "x"})
        write(KEYS["current_plan"], {})
        fake_vector_index.upsert.side_effect = RuntimeError("Vector down")
        memory_search.store_cycle_memory(1)  # must not raise
        fake_log_usage.assert_called_once()


# ---------------------------------------------------------------------------
# 3. retrieve_context()
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_fake_app_slug")
class TestRetrieveContext:
    def test_embed_failure_writes_empty_context_and_returns_empty(self, fake_vector_index, fake_log_usage, monkeypatch):
        monkeypatch.setattr(memory_search, "embed_text_with_fallback",
                             MagicMock(side_effect=RuntimeError("HF down")))
        result = memory_search.retrieve_context("some query")
        assert result == ""
        assert read(KEYS["retrieved_context"]) == ""
        fake_log_usage.assert_not_called()
        fake_vector_index.query.assert_not_called()

    def test_query_failure_writes_empty_context_and_returns_empty(self, fake_vector_index, fake_log_usage):
        fake_vector_index.query.side_effect = RuntimeError("Vector down")
        result = memory_search.retrieve_context("some query")
        assert result == ""
        assert read(KEYS["retrieved_context"]) == ""
        fake_log_usage.assert_called_once()

    def test_happy_path_queries_scoped_to_app_slug_and_builds_context(self, fake_vector_index, fake_log_usage):
        fake_vector_index.query.return_value = [
            _Match(metadata={"text": "cycle 1: shipped login"}),
            _Match(metadata={"text": "cycle 2: shipped search"}),
            _Match(metadata=None),  # no metadata -- must be skipped
        ]
        result = memory_search.retrieve_context("find similar cycles", top_k=5,
                                                  session_id="s1", tier=3, domain="coding")

        fake_vector_index.query.assert_called_once_with(
            vector=[0.1, 0.2, 0.3], top_k=5, include_metadata=True,
            filter="app_slug = 'my-app'",
        )
        fake_log_usage.assert_called_once_with(
            "huggingface", "HUGGINGFACE_API_KEY_1", None,
            session_id="s1", tier=3, agent_name="Memory Search", domain="coding",
        )
        assert result == "- cycle 1: shipped login\n- cycle 2: shipped search"
        assert read(KEYS["retrieved_context"]) == result

    def test_matches_with_blank_text_are_dropped(self, fake_vector_index, fake_log_usage):
        fake_vector_index.query.return_value = [
            _Match(metadata={"text": ""}),
            _Match(metadata={"text": "real line"}),
        ]
        result = memory_search.retrieve_context("query")
        assert result == "- real line"


# ---------------------------------------------------------------------------
# 4. run()
# ---------------------------------------------------------------------------

class TestRun:
    def test_builds_query_from_idea_and_feature_status_and_delegates(self, monkeypatch):
        write(KEYS["original_idea"], "A todo app")
        write(KEYS["feature_status"], {"Login": "done", "Search": "missing"})
        captured = {}

        def _fake_retrieve(query, session_id=None, tier=None, domain=None):
            captured.update(query=query, session_id=session_id, tier=tier, domain=domain)
            return "the context"

        monkeypatch.setattr(memory_search, "retrieve_context", _fake_retrieve)
        result = memory_search.run(session_id="s1", tier=2, domain="coding")

        assert result == "the context"
        assert "A todo app" in captured["query"]
        assert '"Login": "done"' in captured["query"]
        assert captured["session_id"] == "s1"
        assert captured["tier"] == 2
        assert captured["domain"] == "coding"

    def test_defaults_to_blank_idea_and_empty_feature_status(self, monkeypatch):
        captured = {}
        monkeypatch.setattr(memory_search, "retrieve_context",
                             lambda query, **kw: captured.setdefault("query", query))
        memory_search.run()
        assert captured["query"] == ' | feature_status: {}'
