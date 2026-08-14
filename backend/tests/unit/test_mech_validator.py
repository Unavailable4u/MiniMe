"""
tests/unit/test_mech_validator.py — G3c (Master Guide, "G3/G4.
Hierarchical parallel build + validate", validation): covers eo/
mech_validator.py --

  - _checkable_placements()'s Level 0->1 scope (only placements that
    already carry a non-empty `primitives` list)
  - _tolerance_for()'s confidence-aware buffer mapping, including the
    "typical" fallback for missing/unrecognized confidence
  - _build_payload()'s shape
  - validate_layout()'s level gate (LEVEL_0_1/LEVEL_1_2/LEVEL_2_3
    implemented so far)
  - validate_layout()'s no-op path when nothing is checkable yet (never
    even touches the sandbox)
  - validate_layout()'s persistent-session reuse across calls with the
    same session_id, and that FreeCAD setup only runs once
  - validate_layout()'s fail-open behavior (validator_error, not a
    crash) when the sandbox/FreeCAD pipeline itself blows up, and that
    the wedged session gets closed so the next call gets a fresh one
  - close_session()'s no-op-when-nothing-to-close safety
  - _checkable_subsections()/_build_subsection_payload()'s Level 1->2
    scope (G3e-3)
  - _checkable_sections()/_build_section_payload()'s Level 2->3 scope
    (G3f-2, this patch) -- category-based section grouping via `parts`,
    degrading to "nothing checkable" when `parts` is absent

Same "fake out the SDK object itself, no real E2B/FreeCAD network calls"
approach tests/integration/test_sandbox_tester.py already uses for
agents/sandbox_tester.py -- this module's actual geometry checking runs
INSIDE the sandbox as a separate FreeCAD script this test suite can't
execute without a real FreeCAD install, so these tests cover this
module's OWN orchestration logic (batching, session lifecycle, payload
shape, fail-open behavior) rather than re-deriving FreeCAD's geometry
kernel results.
"""
import json

import pytest

import eo.mech_validator as mv


@pytest.fixture(autouse=True)
def _reset_sessions():
    """eo.mech_validator's session cache is module-level global state
    (by design -- see its own docstring on why: one sandbox per run,
    reused across every validate_layout() call in that run). Reset it
    before/after every test so tests never leak a fake sandbox into
    each other."""
    mv._sessions.clear()
    yield
    mv._sessions.clear()


# ---------------------------------------------------------------------------
# _checkable_placements
# ---------------------------------------------------------------------------

def test_checkable_placements_keeps_only_nonempty_primitives():
    placements = [
        {"part_id": "a", "primitives": [{"shape": "box"}]},
        {"part_id": "b", "primitives": []},
        {"part_id": "c"},
        {"part_id": "d", "primitives": None},
        "not a dict",
        {"part_id": "e", "primitives": [{"shape": "cylinder"}]},
    ]
    checkable = mv._checkable_placements(placements)
    assert [p["part_id"] for p in checkable] == ["a", "e"]


def test_checkable_placements_handles_none_and_empty_list():
    assert mv._checkable_placements(None) == []
    assert mv._checkable_placements([]) == []


# ---------------------------------------------------------------------------
# _tolerance_for
# ---------------------------------------------------------------------------

def test_tolerance_for_verified_is_zero_margin():
    assert mv._tolerance_for("verified") == 0.0


def test_tolerance_for_typical_is_a_buffer():
    assert mv._tolerance_for("typical") > 0.0


def test_tolerance_for_missing_or_unknown_falls_back_to_typical_buffer():
    assert mv._tolerance_for(None) == mv._tolerance_for("typical")
    assert mv._tolerance_for("made_up_value") == mv._tolerance_for("typical")


# ---------------------------------------------------------------------------
# _build_payload
# ---------------------------------------------------------------------------

def test_build_payload_shape():
    checkable = [{
        "part_id": "motor_1", "w": 28, "h": 19, "d": 19,
        "dimension_confidence": "verified",
        "primitives": [{"shape": "cylinder"}],
    }]
    payload = mv._build_payload(checkable)
    assert payload == {
        "parts": [{
            "part_id": "motor_1", "w": 28, "h": 19, "d": 19,
            "tolerance_mm": 0.0,
            "primitives": [{"shape": "cylinder"}],
        }],
    }


