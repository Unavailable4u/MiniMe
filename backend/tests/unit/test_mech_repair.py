"""
tests/unit/test_mech_repair.py — G3d (Master Guide, "G3/G4. Hierarchical
parallel build + validate", "Local repair on failure, capped"): covers
eo/mech_repair.py --

  - _subset_for_nodes()'s Level 0->1 placement filtering
  - run_repair_loop()'s no-violations fast path (never calls
    regenerate_node_fn, never loops)
  - the pass-through when validate_layout() can't even run the first
    check (validator_error on the initial call)
  - a node that gets fixed on its first regeneration attempt
  - a node that only gets fixed on its second (last allowed) attempt
  - a node that's still violating after the retry cap -> flagged, not
    blocked
  - multiple simultaneous violations resolving independently (one
    fixed, one flagged)
  - a regenerate_node_fn that raises -- attempt still counts, node
    still gets its remaining retries (or gets flagged), never crashes
    the loop
  - validator_error appearing MID-repair (on a re-validation call, not
    the first) -- loop stops, whatever was outstanding at that moment
    is flagged, prior real fixes still reported as repaired
  - max_retries override

This module's own actual "generate" and "validate" halves (an LLM call
and a real FreeCAD sandbox, respectively) are both someone else's job --
eo/mech_validator.py (G3c, already covered by tests/unit/
test_mech_validator.py) and whatever G3e/F/G's own regenerate_node_fn
ends up calling. These tests only exercise this module's OWN
orchestration logic, so eo.mech_repair.validate_layout is monkeypatched
to a scripted stand-in returning a queue of canned results, and
regenerate_node_fn is a plain test double the test itself controls --
no real sandbox, LLM, or network call anywhere in this file.
"""
import pytest

import eo.mech_repair as mr


LEVEL = "0->1"


