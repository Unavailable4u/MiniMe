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
  - run_level_1_2_repair() (G3e-4) and run_level_2_3_repair() (G3f-2):
    each level's own top-level driver -- repair loop + final full
    re-validate + persisting footprints onto mech["subsections"] /
    mech["sections"] respectively

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

    def __call__(self, mech, level, session_id=None, path=None, domain=None, parts=None):
        self.calls.append({"mech": mech, "level": level, "parts": parts})
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
# _subset_for_nodes -- Level 1->2 (G3e-4): node_ids are subsection_ids,
# and the subset must keep BOTH a kept subsection's anchor placement AND
# its "mount_"-prefixed sibling (see this function's own docstring on why
# dropping the mount would silently re-derive a singleton).
# ---------------------------------------------------------------------------

def _subsection_mech():
    return {"placements": [
        {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5},
        {"part_id": "mount_mcu_1", "x": 0, "y": 20, "z": 0, "w": 30, "h": 5, "d": 5},
        {"part_id": "sensor_1", "x": 50, "y": 0, "z": 0, "w": 20, "h": 15, "d": 5},
        {"part_id": "mount_sensor_1", "x": 50, "y": 15, "z": 0, "w": 20, "h": 5, "d": 5},
        {"part_id": "battery_1", "x": 100, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10},
    ]}


def test_subset_for_nodes_level_1_2_keeps_anchor_and_mount():
    mech = _subsection_mech()
    subset = mr._subset_for_nodes(mech, {"mcu_1"}, level=mr.LEVEL_1_2)
    assert {p["part_id"] for p in subset["placements"]} == {"mcu_1", "mount_mcu_1"}


def test_subset_for_nodes_level_1_2_handles_multiple_subsections():
    mech = _subsection_mech()
    subset = mr._subset_for_nodes(mech, {"mcu_1", "sensor_1"}, level=mr.LEVEL_1_2)
    assert {p["part_id"] for p in subset["placements"]} == {
        "mcu_1", "mount_mcu_1", "sensor_1", "mount_sensor_1",
    }


def test_subset_for_nodes_level_1_2_singleton_contributes_only_itself():
    mech = _subsection_mech()
    subset = mr._subset_for_nodes(mech, {"battery_1"}, level=mr.LEVEL_1_2)
    assert {p["part_id"] for p in subset["placements"]} == {"battery_1"}


def test_subset_for_nodes_default_level_is_still_level_0_1():
    # Backward-compat: no `level` arg at all still behaves exactly like
    # the pre-G3e-4 Level 0->1-only implementation.
    mech = _mech("a", "b", "c")
    subset = mr._subset_for_nodes(mech, {"a", "c"})
    assert [p["part_id"] for p in subset["placements"]] == ["a", "c"]


# ---------------------------------------------------------------------------
# _subset_for_nodes -- Level 2->3 (G3f-2): node_ids are section_ids, and the
# subset must keep every placement belonging to EVERY subsection inside a
# kept section (not just its anchor) -- see this function's own docstring
# on why dropping a non-anchor subsection would silently re-derive the
# section as a singleton for validate_layout()'s own re-grouping.
# ---------------------------------------------------------------------------