def test_build_payload_defaults_missing_dims_to_zero():
    checkable = [{"part_id": "x", "primitives": [{"shape": "box"}]}]
    payload = mv._build_payload(checkable)
    part = payload["parts"][0]
    assert part["w"] == 0 and part["h"] == 0 and part["d"] == 0
    assert part["tolerance_mm"] == mv._tolerance_for("typical")


# ---------------------------------------------------------------------------
# validate_layout -- level gate + no-op path
# ---------------------------------------------------------------------------

def test_validate_layout_rejects_unimplemented_level():
    # "1->2" (LEVEL_1_2), "2->3" (LEVEL_2_3), and "3->4" (LEVEL_3_4) are
    # now implemented as of G3e-3/G3f-2/G3g -- Level 3->4 is also the
    # last level in the tree (no "4->5" will ever land), so this made-up
    # string is guaranteed to stay genuinely unimplemented rather than
    # needing to move again.
    with pytest.raises(NotImplementedError):
        mv.validate_layout({"placements": []}, "4->5")


def test_validate_layout_noop_when_nothing_checkable(monkeypatch):
    calls = []
    monkeypatch.setattr(mv.Sandbox, "create", staticmethod(lambda **kw: calls.append(kw) or None))
    result = mv.validate_layout({"placements": [{"part_id": "a"}]}, mv.LEVEL_0_1)
    assert result == {"valid": True, "violations": []}
    assert calls == []  # never touched the sandbox at all


# ---------------------------------------------------------------------------
# Fake Sandbox -- same "stand in for the SDK object itself" approach
# tests/integration/test_sandbox_tester.py already uses.
# ---------------------------------------------------------------------------

class _FakeCommandResult:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.stderr = ""
        self.exit_code = 0
        self.error = None


class _FakeFiles:
    def __init__(self):
        self.written = {}

    def write(self, path, data):
        self.written[path] = data

    def read(self, path):
        return self.written[path]


class _FakeSandbox:
    """Records every command it's asked to run and pre-seeds the output
    file with `output_json` as though a real freecadcmd run had just
    written it -- this test suite isn't re-testing FreeCAD's geometry
    kernel, just this module's own orchestration around it."""

    def __init__(self, output_json='{"violations": []}'):
        self.files = _FakeFiles()
        self.commands_run = []
        self._output_json = output_json
        self.killed = False

    def commands_run_fn(self, cmd, timeout=None):
        self.commands_run.append(cmd)
        if "command -v" in cmd:
            return _FakeCommandResult(stdout="/usr/bin/freecadcmd\n")
        if "freecadcmd" in cmd:
            self.files.write(mv._OUTPUT_PATH, self._output_json)
        return _FakeCommandResult()

    @property
    def commands(self):
        class _Commands:
            def run(_self, cmd, timeout=None):
                return self.commands_run_fn(cmd, timeout)
        return _Commands()

    def kill(self):
        self.killed = True


@pytest.fixture
def fake_sandbox_factory(monkeypatch):
    """Patches eo.mech_validator.Sandbox.create to hand out fake
    sandboxes in order, one per call, so a test can assert exactly how
    many real sandboxes got created across several validate_layout()
    calls (persistent-session reuse)."""
    instances = []

    def _factory(output_json='{"violations": []}'):
        def _create(**kwargs):
            sbx = _FakeSandbox(output_json=output_json)
            instances.append(sbx)
            return sbx
        monkeypatch.setattr(mv.Sandbox, "create", staticmethod(_create))
        return instances

    return _factory


_ONE_PLACEMENT = {
    "part_id": "motor_1", "w": 28, "h": 19, "d": 19,
    "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 28, "h": 19, "d": 19},
                     "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "cylinder", "color_role": "primary"}],
}


def test_validate_layout_clean_run_reports_valid(fake_sandbox_factory):
    fake_sandbox_factory()
    result = mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1)
    assert result == {"valid": True, "violations": []}


