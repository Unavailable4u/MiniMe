"""
tests/integration/test_loop_v4_tier3.py — Part 11: tier-3 routing
coverage for eo/loop_v4.py.

Moved from tests/test_loop_v4_tier3.py (B1 audit) and rewritten: the
original file tested a "confirmed tier-3 decision hands off to
loop.main() with the exact argv loop.py would have received" contract
that no longer exists. loop.py has been fully retired (see
eo/loop_v4.py's own comment above its tier==3 branch: "Migration Part
14 §1/§3: _ensure_staffable() guarantees hires is never empty for a
tier-3 task now, so the old 'else: loop.py' fallback is gone — there's
no case left for it to catch"). main() no longer calls _confirm_tier3()
at all -- tier 3 now goes straight to _run_tier3_hires(), which is a
thin CLI wrapper around eo/loop_controller.py's run_with_looping().

This file now checks the two things that replaced the old contract:
  1. A tier-3 decision reaches _run_tier3_hires() -> run_with_looping()
     with the hires/execution_order/mode the decision produced, and
     main() prints the final role's answer from the returned results.
  2. _confirm_tier3() itself (Part 8.1's cost-ceiling confirmation) is
     kept and still defaults to NOT proceeding on a blank/declined
     input -- it's no longer wired into main()'s tier-3 branch, but the
     function is still exported and still used elsewhere (e.g.
     api/task_runner.py's own confirmation flow), so its own default-
     deny contract stays covered here directly rather than assuming
     it's exercised transitively.
Plus the untouched manual --tier override parsing check from the
original file, which never depended on loop.py at all.
"""
from unittest.mock import patch

from eo import loop_v4

TIER3_DECISION = {
    "path": "adaptive", "tier": 3, "directed_task_type": None, "confidence": 0.9,
    "suggested_agents": ["writer"], "reasoning": "ongoing multi-cycle project",
    "panel_reviewed": False, "domain": None, "execution_order": ["writer"],
}


def _stub_common(monkeypatch):
    monkeypatch.setattr(loop_v4, "classify", lambda task_text, context=None, session_id=None: dict(TIER3_DECISION))
    # eo/loop_v4.py's _get_decision() escalates to the real panel whenever
    # draft["tier"] >= 2 -- unconditionally, regardless of confidence. Every
    # decision this file's tests use is tier 3, so that branch always
    # fires and calls the REAL eo_panel.run_panel(task_text, draft), which
    # makes real LLM calls unless mocked here too. Without this, whether
    # this test asserts on the mocked TIER3_DECISION or on whatever a live
    # panel actually returns depends entirely on whether real provider
    # credentials happen to be configured in the environment running it.
    monkeypatch.setattr(loop_v4.eo_panel, "run_panel", lambda task_text, draft: dict(draft))
    monkeypatch.setattr(loop_v4, "check_cache", lambda *a, **k: None)  # see test_loop_v4_tier0.py's comment
    monkeypatch.setattr(loop_v4, "write_cache", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4.routing_memory, "retrieve_similar_outcomes", lambda *a, **k: "")
    monkeypatch.setattr(loop_v4.routing_memory, "log_outcome", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4, "write", lambda *a, **k: None)
    monkeypatch.setattr(loop_v4.conversation_memory, "get_light_context", lambda *a, **k: None)
    monkeypatch.setattr(
        loop_v4, "staff_task",
        lambda decision, task_text=None, session_id=None: [{"role": "writer", "agent_key": "FAKE_KEY", "brief": "write it"}],
    )


def test_tier3_decision_reaches_run_with_looping_and_prints_final_answer(monkeypatch, capsys):
    _stub_common(monkeypatch)
    captured = {}

    def fake_run_with_looping(hires, execution_order, task_text, session_id=None,
                               mode=None, domain=None, project_unique_name=None, path=None):
        captured["hires"] = hires
        captured["execution_order"] = execution_order
        captured["task_text"] = task_text
        captured["path"] = path
        captured["session_id"] = session_id
        return {"results": {"writer": {"text": "a finished multi-cycle app"}}, "final_role": "writer"}

    monkeypatch.setattr(loop_v4, "run_with_looping", fake_run_with_looping)

    import sys
    old_argv = sys.argv
    try:
        sys.argv = ["eo/loop_v4.py", "build", "and", "keep", "improving", "a", "todo", "app"]
        loop_v4.main()
    finally:
        sys.argv = old_argv

    assert captured["hires"] == [{"role": "writer", "agent_key": "FAKE_KEY", "brief": "write it"}]
    assert captured["execution_order"] == ["writer"]
    assert captured["path"] == "adaptive"
    # Patch B8: run_with_looping() must now receive a real (non-None)
    # session_id from the CLI path, not the old hard-coded None.
    assert captured["session_id"] is not None
    out = capsys.readouterr().out
    assert "a finished multi-cycle app" in out


def test_empty_input_defaults_to_declining():
    # Part 8.1: the confirmation must default to NOT proceeding, not
    # silently treat a blank Enter-press as "yes." Still covered
    # directly even though main() no longer calls this itself (see
    # module docstring) -- the function's own contract must not regress.
    with patch("builtins.input", return_value=""):
        assert loop_v4._confirm_tier3(dict(TIER3_DECISION)) is False


def test_declined_confirmation_still_returns_false_not_raises():
    with patch("builtins.input", return_value="n"):
        assert loop_v4._confirm_tier3(dict(TIER3_DECISION)) is False


def test_confirmed_input_returns_true():
    with patch("builtins.input", return_value="y"):
        assert loop_v4._confirm_tier3(dict(TIER3_DECISION)) is True


def test_manual_tier_override_bypasses_classification_tier():
    opts = loop_v4._parse_args(["--tier", "1", "reverse", "a", "string"])
    assert opts["tier"] == 1
    assert opts["task_text"] == "reverse a string"
