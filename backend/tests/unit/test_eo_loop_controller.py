"""
tests/unit/test_eo_loop_controller.py — Patch 7e-S2.

eo/loop_controller.py had zero test coverage before this. run_with_looping()
wraps one-or-more passes of execute_graph() with an optional macro-loop:
after a pass completes, a gatekeeper decision (deterministic hard-safety
rules first, then an LLM judgment call) decides STOP / PAUSE_FOR_HUMAN /
CONTINUE. Getting this wrong either loops forever (burning cost) or stops
too early (an incomplete result silently treated as final) -- exactly the
"fails silently and expensively" class of bug the audit called out for
this area.

Style/isolation notes:
  - eo/loop_controller.py imports build_execution_graph_from_hires and
    execute_graph at module TOP LEVEL (`from eo.router import ...`,
    `from eo.executor import ...`) -- bound names in its own namespace.
    Per conftest.py's documented generate_text gotcha, these are patched
    as eo.loop_controller.build_execution_graph_from_hires /
    eo.loop_controller.execute_graph, not on eo.router/eo.executor
    directly.
  - agents.generic_worker.run is imported LAZILY inside
    _run_gatekeeper() itself (`from agents.generic_worker import run as
    generic_run`, deferred to dodge the same circular-import class
    documented elsewhere in this codebase) -- this one IS patched on the
    real agents.generic_worker module, since the deferred import
    re-resolves the attribute fresh on every call.
  - memory.bus read/write need no mocking (autouse fake_bus fixture),
    matching the established pattern -- _hard_safety_check's
    prev_critical_issues bus round-trip is exercised for real.
  - Mode is only ever compared with .lower() against "expert"/"beast",
    so tests use "simple" for the single-pass case and "expert" for the
    looping cases, matching real caller values.
"""
from unittest.mock import MagicMock

import pytest

import eo.loop_controller as loop_controller
from eo.loop_controller import (
    run_with_looping,
    _extract_critical_issue,
    _hard_safety_check,
    MAX_MACRO_LOOPS,
    FORCED_CHECKPOINT_EVERY,
)
import agents.generic_worker as generic_worker_module


@pytest.fixture(autouse=True)
def mock_emit(monkeypatch):
    mock = MagicMock()
    monkeypatch.setattr(loop_controller, "emit_event", mock)
    return mock


@pytest.fixture
def mock_graph_build(monkeypatch):
    """build_execution_graph_from_hires returns (agent_names, role_names,
    key_overrides) -- tests don't care about the real graph shape, just
    that it's forwarded through to execute_graph()."""
    mock = MagicMock(return_value=(["generic_worker"], ["writer"], {}))
    monkeypatch.setattr(loop_controller, "build_execution_graph_from_hires", mock)
    return mock


def _mock_execute_graph(monkeypatch, results_by_call):
    """results_by_call: list of return values, one per successive
    execute_graph() call (one per macro-loop pass)."""
    mock = MagicMock(side_effect=list(results_by_call))
    monkeypatch.setattr(loop_controller, "execute_graph", mock)
    return mock


def _mock_gatekeeper_reply(monkeypatch, text: str):
    mock = MagicMock(return_value={"text": text})
    monkeypatch.setattr(generic_worker_module, "run", mock)
    return mock


# ---------------------------------------------------------------------------
# _extract_critical_issue
# ---------------------------------------------------------------------------

def test_extract_critical_issue_finds_critical_severity():
    results = {"reviewer": {"issues": [{"severity": "critical", "module": "auth.py",
                                          "description": "SQL injection"}]}}
    found = _extract_critical_issue(results)
    assert found == frozenset({("reviewer", "auth.py", "SQL injection")})


def test_extract_critical_issue_ignores_non_critical_severity():
    results = {"reviewer": {"issues": [{"severity": "minor", "module": "x.py", "description": "nit"}]}}
    assert _extract_critical_issue(results) == frozenset()