def test_validate_layout_reports_violations_from_sandbox_output(fake_sandbox_factory):
    fake_sandbox_factory(output_json=json.dumps({
        "violations": [{"node_id": "motor_1", "issue": "primitive(s) extend outside the part's own bounding box (~4.2 mm^3 outside)"}],
    }))
    result = mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1)
    assert result["valid"] is False
    assert result["violations"][0]["node_id"] == "motor_1"


def test_validate_layout_reuses_persistent_sandbox_across_calls(fake_sandbox_factory):
    instances = fake_sandbox_factory()
    mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1, session_id="run-1")
    mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1, session_id="run-1")
    assert len(instances) == 1  # only ONE real sandbox created across both calls

    probe_calls = [c for c in instances[0].commands_run if "command -v" in c]
    assert len(probe_calls) == 1  # FreeCAD readiness probed/installed only once, not per call


def test_validate_layout_different_sessions_get_separate_sandboxes(fake_sandbox_factory):
    instances = fake_sandbox_factory()
    mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1, session_id="run-1")
    mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1, session_id="run-2")
    assert len(instances) == 2


def test_close_session_kills_and_forgets_sandbox(fake_sandbox_factory):
    instances = fake_sandbox_factory()
    mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1, session_id="run-1")
    mv.close_session("run-1")
    assert instances[0].killed is True
    assert mv._session_key("run-1") not in mv._sessions

    # A fresh call after close gets a brand-new sandbox, not the killed one.
    mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1, session_id="run-1")
    assert len(instances) == 2


def test_close_session_is_a_noop_when_nothing_was_ever_created():
    mv.close_session("never-touched")  # must not raise


def test_validate_layout_fails_open_and_closes_wedged_session(monkeypatch):
    def _broken_create(**kwargs):
        class _Broken:
            @property
            def commands(self):
                raise RuntimeError("sandbox unreachable")

            def kill(self):
                pass
        return _Broken()

    monkeypatch.setattr(mv.Sandbox, "create", staticmethod(_broken_create))
    result = mv.validate_layout({"placements": [_ONE_PLACEMENT]}, mv.LEVEL_0_1, session_id="run-x")

    assert result["valid"] is True
    assert result["violations"] == []
    assert "validator_error" in result
    assert mv._session_key("run-x") not in mv._sessions  # wedged session was closed, not left cached


# ---------------------------------------------------------------------------
# Level 1->2 (G3e-3) -- _checkable_subsections / _build_subsection_payload
# ---------------------------------------------------------------------------

_MCU_MEMBER = {
    "part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 30, "h": 20, "d": 5,
    "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 30, "h": 20, "d": 5},
                     "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box"}],
}
_MOUNT_MEMBER = {
    "part_id": "mount_mcu_1", "x": 0, "y": 20, "z": 0, "w": 30, "h": 5, "d": 5,
    "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 30, "h": 5, "d": 5},
                     "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box"}],
}


def test_checkable_subsections_keeps_only_composed_members():
    mech = {"placements": [
        _MCU_MEMBER, _MOUNT_MEMBER,
        {"part_id": "battery_1", "x": 50, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10},  # no primitives
    ]}
    checkable = mv._checkable_subsections(mech)
    assert len(checkable) == 1
    assert checkable[0]["subsection_id"] == "mcu_1"
    assert [m["part_id"] for m in checkable[0]["members"]] == ["mcu_1", "mount_mcu_1"]


def test_checkable_subsections_skips_subsection_with_no_composed_members():
    mech = {"placements": [
        {"part_id": "battery_1", "x": 50, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10},
    ]}
    assert mv._checkable_subsections(mech) == []


def test_checkable_subsections_keeps_partially_composed_subsection():
    # Only the anchor part is composed yet -- mount hasn't gotten
    # primitives from G3a/G3b/G3e-2 yet. Still checkable (contributes a
    # partial footprint), per _checkable_subsections()'s own docstring.
    uncomposed_mount = {**_MOUNT_MEMBER, "primitives": []}
    mech = {"placements": [_MCU_MEMBER, uncomposed_mount]}
    checkable = mv._checkable_subsections(mech)
    assert len(checkable) == 1
    assert [m["part_id"] for m in checkable[0]["members"]] == ["mcu_1"]


