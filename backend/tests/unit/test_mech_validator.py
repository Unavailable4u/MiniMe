"""
tests/unit/test_mech_validator.py — G3c (Master Guide, "G3/G4.
Hierarchical parallel build + validate", validation): covers eo/
mech_validator.py --

  - _checkable_placements()'s Level 0->1 scope (only placements that
    already carry a non-empty `primitives` list)
  - _tolerance_for()'s confidence-aware buffer mapping, including the
    "typical" fallback for missing/unrecognized confidence
  - _build_payload()'s shape
  - validate_layout()'s level gate (only LEVEL_0_1 implemented so far)
  - validate_layout()'s no-op path when nothing is checkable yet (never
    even touches the sandbox)
  - validate_layout()'s persistent-session reuse across calls with the
    same session_id, and that FreeCAD setup only runs once
  - validate_layout()'s fail-open behavior (validator_error, not a
    crash) when the sandbox/FreeCAD pipeline itself blows up, and that
    the wedged session gets closed so the next call gets a fresh one
  - close_session()'s no-op-when-nothing-to-close safety

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
    with pytest.raises(NotImplementedError):
        mv.validate_layout({"placements": []}, "1->2")


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
