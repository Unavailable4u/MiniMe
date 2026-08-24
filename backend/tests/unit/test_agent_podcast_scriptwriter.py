"""
tests/unit/test_agent_podcast_scriptwriter.py — Patch 7f-6-1.

Covers agents/podcast_scriptwriter.py's generate_podcast_script(): reads
via agents/source_planner_lean.py:plan() (scope="project"), optionally
narrows to `source_node_ids` via each topic's "covers" set, builds a
per-topic context string (excerpts, falling back to summary/content_hint,
each truncated to MAX_CONTENT_CHARS_PER_SOURCE), and — if any context
survives — runs it through the deferred agents.generic_worker.run
import against the fixed "podcast_scriptwriter" role, returning the raw
stripped script text. Raises LookupError when the resolved scope has no
readable topic content, so api/server.py's route can turn that into a
400 instead of asking the model to write a script from nothing.

Does NOT call agents/tts_synthesizer.py — that module has its own test
file (test_agent_tts_synthesizer.py) in this same sub-patch.

plan() and the deferred agents.generic_worker.run import are both
patched directly (this module never calls generate_text itself).
"""
from unittest.mock import MagicMock

import pytest

import agents.podcast_scriptwriter as podcast_scriptwriter


def _topic(name="Topic A", summary="summary A", content_hint="hint A", covers=None, excerpts=None):
    t = {"name": name, "summary": summary, "content_hint": content_hint, "covers": covers or []}
    if excerpts is not None:
        t["excerpts"] = excerpts
    return t


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
        context = podcast_scriptwriter._context_for(topics)
        assert "the real source text" in context

    def test_falls_back_to_summary_when_no_excerpts(self):
        topics = {"t1": _topic(summary="just the summary")}
        context = podcast_scriptwriter._context_for(topics)
        assert "just the summary" in context

    def test_falls_back_to_content_hint_when_no_summary_or_excerpts(self):
        topics = {"t1": {"name": "T", "content_hint": "a hint only", "covers": []}}
        context = podcast_scriptwriter._context_for(topics)
        assert "a hint only" in context

    def test_topic_with_no_content_at_all_is_skipped(self):
        topics = {"t1": {"name": "T", "covers": []}}
        context = podcast_scriptwriter._context_for(topics)
        assert context == ""

    def test_content_truncated_to_max_chars_per_source(self):
        topics = {"t1": _topic(excerpts="x" * 10000)}
        context = podcast_scriptwriter._context_for(topics)
        body = context.split("---")[-1]
        assert len(body.strip()) <= podcast_scriptwriter.MAX_CONTENT_CHARS_PER_SOURCE

    def test_untitled_topic_gets_default_title(self):
        topics = {"t1": {"summary": "no name here", "covers": []}}
        context = podcast_scriptwriter._context_for(topics)
        assert "Untitled topic" in context

    def test_multiple_topics_each_get_their_own_section(self):
        topics = {
            "t1": _topic(name="A", excerpts="excerpt A"),
            "t2": _topic(name="B", excerpts="excerpt B"),
        }
        context = podcast_scriptwriter._context_for(topics)
        assert "--- A ---" in context
        assert "--- B ---" in context
        assert "excerpt A" in context
        assert "excerpt B" in context