def test_build_subsection_payload_shape():
    checkable = mv._checkable_subsections({"placements": [_MCU_MEMBER, _MOUNT_MEMBER]})
    payload = mv._build_subsection_payload(checkable)
    assert payload["level"] == mv.LEVEL_1_2
    assert len(payload["subsections"]) == 1
    sub = payload["subsections"][0]
    assert sub["subsection_id"] == "mcu_1"
    assert [m["part_id"] for m in sub["members"]] == ["mcu_1", "mount_mcu_1"]
    assert sub["members"][1]["x"] == 0 and sub["members"][1]["y"] == 20


# ---------------------------------------------------------------------------
# Level 1->2 (G3e-3) -- validate_layout() end-to-end via the fake sandbox
# ---------------------------------------------------------------------------

def test_validate_layout_level_1_2_noop_when_nothing_checkable(monkeypatch):
    calls = []
    monkeypatch.setattr(mv.Sandbox, "create", staticmethod(lambda **kw: calls.append(kw) or None))
    result = mv.validate_layout({"placements": []}, mv.LEVEL_1_2)
    assert result == {"valid": True, "violations": [], "footprints": {}}
    assert calls == []


def test_validate_layout_level_1_2_clean_run_reports_footprints(fake_sandbox_factory):
    fake_sandbox_factory(output_json=json.dumps({
        "violations": [],
        "footprints": {"mcu_1": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 25, "d": 5}},
    }))
    result = mv.validate_layout({"placements": [_MCU_MEMBER, _MOUNT_MEMBER]}, mv.LEVEL_1_2)
    assert result["valid"] is True
    assert result["violations"] == []
    assert result["footprints"] == {"mcu_1": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 25, "d": 5}}


def test_validate_layout_level_1_2_reports_collision_violation(fake_sandbox_factory):
    fake_sandbox_factory(output_json=json.dumps({
        "violations": [{"node_id": "mcu_1", "issue": "part and mount collide (~12.5 mm^3 overlap)"}],
        "footprints": {"mcu_1": {"x": 0, "y": 0, "z": 0, "w": 30, "h": 25, "d": 5}},
    }))
    result = mv.validate_layout({"placements": [_MCU_MEMBER, _MOUNT_MEMBER]}, mv.LEVEL_1_2)
    assert result["valid"] is False
    assert result["violations"][0]["node_id"] == "mcu_1"
    assert result["footprints"]  # still reported even though it collided


def test_validate_layout_level_1_2_fails_open_with_empty_footprints(monkeypatch):
    def _broken_create(**kwargs):
        class _Broken:
            @property
            def commands(self):
                raise RuntimeError("sandbox unreachable")

            def kill(self):
                pass
        return _Broken()

    monkeypatch.setattr(mv.Sandbox, "create", staticmethod(_broken_create))
    result = mv.validate_layout({"placements": [_MCU_MEMBER, _MOUNT_MEMBER]}, mv.LEVEL_1_2, session_id="run-y")

    assert result["valid"] is True
    assert result["violations"] == []
    assert result["footprints"] == {}
    assert "validator_error" in result


# ---------------------------------------------------------------------------
# _check_subsection / _build_primitive_shape (the code that actually runs
# INSIDE FreeCAD) -- exercised directly here rather than via the sandbox,
# same "unit-test the geometry kernel logic with a real FreeCAD install"
# gap tests/integration/test_sandbox_tester.py's own docstring notes for
# static_scan.py. Skipped automatically if FreeCAD isn't importable in
# this environment.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _check_subsection / _build_primitive_shape (the code that actually runs
# INSIDE FreeCAD) -- exercised directly here rather than via the sandbox,
# same "unit-test the geometry kernel logic with a real FreeCAD install"
# gap tests/integration/test_sandbox_tester.py's own docstring notes for
# static_scan.py. Skipped automatically if FreeCAD isn't importable in
# this environment -- guarded per-test (not a module-level importorskip)
# so a missing FreeCAD install only skips THESE tests, not this whole file.
# ---------------------------------------------------------------------------

try:
    import FreeCAD as _freecad_probe  # noqa: F401
    _HAS_FREECAD = True
except ImportError:
    _HAS_FREECAD = False

