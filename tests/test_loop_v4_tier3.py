"""
tests/test_loop_v4_tier3.py — Part 11: "confirm loop_v4.py correctly
hands off to the unmodified loop.py and produces identical behavior to
calling loop.py directly" -- updated for Stage 4 steps 4-7, where tier 3
is no longer the ALWAYS case. It's now one branch among four, reached
only when the Inspector/Panel actually decide tier 3, and gated by Part
8.1's cost-ceiling confirmation.

This does NOT run a real cycle (needs real API keys/Upstash -- that's
test_eo_inspector.py's live-fixture job). It checks the two things this
stage is actually about:
  1. A confirmed tier-3 decision hands off to loop.main() with the exact
     argv loop.py would have received directly, unmodified.
  2. Declining the cost-ceiling confirmation stops BEFORE loop.main() is
     ever called -- the guardrail actually guards something.
"""
import os
import sys
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import eo.loop_v4 as loop_v4

TIER3_DECISION = {
    "tier": 3, "directed_task_type": None, "confidence": 0.9,
    "suggested_agents": [], "reasoning": "ongoing multi-cycle project", "panel_reviewed": False,
}


def _stub_common(monkeypatch):
    monkeypatch.setattr(loop_v4, "classify", lambda task_text, context=None: dict(TIER3_DECISION))
    monkeypatch.setattr(loop_v4.routing_memory, "retrieve_similar_outcomes", lambda *a, **k: "")
    monkeypatch.setattr(loop_v4.routing_memory, "log_outcome", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4, "write", lambda *a, **k: None)


def test_confirmed_tier3_task_argv_reaches_loop_main_unchanged(monkeypatch):
    _stub_common(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "y")
    captured_argv = {}

    class _FakeLoop:
        @staticmethod
        def main():
            captured_argv["argv"] = list(sys.argv)

    with patch.dict(sys.modules, {"loop": _FakeLoop}):
        monkeypatch.setattr(sys, "argv", ["eo/loop_v4.py", "build", "a", "todo", "app"])
        loop_v4.main()
    assert captured_argv["argv"] == ["loop.py", "build a todo app"]


def test_declined_confirmation_never_calls_loop_main(monkeypatch):
    _stub_common(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    called = {"loop_main": False}

    class _FakeLoop:
        @staticmethod
        def main():
            called["loop_main"] = True

    with patch.dict(sys.modules, {"loop": _FakeLoop}):
        monkeypatch.setattr(sys, "argv", ["eo/loop_v4.py", "build", "a", "todo", "app"])
        loop_v4.main()
    assert called["loop_main"] is False


def test_empty_input_defaults_to_declining():
    # Part 8.1: the confirmation must default to NOT proceeding, not
    # silently treat a blank Enter-press as "yes."
    with patch("builtins.input", return_value=""):
        assert loop_v4._confirm_tier3(dict(TIER3_DECISION)) is False


def test_resume_with_no_task_reaches_loop_main_with_normalized_argv(monkeypatch):
    captured_argv = {}

    class _FakeLoop:
        @staticmethod
        def main():
            captured_argv["argv"] = list(sys.argv)

    with patch.dict(sys.modules, {"loop": _FakeLoop}):
        monkeypatch.setattr(sys, "argv", ["eo/loop_v4.py"])
        loop_v4.main()
    assert captured_argv["argv"] == ["loop.py"]


def test_manual_tier_override_bypasses_classification_tier():
    opts = loop_v4._parse_args(["--tier", "1", "reverse", "a", "string"])
    assert opts["tier"] == 1
    assert opts["task_text"] == "reverse a string"


if __name__ == "__main__":
    print("This test file uses pytest fixtures (monkeypatch/patch) — run via:")
    print("  python -m pytest tests/test_loop_v4_tier3.py -v")