def _section_mech():
    return {"placements": [
        {"part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5},
        {"part_id": "sensor_2", "x": 40, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5},
        {"part_id": "battery_1", "x": 100, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10},
    ]}


_SECTION_PARTS = [
    {"id": "sensor_1", "category": "sensor"},
    {"id": "sensor_2", "category": "sensor"},
    {"id": "battery_1", "category": "power"},
]


def test_subset_for_nodes_level_2_3_keeps_every_subsection_in_the_section():
    mech = _section_mech()
    subset = mr._subset_for_nodes(mech, {"Sensing"}, level=mr.LEVEL_2_3, parts=_SECTION_PARTS)
    assert {p["part_id"] for p in subset["placements"]} == {"sensor_1", "sensor_2"}


def test_subset_for_nodes_level_2_3_singleton_section_contributes_only_itself():
    mech = _section_mech()
    subset = mr._subset_for_nodes(mech, {"Power"}, level=mr.LEVEL_2_3, parts=_SECTION_PARTS)
    assert {p["part_id"] for p in subset["placements"]} == {"battery_1"}


def test_subset_for_nodes_level_2_3_handles_multiple_sections():
    mech = _section_mech()
    subset = mr._subset_for_nodes(mech, {"Sensing", "Power"}, level=mr.LEVEL_2_3, parts=_SECTION_PARTS)
    assert {p["part_id"] for p in subset["placements"]} == {"sensor_1", "sensor_2", "battery_1"}


def test_subset_for_nodes_level_2_3_without_parts_degrades_to_empty():
    # Same "not ready yet" degrade eo/mech_validator.py's own
    # _checkable_sections() uses when `parts` hasn't been wired through --
    # never a crash.
    mech = _section_mech()
    assert mr._subset_for_nodes(mech, {"Sensing"}, level=mr.LEVEL_2_3, parts=None) == {"placements": []}
    assert mr._subset_for_nodes(mech, {"Sensing"}, level=mr.LEVEL_2_3, parts=[]) == {"placements": []}


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


# ---------------------------------------------------------------------------
# run_level_1_2_repair (G3e-4) -- the Level 1->2 integration driver:
# run_repair_loop() + one final full validate_layout() call + persisting
# footprints onto mech["subsections"]. eo.mech_repair.validate_layout and
# agents.mech_subsection_pool.regenerate_subsection are both monkeypatched
# scripted stand-ins -- no real FreeCAD sandbox or LLM call in this file.
# ---------------------------------------------------------------------------

def _level_1_2_mech():
    return {"placements": [
        {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5,
         "primitives": [{"shape": "box"}]},
        {"part_id": "mount_mcu_1", "x": 0, "y": 10, "z": 0, "w": 30, "h": 5, "d": 5,
         "primitives": [{"shape": "box"}]},
    ]}


def test_run_level_1_2_repair_clean_run_persists_footprints(monkeypatch):
    # First call: run_repair_loop()'s own initial validate -- clean.
    # Second call: run_level_1_2_repair()'s own final full re-validate.
    _install(monkeypatch, [
        {"valid": True, "violations": [], "footprints": {"mcu_1": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 15, "d": 5}}},
        {"valid": True, "violations": [], "footprints": {"mcu_1": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 15, "d": 5}}},
    ])
    regen_calls = []
    monkeypatch.setattr(
        "agents.mech_subsection_pool.regenerate_subsection",
        lambda *a, **k: regen_calls.append((a, k)),
    )

    spec = {"mech": _level_1_2_mech()}
    result = mr.run_level_1_2_repair(spec, parts=[], session_id="s1")

    assert result["valid"] is True
    assert regen_calls == []  # nothing violated -- never asked to regenerate
    subsections = spec["mech"]["subsections"]
    assert subsections == [{
        "subsection_id": "mcu_1", "member_ids": ["mcu_1", "mount_mcu_1"],
        "footprint": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 15, "d": 5},
    }]


def test_run_level_1_2_repair_regenerates_on_collision_and_persists_footprint(monkeypatch):
    _install(monkeypatch, [
        # run_repair_loop()'s initial validate: collision
        {"valid": False, "violations": [{"node_id": "mcu_1", "issue": "part and mount collide"}]},
        # re-validate after regeneration: clean
        {"valid": True, "violations": []},
        # run_level_1_2_repair()'s own final full re-validate
        {"valid": True, "violations": [], "footprints": {"mcu_1": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 25, "d": 5}}},
    ])
    regen_calls = []
    monkeypatch.setattr(
        "agents.mech_subsection_pool.regenerate_subsection",
        lambda mech, node_id, violation, attempt, **k: regen_calls.append(node_id),
    )

    spec = {"mech": _level_1_2_mech()}
    result = mr.run_level_1_2_repair(spec, parts=[], session_id="s1")

    assert result["valid"] is True
    assert result["repaired"] == ["mcu_1"]
    assert regen_calls == ["mcu_1"]
    subsections = spec["mech"]["subsections"]
    assert subsections[0]["footprint"] == {"x": 0, "y": 0, "z": 0, "w": 30, "h": 25, "d": 5}


def test_run_level_1_2_repair_skips_final_call_on_validator_error(monkeypatch):
    _install(monkeypatch, [
        {"valid": True, "violations": [], "validator_error": "sandbox unreachable"},
    ])
    monkeypatch.setattr("agents.mech_subsection_pool.regenerate_subsection", lambda *a, **k: None)

    spec = {"mech": _level_1_2_mech()}
    result = mr.run_level_1_2_repair(spec, parts=[], session_id="s1")

    assert result["validator_error"] == "sandbox unreachable"
    # No second validate_layout call was scripted -- if run_level_1_2_repair
    # tried to make one, _ScriptedValidate would raise "called more times
    # than the test scripted" and this test would fail with that error.
    assert "subsections" not in spec["mech"]