_needs_freecad = pytest.mark.skipif(not _HAS_FREECAD, reason="real FreeCAD install required for geometry-kernel tests")


def _run_freecad_script_namespace():
    """Executes _FREECAD_SCRIPT's function DEFINITIONS (not its
    top-level main() call, which reads argv/files) in a throwaway
    namespace, so a test can call _check_subsection()/_build_primitive_shape()
    directly without shelling out to freecadcmd."""
    ns = {}
    # Strip the trailing `main()` call (the last non-blank line) so this
    # only defines functions, matching how eo/mech_validator.py's own
    # _run_batch() would otherwise only invoke this inside a sandbox.
    body = mv._FREECAD_SCRIPT.rsplit("main()", 1)[0]
    exec(compile(body, "<freecad_script>", "exec"), ns)
    return ns


@_needs_freecad
def test_check_subsection_no_collision_reports_zero_and_footprint():
    ns = _run_freecad_script_namespace()
    subsection = {
        "members": [
            {"x": 0, "y": 0, "z": 0, "primitives": [_MCU_MEMBER["primitives"][0]]},
            {"x": 0, "y": 20, "z": 0, "primitives": [_MOUNT_MEMBER["primitives"][0]]},
        ],
    }
    collision_mm3, footprint = ns["_check_subsection"](subsection)
    assert collision_mm3 == 0.0
    assert footprint == {"x": 0.0, "y": 0.0, "z": 0.0, "w": 30.0, "h": 25.0, "d": 5.0}


@_needs_freecad
def test_check_subsection_overlapping_members_reports_collision():
    ns = _run_freecad_script_namespace()
    subsection = {
        "members": [
            {"x": 0, "y": 0, "z": 0, "primitives": [_MCU_MEMBER["primitives"][0]]},
            # Mount overlaps the part instead of sitting flush below it.
            {"x": 0, "y": 10, "z": 0, "primitives": [_MOUNT_MEMBER["primitives"][0]]},
        ],
    }
    collision_mm3, footprint = ns["_check_subsection"](subsection)
    assert collision_mm3 > 0.0


@_needs_freecad
def test_check_subsection_singleton_has_no_collision_check():
    ns = _run_freecad_script_namespace()
    subsection = {"members": [{"x": 0, "y": 0, "z": 0, "primitives": [_MCU_MEMBER["primitives"][0]]}]}
    collision_mm3, footprint = ns["_check_subsection"](subsection)
    assert collision_mm3 == 0.0
    assert footprint == {"x": 0.0, "y": 0.0, "z": 0.0, "w": 30.0, "h": 20.0, "d": 5.0}


# ---------------------------------------------------------------------------
# Level 2->3 (G3f-2) -- _checkable_sections / _build_section_payload
# ---------------------------------------------------------------------------

_SENSOR_1_MEMBER = {
    "part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5,
    "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 15, "h": 10, "d": 5},
                     "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box"}],
}
_SENSOR_2_MEMBER = {
    "part_id": "sensor_2", "x": 40, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5,
    "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 15, "h": 10, "d": 5},
                     "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box"}],
}
_BATTERY_MEMBER = {
    "part_id": "battery_1", "x": 100, "y": 0, "z": 0, "w": 20, "h": 10, "d": 10,
    "primitives": [{"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 20, "h": 10, "d": 10},
                     "rotation": {"x": 0, "y": 0, "z": 0}, "shape": "box"}],
}
_SECTION_PARTS = [
    {"id": "sensor_1", "category": "sensor"},
    {"id": "sensor_2", "category": "sensor"},
    {"id": "battery_1", "category": "power"},
]


def test_checkable_sections_groups_by_category():
    mech = {"placements": [_SENSOR_1_MEMBER, _SENSOR_2_MEMBER, _BATTERY_MEMBER]}
    checkable = mv._checkable_sections(mech, _SECTION_PARTS)
    by_id = {s["section_id"]: s for s in checkable}
    assert set(by_id) == {"Sensing", "Power"}
    assert [s["subsection_id"] for s in by_id["Sensing"]["subsections"]] == ["sensor_1", "sensor_2"]
    assert [s["subsection_id"] for s in by_id["Power"]["subsections"]] == ["battery_1"]