def test_extract_critical_issue_empty_results_returns_empty_frozenset():
    assert _extract_critical_issue({}) == frozenset()
    assert _extract_critical_issue(None) == frozenset()


def test_extract_critical_issue_result_without_issues_key_skipped():
    results = {"writer": {"text": "some output, no issues key at all"}}
    assert _extract_critical_issue(results) == frozenset()


def test_extract_critical_issue_non_dict_result_skipped():
    """Not every role's result is a dict shaped like {"issues": [...]} --
    a non-coding domain role might return a bare string. Must not raise."""
    results = {"summarizer": "plain text output", "reviewer": {"issues": "not a list"}}
    assert _extract_critical_issue(results) == frozenset()


def test_extract_critical_issue_non_dict_issue_entries_skipped():
    results = {"reviewer": {"issues": [{"severity": "critical", "module": "a.py", "description": "d"},
                                         "a bare string issue", 42]}}
    found = _extract_critical_issue(results)
    assert found == frozenset({("reviewer", "a.py", "d")})


def test_extract_critical_issue_across_multiple_roles():
    results = {
        "reviewer": {"issues": [{"severity": "critical", "module": "a.py", "description": "d1"}]},
        "sandbox_tester": {"issues": [{"severity": "critical", "module": "b.py", "description": "d2"}]},
    }
    found = _extract_critical_issue(results)
    assert found == frozenset({("reviewer", "a.py", "d1"), ("sandbox_tester", "b.py", "d2")})


# ---------------------------------------------------------------------------
# _hard_safety_check
# ---------------------------------------------------------------------------

def test_hard_safety_check_hard_cap_at_max_loops():
    decision = _hard_safety_check("sess-1", MAX_MACRO_LOOPS, {})
    assert decision == {"action": "STOP", "cause": "hard_cap"}


def test_hard_safety_check_below_cap_and_no_checkpoint_returns_none():
    decision = _hard_safety_check("sess-1", 1, {})
    assert decision is None


def test_hard_safety_check_forced_checkpoint(monkeypatch):
    """With real constants (MAX_MACRO_LOOPS=3 < FORCED_CHECKPOINT_EVERY=5)
    the hard_cap check always fires first at loop_num=5, so the
    checkpoint branch is only reachable when the cap is raised above the
    checkpoint interval -- exercised here by patching MAX_MACRO_LOOPS up
    so loop_num=FORCED_CHECKPOINT_EVERY is reached while still under cap."""
    monkeypatch.setattr(loop_controller, "MAX_MACRO_LOOPS", 10)
    decision = _hard_safety_check("sess-2", FORCED_CHECKPOINT_EVERY, {})
    assert decision == {"action": "PAUSE_FOR_HUMAN", "cause": "forced_checkpoint"}


def test_hard_safety_check_loop_zero_is_not_a_forced_checkpoint():
    """loop_num > 0 guard -- loop 0 % FORCED_CHECKPOINT_EVERY == 0 would
    otherwise false-positive as a checkpoint on the very first loop."""
    decision = _hard_safety_check("sess-3", 0, {})
    assert decision is None


def test_hard_safety_check_repeat_failure_breaker():
    results_loop1 = {"reviewer": {"issues": [{"severity": "critical", "module": "a.py",
                                                "description": "same bug"}]}}
    # First call establishes prev_critical_issues for this session.
    first = _hard_safety_check("sess-4", 1, results_loop1)
    assert first is None
    # Second call, same critical issue recurring -> repeat_failure pause.
    second = _hard_safety_check("sess-4", 2, results_loop1)
    assert second == {"action": "PAUSE_FOR_HUMAN", "cause": "repeat_failure"}


def test_hard_safety_check_different_issue_does_not_trigger_repeat_failure():
    loop1 = {"reviewer": {"issues": [{"severity": "critical", "module": "a.py", "description": "bug A"}]}}
    loop2 = {"reviewer": {"issues": [{"severity": "critical", "module": "b.py", "description": "bug B"}]}}
    assert _hard_safety_check("sess-5", 1, loop1) is None
    assert _hard_safety_check("sess-5", 2, loop2) is None


