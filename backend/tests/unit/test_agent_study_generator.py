"""
tests/unit/test_agent_study_generator.py — Patch 7f-7-3.

Covers agents/study_generator.py's generate_study_content(): resolves
`panel_key` against ROLES_BY_PANEL_KEY (flashcard_writer / quiz_writer /
study_guide_writer), reads via agents/source_planner_lean.py:plan()
(scope="project"), optionally narrows to `source_node_ids` via each
topic's "covers" set, builds a per-topic context string (_context_for()
-- excerpts, falling back to summary/content_hint, each truncated to
MAX_CONTENT_CHARS_PER_SOURCE), and -- if any context survives -- runs it
through the deferred agents.generic_worker.run import against the
resolved role, returning the raw stripped Markdown. Raises ValueError
for an unrecognized panel_key (checked before plan() is ever called) and
LookupError when the resolved scope has zero readable topic content --
same contract agents/podcast_scriptwriter.py's generate_podcast_script()
and agents/slide_deck_planner.py's generate_slide_deck() already give
their own callers (this module is deliberately near-identical in shape
to those, per its own docstring's CHANGED note referencing the same
retrofit).

Does NOT call eo/panel_content.py's set_content() -- saving is
api/server.py's Generate dispatch's own responsibility, out of scope
here.

plan() and the deferred agents.generic_worker.run import are both
patched directly (this module never calls generate_text itself).
"""
import sys
from unittest.mock import MagicMock

import pytest

from agents import study_generator


def _topic(name="Topic A", summary="summary A", content_hint="hint A", covers=None, excerpts=None):
    t = {"name": name, "summary": summary, "content_hint": content_hint, "covers": covers or []}
    if excerpts is not None:
        t["excerpts"] = excerpts
    return t


def _fake_generic_worker(text):
    module = type("M", (), {"run": MagicMock(return_value={"text": text})})()
    sys.modules["agents.generic_worker"] = module
    return module.run


# ---------------------------------------------------------------------------
# 1. generate_study_content(): panel_key -> role resolution
# ---------------------------------------------------------------------------
class TestPanelKeyResolution:
    def test_unrecognized_panel_key_raises_value_error(self):
        with pytest.raises(ValueError, match="unrecognized study panel_key"):
            study_generator.generate_study_content("not_a_real_key", "ws1")

    def test_unrecognized_panel_key_never_calls_plan(self, monkeypatch):
        called = []
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: called.append(1) or {"topics": {}})
        with pytest.raises(ValueError):
            study_generator.generate_study_content("bogus", "ws1")
        assert called == []

    @pytest.mark.parametrize("panel_key,expected_role", [
        ("study_flashcards", "flashcard_writer"),
        ("study_quiz", "quiz_writer"),
        ("study_guide", "study_guide_writer"),
    ])
    def test_each_valid_panel_key_resolves_to_its_role(self, monkeypatch, panel_key, expected_role):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        captured = {}

        def _run_role(**kwargs):
            captured.update(kwargs)
            return {"text": "output"}

        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        study_generator.generate_study_content(panel_key, "ws1")

        assert captured["role"] == expected_role


# ---------------------------------------------------------------------------
# 2. _context_for(): excerpts vs summary fallback, truncation, empties
# ---------------------------------------------------------------------------
class TestContextFor:
    def test_uses_excerpts_when_present(self):
        topics = {"t1": _topic(excerpts="the real source text")}
        context = study_generator._context_for(topics)
        assert "the real source text" in context

    def test_falls_back_to_summary_when_no_excerpts(self):
        topics = {"t1": _topic(summary="just the summary")}
        context = study_generator._context_for(topics)
        assert "just the summary" in context

    def test_falls_back_to_content_hint_when_no_summary_or_excerpts(self):
        topics = {"t1": {"name": "T", "content_hint": "a hint only", "covers": []}}
        context = study_generator._context_for(topics)
        assert "a hint only" in context

    def test_topic_with_no_content_at_all_is_skipped(self):
        topics = {"t1": {"name": "T", "covers": []}}
        context = study_generator._context_for(topics)
        assert context == ""

    def test_content_truncated_to_max_chars_per_source(self):
        topics = {"t1": _topic(excerpts="x" * 10000)}
        context = study_generator._context_for(topics)
        body = context.split("---")[-1]
        assert len(body.strip()) <= study_generator.MAX_CONTENT_CHARS_PER_SOURCE

    def test_untitled_topic_gets_default_title(self):
        topics = {"t1": {"summary": "no name here", "covers": []}}
        context = study_generator._context_for(topics)
        assert "Untitled topic" in context

    def test_multiple_topics_each_get_their_own_section(self):
        topics = {
            "t1": _topic(name="A", excerpts="excerpt A"),
            "t2": _topic(name="B", excerpts="excerpt B"),
        }
        context = study_generator._context_for(topics)
        assert "--- A ---" in context
        assert "--- B ---" in context
        assert "excerpt A" in context
        assert "excerpt B" in context

    def test_whitespace_only_excerpt_is_not_falsy_so_it_does_not_fall_back(self):
        # "excerpts" present-but-whitespace is truthy, so the `if not
        # body` fallback check never fires -- it strips to "" afterward
        # and the topic is skipped entirely, rather than falling back to
        # summary/content_hint the way an actually-missing key would.
        topics = {"t1": _topic(excerpts="   ", summary="the real summary")}
        context = study_generator._context_for(topics)
        assert context == ""


