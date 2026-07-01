"""
tests/test_loop_v4_tier3.py — Part 11: "confirm loop_v4.py correctly
hands off to the unmodified loop.py and produces identical behavior to
calling loop.py directly."

This does NOT run a real cycle (that needs real API keys and Upstash and
is what test_eo_inspector.py's live-fixture path is for). It checks the
one thing this stage is actually about: that eo/loop_v4.py's main()
ultimately calls loop.main() with the exact same argv loop.py would have
received directly, and does not fork loop.py's logic in any way.
"""
import os
import sys
from unittest.mock import patch

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eo.loop_v4 as loop_v4


def test_task_argv_reaches_loop_main_unchanged(monkeypatch):
    monkeypatch.setattr(loop_v4, "run_inspector_and_log", lambda task_text: None)

    captured_argv = {}

    class _FakeLoop:
        @staticmethod
        def main():
            captured_argv["argv"] = list(sys.argv)

    with patch.dict(sys.modules, {"loop": _FakeLoop}):
        monkeypatch.setattr(sys, "argv", ["eo/loop_v4.py", "build", "a", "todo", "app"])
        loop_v4.main()

    assert captured_argv["argv"] == ["loop.py", "build", "a", "todo", "app"]


def test_resume_with_no_task_reaches_loop_main_with_no_extra_args(monkeypatch):
    captured_argv = {}

    class _FakeLoop:
        @staticmethod
        def main():
            captured_argv["argv"] = list(sys.argv)

    with patch.dict(sys.modules, {"loop": _FakeLoop}):
        monkeypatch.setattr(sys, "argv", ["eo/loop_v4.py"])
        loop_v4.main()

    assert captured_argv["argv"] == ["loop.py"]


def test_manual_tier_override_flag_is_accepted_but_stripped(monkeypatch):
    # Stage 4.2: --tier is accepted (won't break future scripts/CI) but
    # currently has zero effect on execution — tier is always forced to 3.
    monkeypatch.setattr(loop_v4, "run_inspector_and_log", lambda task_text: None)
    task_text, remaining = loop_v4._parse_task_arg(
        ["--tier", "3", "build", "and", "keep", "improving", "a", "todo", "app"]
    )
    assert "--tier" not in remaining
    assert task_text == "build and keep improving a todo app"


if __name__ == "__main__":
    print("This test file uses pytest fixtures (monkeypatch) — run via:")
    print("  python -m pytest tests/test_loop_v4_tier3.py -v")