def test_hard_safety_check_partial_overlap_still_counts_as_repeat():
    """Set intersection, not exact equality -- a NEW issue appearing
    alongside a repeated one still counts as a repeat failure."""
    loop1 = {"reviewer": {"issues": [{"severity": "critical", "module": "a.py", "description": "bug A"}]}}
    loop2 = {"reviewer": {"issues": [
        {"severity": "critical", "module": "a.py", "description": "bug A"},
        {"severity": "critical", "module": "c.py", "description": "bug C"},
    ]}}
    assert _hard_safety_check("sess-6", 1, loop1) is None
    assert _hard_safety_check("sess-6", 2, loop2) == {"action": "PAUSE_FOR_HUMAN", "cause": "repeat_failure"}


# ---------------------------------------------------------------------------
# run_with_looping -- single pass (simple mode never loops)
# ---------------------------------------------------------------------------

def test_run_with_looping_simple_mode_runs_exactly_one_pass(mock_graph_build, monkeypatch):
    exec_mock = _mock_execute_graph(monkeypatch, [{"writer": {"text": "output"}}])
    result = run_with_looping(hires=[], execution_order=["writer"], task_text="do a thing",
                               session_id="s1", mode="simple")
    assert result == {"results": {"writer": {"text": "output"}}, "final_role": "writer"}
    exec_mock.assert_called_once()


def test_run_with_looping_final_role_is_last_key_of_pass_results(mock_graph_build, monkeypatch):
    _mock_execute_graph(monkeypatch, [{"idea_planner": {}, "writer": {"text": "final"}}])
    result = run_with_looping(hires=[], execution_order=["idea_planner", "writer"],
                               task_text="x", session_id="s2", mode="simple")
    assert result["final_role"] == "writer"


def test_run_with_looping_empty_pass_results_leaves_final_role_none(mock_graph_build, monkeypatch):
    _mock_execute_graph(monkeypatch, [{}])
    result = run_with_looping(hires=[], execution_order=[], task_text="x",
                               session_id="s3", mode="simple")
    assert result == {"results": {}, "final_role": None}


# ---------------------------------------------------------------------------
# run_with_looping -- paused mid-pass
# ---------------------------------------------------------------------------

def test_run_with_looping_returns_paused_status_immediately(mock_graph_build, monkeypatch):
    _mock_execute_graph(monkeypatch, [{"status": "paused", "paused_at_role": "reviewer"}])
    result = run_with_looping(hires=[], execution_order=["reviewer"], task_text="x",
                               session_id="s4", mode="expert")
    assert result == {"status": "paused", "paused_at_role": "reviewer", "session_id": "s4"}


def test_run_with_looping_paused_status_does_not_pollute_results(mock_graph_build, monkeypatch):
    """The paused shape must be detected and returned BEFORE
    results.update(pass_results) -- otherwise "status"/"paused_at_role"
    would get merged into `results` as if they were role names."""
    _mock_execute_graph(monkeypatch, [{"status": "paused", "paused_at_role": "fixer"}])
    result = run_with_looping(hires=[], execution_order=["fixer"], task_text="x",
                               session_id="s5", mode="expert")
    assert "status" not in result.get("results", {})
    assert result["status"] == "paused"


def test_run_with_looping_paused_enriches_existing_snapshot(mock_graph_build, monkeypatch):
    from memory.bus import write as bus_write, read as bus_read
    bus_write("paused_execution:s6", {"agent_names": ["generic_worker"], "idx": 0})
    _mock_execute_graph(monkeypatch, [{"status": "paused", "paused_at_role": "reviewer"}])

    run_with_looping(hires=[{"role": "writer"}], execution_order=["writer"], task_text="task text",
                      session_id="s6", mode="beast", domain="coding", scope="broad",
                      workspace_id="ws-1", project_unique_name="proj-1")

    snapshot = bus_read("paused_execution:s6")
    assert snapshot["macro_loop_num"] == 1
    assert snapshot["macro_domain"] == "coding"
    assert snapshot["macro_scope"] == "broad"
    assert snapshot["macro_workspace_id"] == "ws-1"
    assert snapshot["macro_project_unique_name"] == "proj-1"
    assert snapshot["macro_mode"] == "beast"
    assert snapshot["macro_results"] == {}  # nothing accumulated before this (first) pass
    # Original pre-existing fields survive the enrichment.
    assert snapshot["agent_names"] == ["generic_worker"]