def test_checkable_sections_requires_parts():
    mech = {"placements": [_SENSOR_1_MEMBER, _SENSOR_2_MEMBER]}
    assert mv._checkable_sections(mech, None) == []
    assert mv._checkable_sections(mech, []) == []


def test_checkable_sections_drops_subsections_with_no_composed_members():
    uncomposed_sensor_2 = {**_SENSOR_2_MEMBER, "primitives": []}
    mech = {"placements": [_SENSOR_1_MEMBER, uncomposed_sensor_2, _BATTERY_MEMBER]}
    checkable = mv._checkable_sections(mech, _SECTION_PARTS)
    by_id = {s["section_id"]: s for s in checkable}
    # sensor_2 isn't composed yet -- Sensing still shows up (sensor_1 is),
    # but with just the one checkable subsection, not two.
    assert [s["subsection_id"] for s in by_id["Sensing"]["subsections"]] == ["sensor_1"]


def test_build_section_payload_shape():
    checkable = mv._checkable_sections(
        {"placements": [_SENSOR_1_MEMBER, _SENSOR_2_MEMBER]}, _SECTION_PARTS[:2],
    )
    payload = mv._build_section_payload(checkable)
    assert payload["level"] == mv.LEVEL_2_3
    assert len(payload["sections"]) == 1
    section = payload["sections"][0]
    assert section["section_id"] == "Sensing"
    assert [s["subsection_id"] for s in section["subsections"]] == ["sensor_1", "sensor_2"]
    assert section["subsections"][1]["members"][0]["x"] == 40


# ---------------------------------------------------------------------------
# Level 2->3 (G3f-2) -- validate_layout() end-to-end via the fake sandbox
# ---------------------------------------------------------------------------

def test_validate_layout_level_2_3_noop_when_nothing_checkable(monkeypatch):
    calls = []
    monkeypatch.setattr(mv.Sandbox, "create", staticmethod(lambda **kw: calls.append(kw) or None))
    result = mv.validate_layout({"placements": []}, mv.LEVEL_2_3, parts=_SECTION_PARTS)
    assert result == {"valid": True, "violations": [], "footprints": {}}
    assert calls == []


def test_validate_layout_level_2_3_noop_without_parts(monkeypatch):
    # Same no-op path, reached via the "parts wasn't wired through yet"
    # degrade rather than an empty mech -- see _checkable_sections()'s
    # own docstring.
    calls = []
    monkeypatch.setattr(mv.Sandbox, "create", staticmethod(lambda **kw: calls.append(kw) or None))
    result = mv.validate_layout({"placements": [_SENSOR_1_MEMBER, _SENSOR_2_MEMBER]}, mv.LEVEL_2_3)
    assert result == {"valid": True, "violations": [], "footprints": {}}
    assert calls == []


def test_validate_layout_level_2_3_clean_run_reports_footprints(fake_sandbox_factory):
    fake_sandbox_factory(output_json=json.dumps({
        "violations": [],
        "footprints": {"Sensing": {"x": 0, "y": 0, "z": 0, "w": 55, "h": 10, "d": 5}},
    }))
    result = mv.validate_layout(
        {"placements": [_SENSOR_1_MEMBER, _SENSOR_2_MEMBER]}, mv.LEVEL_2_3, parts=_SECTION_PARTS[:2],
    )
    assert result["valid"] is True
    assert result["violations"] == []
    assert result["footprints"] == {"Sensing": {"x": 0, "y": 0, "z": 0, "w": 55, "h": 10, "d": 5}}


def test_validate_layout_level_2_3_reports_collision_violation(fake_sandbox_factory):
    fake_sandbox_factory(output_json=json.dumps({
        "violations": [{"node_id": "Sensing", "issue": "subsections collide (~9.0 mm^3 overlap)"}],
        "footprints": {"Sensing": {"x": 0, "y": 0, "z": 0, "w": 55, "h": 10, "d": 5}},
    }))
    result = mv.validate_layout(
        {"placements": [_SENSOR_1_MEMBER, _SENSOR_2_MEMBER]}, mv.LEVEL_2_3, parts=_SECTION_PARTS[:2],
    )
    assert result["valid"] is False
    assert result["violations"][0]["node_id"] == "Sensing"
    assert result["footprints"]  # still reported even though it collided


