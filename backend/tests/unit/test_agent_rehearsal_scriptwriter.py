"""
tests/unit/test_agent_rehearsal_scriptwriter.py — Patch 7f-6-1.

Covers agents/rehearsal_scriptwriter.py's generate_rehearsal_script():
same source_planner_lean.py:plan() (scope="project") / per-topic
context / deferred agents.generic_worker.run shape as
agents/podcast_scriptwriter.py, plus this module's own two additions —
mode ("judge" / "two_host" / "devils_advocate") and difficulty
("novice" / "expert") validation (ValueError on an unrecognized value,
checked BEFORE plan() runs so a bad argument never spends a plan() call)
and both values being folded into task_text ahead of the source
material rather than passed as separate role inputs.

Does NOT call agents/tts_synthesizer.py — that module has its own test
file (test_agent_tts_synthesizer.py) in this same sub-patch.

plan() and the deferred agents.generic_worker.run import are both
patched directly (this module never calls generate_text itself).
"""
import sys
from unittest.mock import MagicMock

import pytest

from agents import rehearsal_scriptwriter


def _topic(name="Topic A", summary="summary A", content_hint="hint A", covers=None, excerpts=None):
    t = {"name": name, "summary": summary, "content_hint": content_hint, "covers": covers or []}
    if excerpts is not None:
        t["excerpts"] = excerpts
    return t


@pytest.fixture(autouse=True)
def _clear_fake_generic_worker():
    """This file substitutes a bare stand-in object for agents.generic_worker
    in sys.modules and never restored it, which poisons the deferred `from
    agents.generic_worker import PROVIDER_DEFAULT_MODEL` in
    eo/quota_sentinel.py for every test that runs afterward in the same
    session -- same landmine and same fix test_agent_idea_planner.py already
    applies for eo.dynamic_chain. Restoring (not popping) avoids
    retriggering the agents.generic_worker <-> eo.registry circular-import
    cycle patch 0001 broke."""
    real_module = sys.modules.get("agents.generic_worker")
    yield
    if real_module is not None:
        sys.modules["agents.generic_worker"] = real_module
    else:
        sys.modules.pop("agents.generic_worker", None)


def _fake_generic_worker(text):
    module = type("M", (), {"run": MagicMock(return_value={"text": text})})()
    sys.modules["agents.generic_worker"] = module
    return module.run


# ---------------------------------------------------------------------------
# 1. _context_for(): identical shape to podcast_scriptwriter's helper
# ---------------------------------------------------------------------------
class TestContextFor:
    def test_uses_excerpts_when_present(self):
        topics = {"t1": _topic(excerpts="the real source text")}
        context = rehearsal_scriptwriter._context_for(topics)
        assert "the real source text" in context

    def test_falls_back_to_summary_when_no_excerpts(self):
        topics = {"t1": _topic(summary="just the summary")}
        context = rehearsal_scriptwriter._context_for(topics)
        assert "just the summary" in context

    def test_topic_with_no_content_at_all_is_skipped(self):
        topics = {"t1": {"name": "T", "covers": []}}
        context = rehearsal_scriptwriter._context_for(topics)
        assert context == ""

    def test_content_truncated_to_max_chars_per_source(self):
        topics = {"t1": _topic(excerpts="x" * 10000)}
        context = rehearsal_scriptwriter._context_for(topics)
        body = context.split("---")[-1]
        assert len(body.strip()) <= rehearsal_scriptwriter.MAX_CONTENT_CHARS_PER_SOURCE


# ---------------------------------------------------------------------------
# 2. generate_rehearsal_script(): mode/difficulty validation
# ---------------------------------------------------------------------------
class TestModeAndDifficultyValidation:
    def test_unknown_mode_raises_value_error_without_calling_plan(self, monkeypatch):
        called = []
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: called.append(1))
        with pytest.raises(ValueError):
            rehearsal_scriptwriter.generate_rehearsal_script("ws1", mode="not_a_real_mode")
        assert called == []

    def test_unknown_difficulty_raises_value_error_without_calling_plan(self, monkeypatch):
        called = []
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: called.append(1))
        with pytest.raises(ValueError):
            rehearsal_scriptwriter.generate_rehearsal_script("ws1", difficulty="impossible")
        assert called == []

    @pytest.mark.parametrize("mode", ["judge", "two_host", "devils_advocate"])
    def test_every_valid_mode_is_accepted(self, monkeypatch, mode):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("JUDGE: question")
        # Should not raise.
        rehearsal_scriptwriter.generate_rehearsal_script("ws1", mode=mode)

    @pytest.mark.parametrize("difficulty", ["novice", "expert"])
    def test_every_valid_difficulty_is_accepted(self, monkeypatch, difficulty):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("JUDGE: question")
        # Should not raise.
        rehearsal_scriptwriter.generate_rehearsal_script("ws1", difficulty=difficulty)

    def test_defaults_are_judge_and_expert_when_unspecified(self, monkeypatch):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "JUDGE: question"}

        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        rehearsal_scriptwriter.generate_rehearsal_script("ws1")
        assert "Mode: judge" in captured["task_text"]
        assert "Difficulty: expert" in captured["task_text"]


