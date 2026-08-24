"""
tests/unit/test_agent_fact_detector.py — Patch 7f-3.

Covers agents/fact_detector.py's detect_facts(): the missing "detect
step" for the Facts subtab (module docstring: propose_fact() had zero
real callers before this file). Reads via
agents/source_planner_lean.py:plan() (Mode B/C), optionally narrows to
`source_node_ids`, builds context, asks the `fact_detector` role, and
proposes each parsed fact through eo/workspace_facts.py -- never
writing directly into live facts.

plan() and the deferred agents.generic_worker.run import are both
patched directly (this module never calls generate_text itself).
"""
import json
from unittest.mock import MagicMock

import pytest

import agents.fact_detector as fact_detector


def _topic(name="Topic A", summary="summary A", content_hint="hint A", covers=None, excerpts=None):
    t = {"name": name, "summary": summary, "content_hint": content_hint, "covers": covers or []}
    if excerpts is not None:
        t["excerpts"] = excerpts
    return t


@pytest.fixture(autouse=True)
def _fake_ensure_role(monkeypatch):
    monkeypatch.setattr(fact_detector, "_ensure_role_registered", lambda: None)


def _fake_generic_worker(text):
    import sys
    module = type("M", (), {"run": MagicMock(return_value={"text": text})})()
    sys.modules["agents.generic_worker"] = module
    return module.run


# ---------------------------------------------------------------------------
# 1. _context_for(): excerpts vs summary fallback, truncation, empties
# ---------------------------------------------------------------------------
class TestContextFor:
    def test_uses_excerpts_when_present(self):
        topics = {"t1": _topic(excerpts="the real source text")}
        context = fact_detector._context_for(topics)
        assert "the real source text" in context

    def test_falls_back_to_summary_when_no_excerpts(self):
        topics = {"t1": _topic(summary="just the summary")}
        context = fact_detector._context_for(topics)
        assert "just the summary" in context

    def test_falls_back_to_content_hint_when_no_summary_or_excerpts(self):
        topics = {"t1": {"name": "T", "content_hint": "a hint only", "covers": []}}
        context = fact_detector._context_for(topics)
        assert "a hint only" in context

    def test_topic_with_no_content_at_all_is_skipped(self):
        topics = {"t1": {"name": "T", "covers": []}}
        context = fact_detector._context_for(topics)
        assert context == ""

    def test_content_truncated_to_max_chars_per_source(self):
        topics = {"t1": _topic(excerpts="x" * 10000)}
        context = fact_detector._context_for(topics)
        # title header + truncated body
        body = context.split("---")[-1]
        assert len(body.strip()) <= fact_detector.MAX_CONTENT_CHARS_PER_SOURCE

    def test_untitled_topic_gets_default_title(self):
        topics = {"t1": {"summary": "no name here", "covers": []}}
        context = fact_detector._context_for(topics)
        assert "Untitled topic" in context


# ---------------------------------------------------------------------------
# 2. detect_facts(): scoping by source_node_ids
# ---------------------------------------------------------------------------
class TestScoping:
    def test_no_source_node_ids_uses_every_topic(self, monkeypatch):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"]), "t2": _topic(name="B", covers=["n2"])},
        })
        _fake_generic_worker("NONE")
        result = fact_detector.detect_facts("ws1")
        assert result == []  # NONE -> no candidates, but confirms it ran without filtering error

    def test_source_node_ids_narrows_to_matching_topics_only(self, monkeypatch):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {
                "t1": _topic(name="A", excerpts="excerpt A", covers=["n1"]),
                "t2": _topic(name="B", excerpts="excerpt B", covers=["n2"]),
            },
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "NONE"}

        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        fact_detector.detect_facts("ws1", source_node_ids=["n1"])
        assert "excerpt A" in captured["task_text"]
        assert "excerpt B" not in captured["task_text"]

    def test_no_matching_topics_after_scoping_returns_empty_without_llm_call(self, monkeypatch):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"])},
        })
        called = []
        import sys
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": "NONE"})},
        )()
        result = fact_detector.detect_facts("ws1", source_node_ids=["n999"])
        assert result == []
        assert called == []


# ---------------------------------------------------------------------------
# 3. Response parsing: NONE, fenced JSON, malformed
# ---------------------------------------------------------------------------
class TestResponseParsing:
    def test_none_response_returns_empty_list(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("NONE")
        result = fact_detector.detect_facts("ws1")
        assert result == []

    def test_none_response_is_case_insensitive(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("none")
        result = fact_detector.detect_facts("ws1")
        assert result == []

    def test_valid_fenced_json_proposes_facts(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker(
            '```json\n[{"key": "target_platform", "value": "runs on AWS Lambda"}]\n```'
        )
        result = fact_detector.detect_facts("ws1")
        assert len(result) == 1
        assert result[0]["key"] == "target_platform"
        assert result[0]["value"] == "runs on AWS Lambda"

    def test_no_fenced_block_returns_empty(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("just some prose with no code block")
        result = fact_detector.detect_facts("ws1")
        assert result == []

    def test_malformed_json_in_fence_returns_empty(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("```json\n{not valid json\n```")
        result = fact_detector.detect_facts("ws1")
        assert result == []

    def test_json_that_is_not_a_list_returns_empty(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker('```json\n{"key": "x", "value": "y"}\n```')  # object, not array
        result = fact_detector.detect_facts("ws1")
        assert result == []

    def test_items_missing_key_or_value_are_skipped(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker('```json\n[{"key": "only_key"}, {"value": "only_value"}, '
                              '{"key": "good", "value": "value"}]\n```')
        result = fact_detector.detect_facts("ws1")
        assert len(result) == 1
        assert result[0]["key"] == "good"

    def test_title_and_summary_aliases_accepted_for_key_and_value(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker('```json\n[{"title": "aliased_key", "summary": "aliased_value"}]\n```')
        result = fact_detector.detect_facts("ws1")
        assert len(result) == 1
        assert result[0]["key"] == "aliased_key"
        assert result[0]["value"] == "aliased_value"


# ---------------------------------------------------------------------------
# 4. Returns only THIS call's new candidates (diff against before)
# ---------------------------------------------------------------------------
class TestReturnsOnlyNewCandidates:
    def test_only_newly_added_candidates_returned_not_pre_existing_ones(self, monkeypatch, fake_bus):
        from eo import workspace_facts
        workspace_facts.propose_fact("ws1", key="pre_existing", value="already there", proposed_by="someone_else")

        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker('```json\n[{"key": "new_fact", "value": "just added"}]\n```')

        result = fact_detector.detect_facts("ws1")
        assert len(result) == 1
        assert result[0]["key"] == "new_fact"

    def test_no_context_returns_empty_without_calling_llm(self, monkeypatch, fake_bus):
        monkeypatch.setattr(fact_detector, "plan", lambda *a, **k: {"topics": {}})
        called = []
        import sys
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": "NONE"})},
        )()
        result = fact_detector.detect_facts("ws1")
        assert result == []
        assert called == []