def test_validate_layout_level_2_3_fails_open_with_empty_footprints(monkeypatch):
    def _broken_create(**kwargs):
        class _Broken:
            @property
            def commands(self):
                raise RuntimeError("sandbox unreachable")

            def kill(self):
                pass
        return _Broken()

    monkeypatch.setattr(mv.Sandbox, "create", staticmethod(_broken_create))
    result = mv.validate_layout(
        {"placements": [_SENSOR_1_MEMBER, _SENSOR_2_MEMBER]}, mv.LEVEL_2_3,
        parts=_SECTION_PARTS[:2], session_id="run-z",
    )

    assert result["valid"] is True
    assert result["violations"] == []
    assert result["footprints"] == {}
    assert "validator_error" in result


# ---------------------------------------------------------------------------
# _check_section (the code that actually runs INSIDE FreeCAD) -- exercised
# directly here rather than via the sandbox, same approach _check_subsection
# uses above. Skipped automatically if FreeCAD isn't importable.
# ---------------------------------------------------------------------------

@_needs_freecad
def test_check_section_no_collision_reports_zero_and_footprint():
    ns = _run_freecad_script_namespace()
    section = {
        "subsections": [
            {"subsection_id": "sensor_1", "members": [
                {"x": 0, "y": 0, "z": 0, "primitives": [_SENSOR_1_MEMBER["primitives"][0]]},
            ]},
            {"subsection_id": "sensor_2", "members": [
                {"x": 40, "y": 0, "z": 0, "primitives": [_SENSOR_2_MEMBER["primitives"][0]]},
            ]},
        ],
    }
    collision_mm3, footprint = ns["_check_section"](section)
    assert collision_mm3 == 0.0
    assert footprint == {"x": 0.0, "y": 0.0, "z": 0.0, "w": 55.0, "h": 10.0, "d": 5.0}


@_needs_freecad
def test_check_section_overlapping_subsections_reports_collision():
    ns = _run_freecad_script_namespace()
    section = {
        "subsections": [
            {"subsection_id": "sensor_1", "members": [
                {"x": 0, "y": 0, "z": 0, "primitives": [_SENSOR_1_MEMBER["primitives"][0]]},
            ]},
            # sensor_2 overlaps sensor_1 instead of sitting clear of it.
            {"subsection_id": "sensor_2", "members": [
                {"x": 5, "y": 0, "z": 0, "primitives": [_SENSOR_2_MEMBER["primitives"][0]]},
            ]},
        ],
    }
    collision_mm3, footprint = ns["_check_section"](section)
    assert collision_mm3 > 0.0


@_needs_freecad
def test_check_section_ignores_intra_subsection_overlap():
    # Two MEMBERS of the SAME subsection overlapping each other (already
    # Level 1->2's job) must not surface as a Level 2->3 collision -- only
    # cross-subsection pairs count here. See module docstring.
    ns = _run_freecad_script_namespace()
    section = {
        "subsections": [
            {"subsection_id": "mcu_1", "members": [
                {"x": 0, "y": 0, "z": 0, "primitives": [_MCU_MEMBER["primitives"][0]]},
                # Overlaps mcu_1 itself, but it's the SAME subsection.
                {"x": 0, "y": 10, "z": 0, "primitives": [_MOUNT_MEMBER["primitives"][0]]},
            ]},
        ],
    }
    collision_mm3, footprint = ns["_check_section"](section)
    assert collision_mm3 == 0.0  # single subsection -- nothing to cross-compare


@_needs_freecad
def test_check_section_singleton_has_no_collision_check():
    ns = _run_freecad_script_namespace()
    section = {
        "subsections": [
            {"subsection_id": "sensor_1", "members": [
                {"x": 0, "y": 0, "z": 0, "primitives": [_SENSOR_1_MEMBER["primitives"][0]]},
            ]},
        ],
    }
    collision_mm3, footprint = ns["_check_section"](section)
    assert collision_mm3 == 0.0
    assert footprint == {"x": 0.0, "y": 0.0, "z": 0.0, "w": 15.0, "h": 10.0, "d": 5.0}