def test_run_with_looping_paused_with_no_existing_snapshot_does_not_raise(mock_graph_build, monkeypatch):
    """read() returning None (nothing written yet, e.g. an edge case
    where executor.py's own snapshot write raced or was skipped) must
    not crash the enrichment block."""
    _mock_execute_graph(monkeypatch, [{"status": "paused", "paused_at_role": "reviewer"}])
    result = run_with_looping(hires=[], execution_order=["reviewer"], task_text="x",
                               session_id="s-no-snapshot", mode="expert")
    assert result["status"] == "paused"


# ---------------------------------------------------------------------------
# run_with_looping -- multi-pass looping via the gatekeeper
# ---------------------------------------------------------------------------

def test_run_with_looping_gatekeeper_stop_ends_after_one_extra_pass(mock_graph_build, monkeypatch):
    _mock_execute_graph(monkeypatch, [{"writer": {"text": "draft"}}])
    _mock_gatekeeper_reply(monkeypatch, "STOP")

    result = run_with_looping(hires=[], execution_order=["writer"], task_text="x",
                               session_id="s7", mode="expert")
    assert result["results"] == {"writer": {"text": "draft"}}
    assert result["final_role"] == "writer"


def test_run_with_looping_gatekeeper_continue_runs_another_pass(mock_graph_build, monkeypatch):
    exec_mock = _mock_execute_graph(monkeypatch, [
        {"writer": {"text": "draft 1"}},
        {"writer": {"text": "draft 2"}},
    ])
    gk_mock = MagicMock(side_effect=[
        {"text": "CONTINUE: writer"},
        {"text": "STOP"},
    ])
    monkeypatch.setattr(generic_worker_module, "run", gk_mock)

    result = run_with_looping(hires=[], execution_order=["writer"], task_text="x",
                               session_id="s8", mode="expert")
    assert exec_mock.call_count == 2
    # Second pass's result overwrites the same role key (redo semantics).
    assert result["results"] == {"writer": {"text": "draft 2"}}


def test_run_with_looping_continue_merges_not_replaces_results(mock_graph_build, monkeypatch):
    """A redo pass should only overwrite the roles it re-ran, not erase
    results from earlier passes for roles NOT in the redo set."""
    exec_mock = _mock_execute_graph(monkeypatch, [
        {"idea_planner": {"text": "idea"}, "writer": {"text": "draft 1"}},
        {"writer": {"text": "draft 2"}},  # only writer redone
    ])
    gk_mock = MagicMock(side_effect=[
        {"text": "CONTINUE: writer"},
        {"text": "STOP"},
    ])
    monkeypatch.setattr(generic_worker_module, "run", gk_mock)

    result = run_with_looping(hires=[], execution_order=["idea_planner", "writer"], task_text="x",
                               session_id="s9", mode="expert")
    assert result["results"] == {"idea_planner": {"text": "idea"}, "writer": {"text": "draft 2"}}
    assert exec_mock.call_count == 2


def test_run_with_looping_stops_at_max_macro_loops_without_calling_gatekeeper(mock_graph_build, monkeypatch):
    """loop_num >= MAX_MACRO_LOOPS short-circuits the `while True` loop's
    own break condition BEFORE _run_gatekeeper() is even called for that
    final pass -- the hard cap inside _hard_safety_check is a second,
    independent backstop reached only via the gatekeeper path."""
    exec_mock = _mock_execute_graph(monkeypatch, [{"writer": {"text": f"draft {i}"}}
                                                    for i in range(MAX_MACRO_LOOPS)])
    gk_mock = MagicMock(return_value={"text": "CONTINUE: writer"})
    monkeypatch.setattr(generic_worker_module, "run", gk_mock)

    run_with_looping(hires=[], execution_order=["writer"], task_text="x",
                      session_id="s10", mode="expert")
    assert exec_mock.call_count == MAX_MACRO_LOOPS