# ---------------------------------------------------------------------------
# 2. generate_podcast_script(): scoping by source_node_ids
# ---------------------------------------------------------------------------
class TestScoping:
    def test_no_source_node_ids_uses_every_topic(self, monkeypatch):
        monkeypatch.setattr(podcast_scriptwriter, "plan", lambda *a, **k: {
            "topics": {
                "t1": _topic(name="A", excerpts="excerpt A", covers=["n1"]),
                "t2": _topic(name="B", excerpts="excerpt B", covers=["n2"]),
            },
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "HOST A: hi"}

        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        podcast_scriptwriter.generate_podcast_script("ws1")
        assert "excerpt A" in captured["task_text"]
        assert "excerpt B" in captured["task_text"]

    def test_source_node_ids_narrows_to_matching_topics_only(self, monkeypatch):
        monkeypatch.setattr(podcast_scriptwriter, "plan", lambda *a, **k: {
            "topics": {
                "t1": _topic(name="A", excerpts="excerpt A", covers=["n1"]),
                "t2": _topic(name="B", excerpts="excerpt B", covers=["n2"]),
            },
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "HOST A: hi"}

        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        podcast_scriptwriter.generate_podcast_script("ws1", source_node_ids=["n1"])
        assert "excerpt A" in captured["task_text"]
        assert "excerpt B" not in captured["task_text"]

    def test_no_matching_topics_after_scoping_raises_without_llm_call(self, monkeypatch):
        monkeypatch.setattr(podcast_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"])},
        })
        called = []
        import sys
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": ""})},
        )()
        with pytest.raises(LookupError):
            podcast_scriptwriter.generate_podcast_script("ws1", source_node_ids=["n999"])
        assert called == []


# ---------------------------------------------------------------------------
# 3. generate_podcast_script(): the LookupError contract and empty scope
# ---------------------------------------------------------------------------
class TestEmptyScope:
    def test_zero_topics_raises_lookup_error_without_llm_call(self, monkeypatch):
        monkeypatch.setattr(podcast_scriptwriter, "plan", lambda *a, **k: {"topics": {}})
        called = []
        import sys
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": ""})},
        )()
        with pytest.raises(LookupError):
            podcast_scriptwriter.generate_podcast_script("ws1")
        assert called == []

    def test_topics_present_but_all_content_empty_raises_lookup_error(self, monkeypatch):
        monkeypatch.setattr(podcast_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": {"name": "T", "covers": []}},
        })
        with pytest.raises(LookupError):
            podcast_scriptwriter.generate_podcast_script("ws1")


# ---------------------------------------------------------------------------
# 4. generate_podcast_script(): the happy path — role, domain, result shape
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_calls_generic_worker_with_fixed_role_and_notes_domain(self, monkeypatch):
        monkeypatch.setattr(podcast_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        captured = {}

        def _run_role(**kwargs):
            captured.update(kwargs)
            return {"text": "HOST A: hello\nHOST B: hi there"}

        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        result = podcast_scriptwriter.generate_podcast_script("ws1")

        assert captured["role"] == "podcast_scriptwriter"
        assert captured["domain"] == "notes"
        assert captured["include_conversation_context"] is False
        assert captured["session_id"] is None
        assert result == "HOST A: hello\nHOST B: hi there"

    def test_result_is_stripped_of_surrounding_whitespace(self, monkeypatch):
        monkeypatch.setattr(podcast_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("\n\n  HOST A: padded  \n\n")
        result = podcast_scriptwriter.generate_podcast_script("ws1")
        assert result == "HOST A: padded"

    def test_missing_text_key_in_result_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(podcast_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(lambda **k: {})})()
        result = podcast_scriptwriter.generate_podcast_script("ws1")
        assert result == ""

    def test_plan_called_with_project_scope_and_task_text(self, monkeypatch):
        captured_plan_args = {}

        def _fake_plan(workspace_id, task_text=None, scope=None):
            captured_plan_args["workspace_id"] = workspace_id
            captured_plan_args["task_text"] = task_text
            captured_plan_args["scope"] = scope
            return {"topics": {"t1": _topic(excerpts="content")}}

        monkeypatch.setattr(podcast_scriptwriter, "plan", _fake_plan)
        _fake_generic_worker("HOST A: hi")

        podcast_scriptwriter.generate_podcast_script("ws42")

        assert captured_plan_args["workspace_id"] == "ws42"
        assert captured_plan_args["scope"] == "project"
        assert captured_plan_args["task_text"]  # non-empty guidance string