def test_run_level_1_2_repair_flagged_subsection_still_gets_a_footprint(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [{"node_id": "mcu_1", "issue": "still colliding"}]},  # initial
        {"valid": False, "violations": [{"node_id": "mcu_1", "issue": "still colliding"}]},  # attempt 1 revalidate
        {"valid": False, "violations": [{"node_id": "mcu_1", "issue": "still colliding"}]},  # attempt 2 revalidate -> cap hit
        # final full re-validate after the repair loop settles
        {"valid": True, "violations": [], "footprints": {"mcu_1": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 12, "d": 5}}},
    ])
    attempts_seen = []
    monkeypatch.setattr(
        "agents.mech_subsection_pool.regenerate_subsection",
        lambda mech, node_id, violation, attempt, **k: attempts_seen.append(attempt),
    )

    spec = {"mech": _level_1_2_mech()}
    result = mr.run_level_1_2_repair(spec, parts=[], session_id="s1", max_retries=2)

    assert attempts_seen == [1, 2]  # both retries actually used, cap respected
    assert result["valid"] is False
    assert result["violations"][0]["node_id"] == "mcu_1"
    # Still gets a footprint from the final call even though it's flagged.
    subsections = spec["mech"]["subsections"]
    assert subsections[0]["footprint"] == {"x": 0, "y": 0, "z": 0, "w": 30, "h": 12, "d": 5}


# ---------------------------------------------------------------------------
# run_level_2_3_repair (G3f-2) -- the Level 2->3 integration driver, same
# shape as run_level_1_2_repair() one level up: run_repair_loop() + one
# final full validate_layout() call + persisting footprints onto
# mech["sections"]. eo.mech_repair.validate_layout and agents.
# mech_section_pool.regenerate_section are both monkeypatched scripted
# stand-ins -- no real FreeCAD sandbox or LLM call in this file.
# ---------------------------------------------------------------------------

