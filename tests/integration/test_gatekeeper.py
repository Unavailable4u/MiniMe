"""
tests/integration/test_gatekeeper.py — replaces the old
tests/test_gatekeeper.py, which is dead the same way
tests/test_changelog_writer.py was (see
tests/integration/test_generic_worker_legacy_roles.py's docstring): it
did `from agents.gatekeeper import run_gatekeeper, MAX_CYCLES,
HUMAN_CHECKPOINT_EVERY`, but agents/gatekeeper.py no longer exists.

Per eo/registry.py's Migration Part 27 comment, "gatekeeper" (along with
changelog_writer/final_qa) was retired as a dedicated module -- the role
name stays valid, but eo/loop_controller.py's _run_gatekeeper() is now
the ONLY thing that asks it anything, once per completed macro-loop pass,
via generic_worker's role="gatekeeper" path. It's a fundamentally
different shape than the old per-cycle build-loop gatekeeper the dead
test exercised (no MAX_CYCLES/HUMAN_CHECKPOINT_EVERY constants exist
anymore either) -- this test covers the CURRENT contract:

  - _hard_safety_check()'s three deterministic rules (checked BEFORE any
    LLM call, so they must never touch generate_text)
  - the LLM-judgment path once none of those rules fire, resolved through
    generic_worker exactly like test_generic_worker_legacy_roles.py's
    changelog_writer case
"""
import importlib

import pytest

import agents.generic_worker as generic_worker  # noqa: F401  (ensures mock_llm patches this module)
import eo.loop_controller as loop_controller
from eo.loop_controller import MAX_MACRO_LOOPS, FORCED_CHECKPOINT_EVERY


CRITICAL_ISSUE_RESULTS = {
    "verifier": {
        "issues": [
            {"module": "todo_api", "severity": "critical", "description": "undefined global 'storage'"},
        ],
        "summary": "One critical bug, unresolved.",
    },
}

CLEAN_RESULTS = {
    "verifier": {"issues": [], "summary": "No issues."},
}


def test_no_dedicated_gatekeeper_module_exists_anymore():
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("agents.gatekeeper")


def test_hard_cap_stops_without_reaching_the_llm(mock_llm):
    decision = loop_controller._run_gatekeeper(
        CLEAN_RESULTS, "build a todo app", "sess_test", MAX_MACRO_LOOPS,
    )

    assert decision["action"] == "STOP"
    assert decision["cause"] == "hard_cap"
    assert mock_llm.mock.call_count == 0


def test_forced_checkpoint_is_currently_unreachable_given_max_macro_loops(mock_llm):
    """BUG FOUND while building this test, not a design choice: with the
    real constants (MAX_MACRO_LOOPS = 3, FORCED_CHECKPOINT_EVERY = 5),
    _hard_safety_check()'s hard_cap rule ("loop_num >= MAX_MACRO_LOOPS")
    is checked BEFORE the forced_checkpoint rule, and there is no
    loop_num that is both < 3 (so hard_cap hasn't already fired) and a
    positive multiple of 5 (so forced_checkpoint would fire). The
    forced_checkpoint branch can never execute in production as
    currently configured -- confirmed here across every loop_num from 1
    through a generous margin past FORCED_CHECKPOINT_EVERY, all landing
    on hard_cap (or the LLM path) instead. Either MAX_MACRO_LOOPS needs
    to be raised well past FORCED_CHECKPOINT_EVERY, or the two rules'
    check order needs to swap -- flagging rather than silently
    "fixing" the constants here, since only the author knows which
    fix matches the intended behavior."""
    for loop_num in range(1, FORCED_CHECKPOINT_EVERY * 2 + 1):
        decision = loop_controller._run_gatekeeper(
            CLEAN_RESULTS, "build a todo app", "sess_test", loop_num,
        )
        if loop_num % FORCED_CHECKPOINT_EVERY == 0:
            assert decision.get("cause") != "forced_checkpoint", (
                f"loop_num={loop_num}: forced_checkpoint fired -- if this "
                f"assertion is what failed, the bug above has been fixed; "
                f"update/remove this regression test accordingly"
            )