class _ScriptedValidate:
    """Stands in for eo.mech_validator.validate_layout(): returns each
    entry in `results` in order, one per call, and records the (mech,
    level) it was called with so a test can assert exactly what got
    re-validated on each round."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def __call__(self, mech, level, session_id=None, path=None, domain=None):
        self.calls.append({"mech": mech, "level": level})
        assert self._results, "validate_layout called more times than the test scripted"
        return self._results.pop(0)


def _install(monkeypatch, results):
    stub = _ScriptedValidate(results)
    monkeypatch.setattr(mr, "validate_layout", stub)
    return stub


def _mech(*part_ids):
    return {"placements": [{"part_id": pid, "w": 10, "h": 10, "d": 10, "primitives": [{"shape": "box"}]}
                            for pid in part_ids]}


# ---------------------------------------------------------------------------
# _subset_for_nodes
# ---------------------------------------------------------------------------

def test_subset_for_nodes_filters_by_part_id():
    mech = _mech("a", "b", "c")
    subset = mr._subset_for_nodes(mech, {"a", "c"})
    assert [p["part_id"] for p in subset["placements"]] == ["a", "c"]


def test_subset_for_nodes_handles_missing_placements():
    assert mr._subset_for_nodes({}, {"a"}) == {"placements": []}
    assert mr._subset_for_nodes(None, {"a"}) == {"placements": []}


# ---------------------------------------------------------------------------
# No violations / can't-even-check paths
# ---------------------------------------------------------------------------

def test_no_violations_is_a_fast_path(monkeypatch):
    _install(monkeypatch, [{"valid": True, "violations": []}])
    calls = []
    result = mr.run_repair_loop(_mech("a"), LEVEL, lambda *a: calls.append(a))
    assert result == {"valid": True, "violations": [], "attempts": {}, "repaired": []}
    assert calls == []  # never asked to regenerate anything


def test_validator_error_on_first_call_skips_repair_entirely(monkeypatch):
    _install(monkeypatch, [{"valid": True, "violations": [], "validator_error": "sandbox unreachable"}])
    calls = []
    result = mr.run_repair_loop(_mech("a"), LEVEL, lambda *a: calls.append(a))
    assert result == {"valid": True, "violations": [], "attempts": {}, "repaired": [],
                       "validator_error": "sandbox unreachable"}
    assert calls == []


# ---------------------------------------------------------------------------
# Successful repair
# ---------------------------------------------------------------------------

def test_node_fixed_on_first_attempt(monkeypatch):
    stub = _install(monkeypatch, [
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "pokes out"}]},
        {"valid": True, "violations": []},  # re-validate after 1 regen -> clean
    ])
    calls = []
    result = mr.run_repair_loop(_mech("motor_1"), LEVEL, lambda *a: calls.append(a))

    assert result["valid"] is True
    assert result["violations"] == []
    assert result["attempts"] == {"motor_1": 1}
    assert result["repaired"] == ["motor_1"]
    assert len(calls) == 1
    assert calls[0][1] == "motor_1"  # (mech, node_id, violation, attempt)
    assert calls[0][3] == 1
    # second validate_layout call was scoped to just the retried node
    assert [p["part_id"] for p in stub.calls[1]["mech"]["placements"]] == ["motor_1"]


def test_node_fixed_only_on_second_attempt(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "pokes out"}]},
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "still pokes out"}]},
        {"valid": True, "violations": []},
    ])
    calls = []
    result = mr.run_repair_loop(_mech("motor_1"), LEVEL, lambda *a: calls.append(a))

    assert result["valid"] is True
    assert result["attempts"] == {"motor_1": 2}
    assert result["repaired"] == ["motor_1"]
    assert [c[3] for c in calls] == [1, 2]  # attempt numbers passed through in order


def test_node_still_bad_after_cap_is_flagged_not_blocked(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "v1"}]},
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "v2"}]},
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "v3"}]},
    ])
    calls = []
    result = mr.run_repair_loop(_mech("motor_1"), LEVEL, lambda *a: calls.append(a))

    assert result["valid"] is False
    assert result["attempts"] == {"motor_1": 2}  # capped at DEFAULT_MAX_RETRIES
    assert [v["node_id"] for v in result["violations"]] == ["motor_1"]
    assert result["violations"][0]["issue"] == "v3"  # the LAST re-validate's issue text, not the first
    assert result["repaired"] == []
    assert len(calls) == 2  # never asked for a 3rd regeneration


def test_max_retries_override(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "v1"}]},
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "v2"}]},
    ])
    calls = []
    result = mr.run_repair_loop(_mech("motor_1"), LEVEL, lambda *a: calls.append(a), max_retries=1)

    assert result["valid"] is False
    assert result["attempts"] == {"motor_1": 1}
    assert len(calls) == 1


def test_two_violations_one_fixed_one_flagged(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [
            {"node_id": "motor_1", "issue": "m1 bad"},
            {"node_id": "battery_1", "issue": "b1 bad"},
        ]},
        # round 2: both regenerated, only motor_1 got fixed
        {"valid": False, "violations": [{"node_id": "battery_1", "issue": "b1 still bad"}]},
        {"valid": False, "violations": [{"node_id": "battery_1", "issue": "b1 still bad again"}]},
    ])
    calls = []
    result = mr.run_repair_loop(_mech("motor_1", "battery_1"), LEVEL, lambda *a: calls.append(a))

    assert result["valid"] is False
    assert result["repaired"] == ["motor_1"]
    assert [v["node_id"] for v in result["violations"]] == ["battery_1"]
    assert result["attempts"] == {"motor_1": 1, "battery_1": 2}


# ---------------------------------------------------------------------------
# regenerate_node_fn raising
# ---------------------------------------------------------------------------

def test_regenerate_exception_still_counts_as_an_attempt_and_eventually_flags(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "pokes out"}]},
    ])

    def _boom(mech, node_id, violation, attempt):
        raise RuntimeError("LLM call failed")

    result = mr.run_repair_loop(_mech("motor_1"), LEVEL, _boom, max_retries=1)

    assert result["valid"] is False
    assert result["attempts"] == {"motor_1": 1}
    assert "regeneration attempt failed" in result["violations"][0]["issue"]
    assert "pokes out" in result["violations"][0]["issue"]  # original issue text preserved


def test_regenerate_exception_gets_a_real_retry_after_the_failed_attempt(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [{"node_id": "motor_1", "issue": "pokes out"}]},
        {"valid": True, "violations": []},
    ])

    call_count = {"n": 0}

    def _flaky(mech, node_id, violation, attempt):
        call_count["n"] += 1
        if attempt == 1:
            raise RuntimeError("transient failure")
        # attempt 2 "succeeds" (test controls the outcome via the scripted validator)

    result = mr.run_repair_loop(_mech("motor_1"), LEVEL, _flaky)

    assert call_count["n"] == 2
    assert result["valid"] is True
    assert result["repaired"] == ["motor_1"]


# ---------------------------------------------------------------------------
# validator_error appearing mid-repair
# ---------------------------------------------------------------------------

def test_validator_error_mid_repair_flags_outstanding_and_keeps_prior_fixes(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [
            {"node_id": "motor_1", "issue": "m1 bad"},
            {"node_id": "battery_1", "issue": "b1 bad"},
        ]},
        {"valid": True, "violations": [], "validator_error": "sandbox died"},
    ])
    calls = []
    result = mr.run_repair_loop(_mech("motor_1", "battery_1"), LEVEL, lambda *a: calls.append(a))

    assert result["validator_error"] == "sandbox died"
    assert result["valid"] is False
    assert {v["node_id"] for v in result["violations"]} == {"motor_1", "battery_1"}
    assert result["repaired"] == []  # nothing was CONFIRMED fixed before the validator died
    assert len(calls) == 2  # both nodes got their one regeneration attempt before the validator died


def test_validator_error_mid_repair_after_an_earlier_confirmed_fix(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [
            {"node_id": "motor_1", "issue": "m1 bad"},
            {"node_id": "battery_1", "issue": "b1 bad"},
        ]},
        # round 2: motor_1 confirmed fixed, battery_1 still bad
        {"valid": False, "violations": [{"node_id": "battery_1", "issue": "still bad"}]},
        # round 3 re-validate for battery_1's 2nd attempt: validator dies
        {"valid": True, "violations": [], "validator_error": "sandbox died"},
    ])
    result = mr.run_repair_loop(_mech("motor_1", "battery_1"), LEVEL, lambda *a: None)

    assert result["validator_error"] == "sandbox died"
    assert result["repaired"] == ["motor_1"]  # confirmed in round 2, preserved through the round-3 failure
    assert [v["node_id"] for v in result["violations"]] == ["battery_1"]