def _level_2_3_mech():
    return {
        "placements": [
            {"part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5,
             "primitives": [{"shape": "box"}]},
            {"part_id": "sensor_2", "x": 40, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5,
             "primitives": [{"shape": "box"}]},
        ],
        "subsections": [
            {"subsection_id": "sensor_1", "member_ids": ["sensor_1"],
             "footprint": {"x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5}},
            {"subsection_id": "sensor_2", "member_ids": ["sensor_2"],
             "footprint": {"x": 40, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5}},
        ],
    }


_LEVEL_2_3_PARTS = [
    {"id": "sensor_1", "category": "sensor"},
    {"id": "sensor_2", "category": "sensor"},
]


def test_run_level_2_3_repair_clean_run_persists_footprints(monkeypatch):
    # First call: run_repair_loop()'s own initial validate -- clean.
    # Second call: run_level_2_3_repair()'s own final full re-validate.
    _install(monkeypatch, [
        {"valid": True, "violations": [], "footprints": {"Sensing": {"x": 0, "y": 0, "z": 0, "w": 55, "h": 10, "d": 5}}},
        {"valid": True, "violations": [], "footprints": {"Sensing": {"x": 0, "y": 0, "z": 0, "w": 55, "h": 10, "d": 5}}},
    ])
    regen_calls = []
    monkeypatch.setattr(
        "agents.mech_section_pool.regenerate_section",
        lambda *a, **k: regen_calls.append((a, k)),
    )

    spec = {"mech": _level_2_3_mech()}
    result = mr.run_level_2_3_repair(spec, parts=_LEVEL_2_3_PARTS, session_id="s1")

    assert result["valid"] is True
    assert regen_calls == []  # nothing violated -- never asked to regenerate
    sections = spec["mech"]["sections"]
    assert sections == [{
        "section_id": "Sensing", "subsection_ids": ["sensor_1", "sensor_2"],
        "footprint": {"x": 0, "y": 0, "z": 0, "w": 55, "h": 10, "d": 5},
    }]


def test_run_level_2_3_repair_regenerates_on_collision_and_persists_footprint(monkeypatch):
    _install(monkeypatch, [
        # run_repair_loop()'s initial validate: collision
        {"valid": False, "violations": [{"node_id": "Sensing", "issue": "subsections collide"}]},
        # re-validate after regeneration: clean
        {"valid": True, "violations": []},
        # run_level_2_3_repair()'s own final full re-validate
        {"valid": True, "violations": [], "footprints": {"Sensing": {"x": 0, "y": 0, "z": 0, "w": 65, "h": 10, "d": 5}}},
    ])
    regen_calls = []
    monkeypatch.setattr(
        "agents.mech_section_pool.regenerate_section",
        lambda mech, node_id, violation, attempt, parts, **k: regen_calls.append(node_id),
    )

    spec = {"mech": _level_2_3_mech()}
    result = mr.run_level_2_3_repair(spec, parts=_LEVEL_2_3_PARTS, session_id="s1")

    assert result["valid"] is True
    assert result["repaired"] == ["Sensing"]
    assert regen_calls == ["Sensing"]
    sections = spec["mech"]["sections"]
    assert sections[0]["footprint"] == {"x": 0, "y": 0, "z": 0, "w": 65, "h": 10, "d": 5}


def test_run_level_2_3_repair_skips_final_call_on_validator_error(monkeypatch):
    _install(monkeypatch, [
        {"valid": True, "violations": [], "validator_error": "sandbox unreachable"},
    ])
    monkeypatch.setattr("agents.mech_section_pool.regenerate_section", lambda *a, **k: None)

    spec = {"mech": _level_2_3_mech()}
    result = mr.run_level_2_3_repair(spec, parts=_LEVEL_2_3_PARTS, session_id="s1")

    assert result["validator_error"] == "sandbox unreachable"
    # No second validate_layout call was scripted -- if run_level_2_3_repair
    # tried to make one, _ScriptedValidate would raise "called more times
    # than the test scripted" and this test would fail with that error.
    assert "sections" not in spec["mech"]


def test_run_level_2_3_repair_flagged_section_still_gets_a_footprint(monkeypatch):
    _install(monkeypatch, [
        {"valid": False, "violations": [{"node_id": "Sensing", "issue": "still colliding"}]},  # initial
        {"valid": False, "violations": [{"node_id": "Sensing", "issue": "still colliding"}]},  # attempt 1 revalidate
        {"valid": False, "violations": [{"node_id": "Sensing", "issue": "still colliding"}]},  # attempt 2 revalidate -> cap hit
        # final full re-validate after the repair loop settles
        {"valid": True, "violations": [], "footprints": {"Sensing": {"x": 0, "y": 0, "z": 0, "w": 55, "h": 10, "d": 5}}},
    ])
    attempts_seen = []
    monkeypatch.setattr(
        "agents.mech_section_pool.regenerate_section",
        lambda mech, node_id, violation, attempt, parts, **k: attempts_seen.append(attempt),
    )

    spec = {"mech": _level_2_3_mech()}
    result = mr.run_level_2_3_repair(spec, parts=_LEVEL_2_3_PARTS, session_id="s1", max_retries=2)

    assert attempts_seen == [1, 2]  # both retries actually used, cap respected
    assert result["valid"] is False
    assert result["violations"][0]["node_id"] == "Sensing"
    # Still gets a footprint from the final call even though it's flagged.
    sections = spec["mech"]["sections"]
    assert sections[0]["footprint"] == {"x": 0, "y": 0, "z": 0, "w": 55, "h": 10, "d": 5}


def test_run_level_2_3_repair_passes_parts_through_every_validate_call(monkeypatch):
    # Both the initial/repair-round validate_layout calls (inside
    # run_repair_loop) AND the final full re-validate must all see the
    # same `parts` this function was called with -- Level 2->3's section
    # grouping can't be re-derived without it. See eo/mech_validator.py's
    # _checkable_sections() docstring.
    stub = _install(monkeypatch, [
        {"valid": True, "violations": [], "footprints": {}},
        {"valid": True, "violations": [], "footprints": {}},
    ])
    monkeypatch.setattr("agents.mech_section_pool.regenerate_section", lambda *a, **k: None)

    spec = {"mech": _level_2_3_mech()}
    mr.run_level_2_3_repair(spec, parts=_LEVEL_2_3_PARTS, session_id="s1")

    assert all(call["parts"] == _LEVEL_2_3_PARTS for call in stub.calls)