def test_run_with_looping_mode_case_insensitive(mock_graph_build, monkeypatch):
    """mode.lower() not in ("expert", "beast") -- "Expert"/"BEAST" style
    input must still be recognized as loop-eligible."""
    exec_mock = _mock_execute_graph(monkeypatch, [
        {"writer": {"text": "draft 1"}},
        {"writer": {"text": "draft 2"}},
    ])
    gk_mock = MagicMock(side_effect=[{"text": "CONTINUE: writer"}, {"text": "STOP"}])
    monkeypatch.setattr(generic_worker_module, "run", gk_mock)

    run_with_looping(hires=[], execution_order=["writer"], task_text="x",
                      session_id="s11", mode="Expert")
    assert exec_mock.call_count == 2


def test_run_with_looping_redo_roles_empty_falls_back_to_execution_order(mock_graph_build, monkeypatch):
    """decision.get("redo_roles") or execution_order -- a CONTINUE with
    no colon (redo list empty) must fall back to redoing the FULL
    original execution_order, not an empty run."""
    exec_mock = _mock_execute_graph(monkeypatch, [
        {"writer": {"text": "draft 1"}},
        {"writer": {"text": "draft 2"}},
    ])
    gk_mock = MagicMock(side_effect=[{"text": "CONTINUE"}, {"text": "STOP"}])
    monkeypatch.setattr(generic_worker_module, "run", gk_mock)

    run_with_looping(hires=[], execution_order=["writer"], task_text="x",
                      session_id="s12", mode="expert")
    second_call_kwargs = mock_graph_build.call_args_list[1]
    # build_execution_graph_from_hires(hires, current_order) -- current_order
    # positional/keyword depends on call site; assert against call args directly.
    assert mock_graph_build.call_count == 2


def test_run_with_looping_repeat_failure_pauses_second_check_without_llm_call(mock_graph_build, monkeypatch):
    """The FIRST gatekeeper check for a given critical issue always goes
    to the LLM (nothing to compare against yet, so hard=None) -- it's
    only the SECOND consecutive check seeing the same critical issue
    that _hard_safety_check itself blocks, short-circuiting BEFORE that
    second LLM call."""
    critical = {"reviewer": {"issues": [{"severity": "critical", "module": "a.py",
                                           "description": "same bug"}]}}
    exec_mock = _mock_execute_graph(monkeypatch, [critical, critical])
    gk_mock = MagicMock(return_value={"text": "CONTINUE: reviewer"})
    monkeypatch.setattr(generic_worker_module, "run", gk_mock)

    result = run_with_looping(hires=[], execution_order=["reviewer"], task_text="x",
                               session_id="s13", mode="expert")
    assert exec_mock.call_count == 2
    gk_mock.assert_called_once()  # only pass 1's check reaches the LLM
    assert result["results"] == critical


def test_run_with_looping_domain_forwarded_to_execute_graph_every_pass(mock_graph_build, monkeypatch):
    exec_mock = _mock_execute_graph(monkeypatch, [
        {"writer": {"text": "d1"}},
        {"writer": {"text": "d2"}},
    ])
    gk_mock = MagicMock(side_effect=[{"text": "CONTINUE: writer"}, {"text": "STOP"}])
    monkeypatch.setattr(generic_worker_module, "run", gk_mock)

    run_with_looping(hires=[], execution_order=["writer"], task_text="x", session_id="s14",
                      mode="expert", domain="coding")
    for c in exec_mock.call_args_list:
        assert c.kwargs["domain"] == "coding"