# ---------------------------------------------------------------------------
# 3. generate_study_content(): scoping by source_node_ids
# ---------------------------------------------------------------------------
class TestScoping:
    def test_no_source_node_ids_uses_every_topic(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {
                "t1": _topic(name="A", excerpts="excerpt A", covers=["n1"]),
                "t2": _topic(name="B", excerpts="excerpt B", covers=["n2"]),
            },
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "# Flashcards"}

        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        study_generator.generate_study_content("study_flashcards", "ws1")
        assert "excerpt A" in captured["task_text"]
        assert "excerpt B" in captured["task_text"]

    def test_source_node_ids_narrows_to_matching_topics_only(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {
                "t1": _topic(name="A", excerpts="excerpt A", covers=["n1"]),
                "t2": _topic(name="B", excerpts="excerpt B", covers=["n2"]),
            },
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "# Quiz"}

        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        study_generator.generate_study_content("study_quiz", "ws1", source_node_ids=["n1"])
        assert "excerpt A" in captured["task_text"]
        assert "excerpt B" not in captured["task_text"]

    def test_no_matching_topics_after_scoping_raises_without_llm_call(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"])},
        })
        called = []
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": ""})},
        )()
        with pytest.raises(LookupError):
            study_generator.generate_study_content("study_guide", "ws1", source_node_ids=["n999"])
        assert called == []


# ---------------------------------------------------------------------------
# 4. generate_study_content(): the LookupError contract and empty scope
# ---------------------------------------------------------------------------
class TestEmptyScope:
    def test_zero_topics_raises_lookup_error_without_llm_call(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {"topics": {}})
        called = []
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": ""})},
        )()
        with pytest.raises(LookupError):
            study_generator.generate_study_content("study_flashcards", "ws1")
        assert called == []

    def test_topics_present_but_all_content_empty_raises_lookup_error(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {"t1": {"name": "T", "covers": []}},
        })
        with pytest.raises(LookupError):
            study_generator.generate_study_content("study_quiz", "ws1")

    def test_value_error_for_bad_panel_key_takes_priority_over_scope_check(self, monkeypatch):
        # A bad panel_key must be rejected before plan() is even
        # consulted, regardless of what the (never-reached) scope would
        # have resolved to.
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {"topics": {}})
        with pytest.raises(ValueError):
            study_generator.generate_study_content("nonsense_key", "ws1")


# ---------------------------------------------------------------------------
# 5. generate_study_content(): the happy path — role, domain, result shape
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_calls_generic_worker_with_resolved_role_and_notes_domain(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        captured = {}

        def _run_role(**kwargs):
            captured.update(kwargs)
            return {"text": "Q: What is X?\nA: X is Y."}

        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        result = study_generator.generate_study_content("study_quiz", "ws1")

        assert captured["role"] == "quiz_writer"
        assert captured["domain"] == "notes"
        assert captured["include_conversation_context"] is False
        assert captured["session_id"] is None
        assert captured["input_keys"] == []
        assert result == "Q: What is X?\nA: X is Y."

    def test_result_is_stripped_of_surrounding_whitespace(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("\n\n  # Study Guide padded  \n\n")
        result = study_generator.generate_study_content("study_guide", "ws1")
        assert result == "# Study Guide padded"

    def test_missing_text_key_in_result_returns_empty_string(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(lambda **k: {})})()
        result = study_generator.generate_study_content("study_flashcards", "ws1")
        assert result == ""

    def test_plan_called_with_project_scope_and_panel_key_in_task_text(self, monkeypatch):
        captured_plan_args = {}

        def _fake_plan(workspace_id, task_text=None, scope=None):
            captured_plan_args["workspace_id"] = workspace_id
            captured_plan_args["task_text"] = task_text
            captured_plan_args["scope"] = scope
            return {"topics": {"t1": _topic(excerpts="content")}}

        monkeypatch.setattr(study_generator, "plan", _fake_plan)
        _fake_generic_worker("# Flashcards")

        study_generator.generate_study_content("study_flashcards", "ws42")

        assert captured_plan_args["workspace_id"] == "ws42"
        assert captured_plan_args["scope"] == "project"
        assert "study flashcards" in captured_plan_args["task_text"]

    def test_task_text_carries_the_built_context(self, monkeypatch):
        monkeypatch.setattr(study_generator, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(name="Photosynthesis", excerpts="light -> energy")},
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "# Guide"}

        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        study_generator.generate_study_content("study_guide", "ws1")

        assert "Source material:" in captured["task_text"]
        assert "Photosynthesis" in captured["task_text"]
        assert "light -> energy" in captured["task_text"]

    def test_panel_key_underscores_are_spaced_out_in_plan_task_text(self, monkeypatch):
        captured_plan_args = {}

        def _fake_plan(workspace_id, task_text=None, scope=None):
            captured_plan_args["task_text"] = task_text
            return {"topics": {"t1": _topic(excerpts="content")}}

        monkeypatch.setattr(study_generator, "plan", _fake_plan)
        _fake_generic_worker("# Guide")

        study_generator.generate_study_content("study_guide", "ws1")

        assert "study guide" in captured_plan_args["task_text"]
        assert "study_guide" not in captured_plan_args["task_text"]