# ---------------------------------------------------------------------------
# 3. generate_rehearsal_script(): scoping by source_node_ids
# ---------------------------------------------------------------------------
class TestScoping:
    def test_no_source_node_ids_uses_every_topic(self, monkeypatch):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {
            "topics": {
                "t1": _topic(name="A", excerpts="excerpt A", covers=["n1"]),
                "t2": _topic(name="B", excerpts="excerpt B", covers=["n2"]),
            },
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "JUDGE: hi"}

        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        rehearsal_scriptwriter.generate_rehearsal_script("ws1")
        assert "excerpt A" in captured["task_text"]
        assert "excerpt B" in captured["task_text"]

    def test_source_node_ids_narrows_to_matching_topics_only(self, monkeypatch):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {
            "topics": {
                "t1": _topic(name="A", excerpts="excerpt A", covers=["n1"]),
                "t2": _topic(name="B", excerpts="excerpt B", covers=["n2"]),
            },
        })
        captured = {}

        def _run_role(**kwargs):
            captured["task_text"] = kwargs["task_text"]
            return {"text": "JUDGE: hi"}

        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        rehearsal_scriptwriter.generate_rehearsal_script("ws1", source_node_ids=["n1"])
        assert "excerpt A" in captured["task_text"]
        assert "excerpt B" not in captured["task_text"]

    def test_no_matching_topics_after_scoping_raises_without_llm_call(self, monkeypatch):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(covers=["n1"])},
        })
        called = []
        import sys
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": ""})},
        )()
        with pytest.raises(LookupError):
            rehearsal_scriptwriter.generate_rehearsal_script("ws1", source_node_ids=["n999"])
        assert called == []


# ---------------------------------------------------------------------------
# 4. generate_rehearsal_script(): empty scope raises LookupError
# ---------------------------------------------------------------------------
class TestEmptyScope:
    def test_zero_topics_raises_lookup_error_without_llm_call(self, monkeypatch):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {"topics": {}})
        called = []
        import sys
        sys.modules["agents.generic_worker"] = type(
            "M", (), {"run": staticmethod(lambda **k: called.append(1) or {"text": ""})},
        )()
        with pytest.raises(LookupError):
            rehearsal_scriptwriter.generate_rehearsal_script("ws1")
        assert called == []


# ---------------------------------------------------------------------------
# 5. generate_rehearsal_script(): the happy path — role, domain, result shape
# ---------------------------------------------------------------------------
class TestHappyPath:
    def test_calls_generic_worker_with_fixed_role_and_notes_domain(self, monkeypatch):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        captured = {}

        def _run_role(**kwargs):
            captured.update(kwargs)
            return {"text": "JUDGE: question\n[PAUSE:8]\nMODEL ANSWER: answer"}

        import sys
        sys.modules["agents.generic_worker"] = type("M", (), {"run": staticmethod(_run_role)})()

        result = rehearsal_scriptwriter.generate_rehearsal_script("ws1", mode="two_host", difficulty="novice")

        assert captured["role"] == "rehearsal_scriptwriter"
        assert captured["domain"] == "notes"
        assert captured["include_conversation_context"] is False
        assert captured["session_id"] is None
        assert "Mode: two_host" in captured["task_text"]
        assert "Difficulty: novice" in captured["task_text"]
        assert result == "JUDGE: question\n[PAUSE:8]\nMODEL ANSWER: answer"

    def test_result_is_stripped_of_surrounding_whitespace(self, monkeypatch):
        monkeypatch.setattr(rehearsal_scriptwriter, "plan", lambda *a, **k: {
            "topics": {"t1": _topic(excerpts="content")},
        })
        _fake_generic_worker("\n\n  JUDGE: padded  \n\n")
        result = rehearsal_scriptwriter.generate_rehearsal_script("ws1")
        assert result == "JUDGE: padded"