def test_forced_checkpoint_logic_is_correct_in_isolation(mock_llm, monkeypatch):
    """The modulo logic itself is correct -- only the real MAX_MACRO_LOOPS
    vs FORCED_CHECKPOINT_EVERY relationship makes it unreachable (see the
    test above). Proven here by giving the checkpoint rule room to fire
    before the hard cap would."""
    monkeypatch.setattr(loop_controller, "MAX_MACRO_LOOPS", 100)
    monkeypatch.setattr(loop_controller, "FORCED_CHECKPOINT_EVERY", 5)

    decision = loop_controller._run_gatekeeper(
        CLEAN_RESULTS, "build a todo app", "sess_test", 5,
    )

    assert decision["action"] == "PAUSE_FOR_HUMAN"
    assert decision["cause"] == "forced_checkpoint"
    assert mock_llm.mock.call_count == 0


def test_repeat_failure_breaker_pauses_on_the_second_occurrence(mock_llm):
    mock_llm.set_response("CONTINUE: verifier")

    # First time seeing this critical issue: no hard rule fires yet, so
    # this pass genuinely reaches the LLM.
    first = loop_controller._run_gatekeeper(
        CRITICAL_ISSUE_RESULTS, "build a todo app", "sess_test", 1,
    )
    assert first["action"] == "CONTINUE"
    assert mock_llm.mock.call_count == 1

    # Same critical issue, next loop: the repeat-failure breaker must
    # catch it deterministically WITHOUT a second LLM call.
    second = loop_controller._run_gatekeeper(
        CRITICAL_ISSUE_RESULTS, "build a todo app", "sess_test", 2,
    )
    assert second["action"] == "PAUSE_FOR_HUMAN"
    assert second["cause"] == "repeat_failure"
    assert mock_llm.mock.call_count == 1, "the breaker must not need a second LLM call"


def test_no_repeat_when_the_critical_issue_actually_changes(mock_llm):
    mock_llm.set_response("CONTINUE: verifier")
    loop_controller._run_gatekeeper(CRITICAL_ISSUE_RESULTS, "task", "sess_test", 1)

    different_issue = {
        "verifier": {
            "issues": [{"module": "todo_storage", "severity": "critical", "description": "off-by-one"}],
            "summary": "A different critical bug.",
        },
    }
    decision = loop_controller._run_gatekeeper(different_issue, "task", "sess_test", 2)

    assert decision["action"] == "CONTINUE", "a genuinely different critical issue must not trip the breaker"


def test_llm_judgment_stop_when_no_hard_rule_fires(mock_llm):
    mock_llm.set_response("STOP")

    decision = loop_controller._run_gatekeeper(CLEAN_RESULTS, "task", "sess_test", 1)

    assert decision == {"action": "STOP"}
    assert mock_llm.mock.call_count == 1


def test_llm_judgment_continue_parses_redo_roles(mock_llm):
    mock_llm.set_response("CONTINUE: verifier, fixer")

    decision = loop_controller._run_gatekeeper(CLEAN_RESULTS, "task", "sess_test", 1)

    assert decision["action"] == "CONTINUE"
    assert decision["redo_roles"] == ["verifier", "fixer"]


def test_gatekeeper_role_resolves_through_generic_worker(mock_llm):
    """Confirms the actual resolution path this test file's docstring
    describes: _run_gatekeeper() reaches generate_text() via
    agents.generic_worker.run(role="gatekeeper", ...), not any
    standalone module."""
    mock_llm.set_response("STOP")

    loop_controller._run_gatekeeper(CLEAN_RESULTS, "task", "sess_test", 1)

    assert mock_llm.mock.called
    call_kwargs = mock_llm.mock.call_args.kwargs
    assert call_kwargs.get("agent_name") == "generic:gatekeeper"
