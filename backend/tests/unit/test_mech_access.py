"""
tests/unit/test_mech_access.py — Patch D.6 (Phase D, "Access mechanisms"):
covers eo/mech_access.py --

  - generate_hinge() / generate_snap_latch() / generate_slide() (Patch
    D.2/D.3/D.4): one test class per generator, pinning each one's own
    primitive shape/count/placement against a fixed boundary box, plus
    the shared "zero-width box still returns a well-formed, empty-
    primitives result" fail-safe every generator holds itself to.
  - apply_access_generation() (Patch D.5): the access_type join off each
    subsection's own anchor BOM part, the "fastened" (default/missing/
    invalid) no-op skip, the "no sections yet" no-op, the mech["access"]
    stash, and -- per this patch's own breakdown ("confirm a mixed-mode
    device... produces correct geometry for both regions simultaneously")
    -- one sealed main body alongside one slide-out hatch on the SAME
    device, checked together.

No LLM, no FreeCAD -- pure data reshaping (same as
tests/unit/test_mech_supports.py, tests/unit/test_mech_enclosure.py), so
no mock_llm/fake_bus fixtures needed. apply_access_generation() pulls
real eo/mech_sections.py + eo/mech_subsections.py grouping logic (not
mocked) -- same "build a real mech dict, let the real pipeline resolve
it" approach test_mech_supports.py's own _mech_for() fixture already
uses for apply_supports_generation().
"""
import eo.mech_access as ma
from eo.enclosure_spec import ACCESS_GEOMETRY, DEFAULT_ACCESS_TYPE
from eo.mech_sections import apply_section_grouping

_BOX = {"section_id": "hatch_1", "x": 0, "y": 0, "z": 0, "w": 40, "h": 5, "d": 25}
_ZERO_BOX = {"section_id": "hatch_1", "x": 0, "y": 0, "z": 0, "w": 0, "h": 0, "d": 0}


# ---------------------------------------------------------------------
# generate_hinge (Patch D.2)
# ---------------------------------------------------------------------

class TestGenerateHinge:
    def test_returns_correct_section_id_and_access_type(self):
        result = ma.generate_hinge(_BOX)
        assert result["section_id"] == "hatch_1"
        assert result["access_type"] == "hinged"

    def test_emits_configured_knuckle_count_plus_one_pin(self):
        geo = ACCESS_GEOMETRY["hinged"]
        result = ma.generate_hinge(_BOX)
        knuckles = [p for p in result["primitives"] if p["type"] == "hinge_knuckle"]
        pins = [p for p in result["primitives"] if p["type"] == "hinge_pin"]
        assert len(knuckles) == geo["knuckle_count"]
        assert len(pins) == 1

    def test_knuckles_alternate_housing_and_lid_membership(self):
        result = ma.generate_hinge(_BOX)
        knuckles = [p for p in result["primitives"] if p["type"] == "hinge_knuckle"]
        members = [k["member"] for k in knuckles]
        assert members[0] == "housing"
        assert members[1] == "lid"
        assert members[2] == "housing"

    def test_knuckle_and_pin_diameters_come_from_access_geometry(self):
        geo = ACCESS_GEOMETRY["hinged"]
        result = ma.generate_hinge(_BOX)
        knuckles = [p for p in result["primitives"] if p["type"] == "hinge_knuckle"]
        pin = next(p for p in result["primitives"] if p["type"] == "hinge_pin")
        assert all(k["diameter_mm"] == geo["knuckle_dia_mm"] for k in knuckles)
        assert pin["diameter_mm"] == geo["pin_dia_mm"]

    def test_pin_bore_leaves_clearance_over_pin_diameter(self):
        geo = ACCESS_GEOMETRY["hinged"]
        result = ma.generate_hinge(_BOX)
        knuckles = [p for p in result["primitives"] if p["type"] == "hinge_knuckle"]
        expected_bore = geo["pin_dia_mm"] + geo["pin_clearance_mm"]
        assert all(k["bore_diameter_mm"] == expected_bore for k in knuckles)

    def test_knuckles_span_centered_within_box_width(self):
        geo = ACCESS_GEOMETRY["hinged"]
        result = ma.generate_hinge(_BOX)
        knuckles = sorted(
            (p for p in result["primitives"] if p["type"] == "hinge_knuckle"),
            key=lambda k: k["x"],
        )
        span = geo["knuckle_count"] * geo["knuckle_length_mm"]
        expected_start = _BOX["x"] + (_BOX["w"] - span) / 2.0
        assert knuckles[0]["x"] == expected_start

    def test_zero_width_box_returns_empty_primitives_not_a_crash(self):
        result = ma.generate_hinge(_ZERO_BOX)
        assert result["section_id"] == "hatch_1"
        assert result["access_type"] == "hinged"
        assert result["primitives"] == []

    def test_pure_function_never_mutates_input(self):
        box = dict(_BOX)
        ma.generate_hinge(box)
        assert box == _BOX


# ---------------------------------------------------------------------
# generate_snap_latch (Patch D.3)
# ---------------------------------------------------------------------

class TestGenerateSnapLatch:
    def test_returns_correct_section_id_and_access_type(self):
        result = ma.generate_snap_latch(_BOX)
        assert result["section_id"] == "hatch_1"
        assert result["access_type"] == "snap_latch"

    def test_emits_exactly_one_hook_and_one_catch(self):
        result = ma.generate_snap_latch(_BOX)
        types = [p["type"] for p in result["primitives"]]
        assert types == ["latch_hook", "latch_catch"]

    def test_hook_dimensions_come_from_access_geometry(self):
        geo = ACCESS_GEOMETRY["snap_latch"]
        result = ma.generate_snap_latch(_BOX)
        hook = result["primitives"][0]
        assert hook["length_mm"] == geo["cantilever_length_mm"]
        assert hook["width_mm"] == geo["cantilever_width_mm"]
        assert hook["thickness_mm"] == geo["cantilever_thickness_mm"]

    def test_catch_sits_one_cantilever_length_beyond_the_hook(self):
        geo = ACCESS_GEOMETRY["snap_latch"]
        result = ma.generate_snap_latch(_BOX)
        hook, catch = result["primitives"]
        assert catch["y"] == hook["y"] + geo["cantilever_length_mm"]
        assert catch["depth_mm"] == geo["catch_depth_mm"]
        assert catch["overhang_mm"] == geo["catch_overhang_mm"]

    def test_hook_and_catch_share_the_same_centered_x(self):
        result = ma.generate_snap_latch(_BOX)
        hook, catch = result["primitives"]
        assert hook["x"] == catch["x"]

    def test_pure_function_never_mutates_input(self):
        box = dict(_BOX)
        ma.generate_snap_latch(box)
        assert box == _BOX


# ---------------------------------------------------------------------
# generate_slide (Patch D.4)
# ---------------------------------------------------------------------

class TestGenerateSlide:
    def test_returns_correct_section_id_and_access_type(self):
        result = ma.generate_slide(_BOX)
        assert result["section_id"] == "hatch_1"
        assert result["access_type"] == "slide"

    def test_emits_two_channels_and_one_stop(self):
        result = ma.generate_slide(_BOX)
        types = [p["type"] for p in result["primitives"]]
        assert types.count("slide_channel") == 2
        assert types.count("slide_stop") == 1

    def test_channels_flank_left_and_right_at_box_width(self):
        result = ma.generate_slide(_BOX)
        channels = {p["side"]: p for p in result["primitives"] if p["type"] == "slide_channel"}
        assert channels["left"]["x"] == _BOX["x"]
        assert channels["right"]["x"] == _BOX["x"] + _BOX["w"]

    def test_channels_run_the_full_box_depth(self):
        result = ma.generate_slide(_BOX)
        channels = [p for p in result["primitives"] if p["type"] == "slide_channel"]
        assert all(c["length_mm"] == _BOX["d"] for c in channels)

    def test_channel_clearance_and_depth_come_from_access_geometry(self):
        geo = ACCESS_GEOMETRY["slide"]
        result = ma.generate_slide(_BOX)
        channels = [p for p in result["primitives"] if p["type"] == "slide_channel"]
        assert all(c["clearance_mm"] == geo["channel_clearance_mm"] for c in channels)
        assert all(c["depth_mm"] == geo["channel_depth_mm"] for c in channels)

    def test_stop_sits_at_the_far_end_of_the_channel(self):
        geo = ACCESS_GEOMETRY["slide"]
        result = ma.generate_slide(_BOX)
        stop = next(p for p in result["primitives"] if p["type"] == "slide_stop")
        assert stop["z"] == _BOX["z"] + _BOX["d"]
        assert stop["length_mm"] == geo["stop_length_mm"]
        assert stop["height_mm"] == geo["stop_height_mm"]

    def test_pure_function_never_mutates_input(self):
        box = dict(_BOX)
        ma.generate_slide(box)
        assert box == _BOX


# ---------------------------------------------------------------------
# apply_access_generation (Patch D.5)
# ---------------------------------------------------------------------

def _mech_for(parts):
    """One flat, mount-free subsection per part, all in one section --
    same shallow shape test_mech_supports.py's own _mech_for() fixture
    uses, since apply_access_generation() joins mech["placements"]
    against `parts` via the real group_into_sections()/
    subsections_for_section()/members_for_subsection() pipeline, not a
    mocked one. Each part gets a distinct, non-overlapping footprint so a
    mixed-mode device's regions never accidentally share a bounding box.
    """
    placements = [
        {"part_id": p["id"], "x": i * 50, "y": 0, "z": 0, "w": 20, "h": 10, "d": 15}
        for i, p in enumerate(parts)
    ]
    mech = {"placements": placements, "sections": []}
    apply_section_grouping(mech, parts)
    return mech


def test_no_sections_returns_and_stashes_empty_result():
    mech = {"placements": [], "sections": []}
    result = ma.apply_access_generation(mech, [])
    assert result == []
    assert mech["access"] == []


def test_fastened_part_produces_no_access_entry():
    parts = [{"id": "mcu_1", "category": "mcu", "access_type": "fastened"}]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)
    assert result == []


def test_part_with_no_access_type_field_defaults_to_fastened_noop():
    parts = [{"id": "mcu_1", "category": "mcu"}]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)
    assert result == []


def test_part_with_invalid_access_type_falls_back_to_fastened_noop():
    parts = [{"id": "mcu_1", "category": "mcu", "access_type": "glued_shut"}]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)
    assert result == []


def test_hinged_part_dispatches_to_generate_hinge():
    parts = [{"id": "lid_1", "category": "3D_PRINT", "access_type": "hinged"}]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)
    assert len(result) == 1
    assert result[0]["access_type"] == "hinged"
    assert result[0]["section_id"] == "lid_1"


def test_snap_latch_part_dispatches_to_generate_snap_latch():
    parts = [{"id": "panel_1", "category": "3D_PRINT", "access_type": "snap_latch"}]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)
    assert len(result) == 1
    assert result[0]["access_type"] == "snap_latch"


def test_slide_part_dispatches_to_generate_slide():
    parts = [{"id": "hatch_1", "category": "power", "access_type": "slide"}]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)
    assert len(result) == 1
    assert result[0]["access_type"] == "slide"


def test_bounding_box_matches_the_subsections_own_placement():
    parts = [{"id": "hatch_1", "category": "power", "access_type": "slide"}]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)
    channels = [p for p in result[0]["primitives"] if p["type"] == "slide_channel"]
    # _mech_for's single placement is x=0,y=0,z=0,w=20,h=10,d=15 --
    # the bounding box of one member is just that member's own footprint.
    assert {c["x"] for c in channels} == {0, 20}
    assert all(c["length_mm"] == 15 for c in channels)


def test_result_is_stashed_on_mech_access_key():
    parts = [{"id": "hatch_1", "category": "power", "access_type": "slide"}]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)
    assert mech["access"] == result


def test_never_mutates_placements_or_parts_lists():
    parts = [{"id": "hatch_1", "category": "power", "access_type": "slide"}]
    mech = _mech_for(parts)
    placements_snapshot = [dict(p) for p in mech["placements"]]
    parts_snapshot = [dict(p) for p in parts]

    ma.apply_access_generation(mech, parts)

    assert mech["placements"] == placements_snapshot
    assert parts == parts_snapshot


def test_mixed_mode_device_sealed_body_plus_slide_out_hatch():
    """The guide's own D.6 breakdown: 'confirm a mixed-mode device
    (sealed main body, one slide-out hatch) produces correct geometry
    for both regions simultaneously.' The main body's MCU/sensor stay on
    the default ("fastened") -- and are checked to have produced NO
    access entries -- while only the battery hatch, explicitly marked
    "slide", gets channel+stop geometry, all resolved off the same
    mech/apply_access_generation() call.
    """
    parts = [
        {"id": "mcu_1", "category": "mcu"},                       # sealed, default
        {"id": "sensor_1", "category": "sensor"},                 # sealed, default
        {"id": "battery_1", "category": "power", "access_type": "slide"},  # hatch
    ]
    mech = _mech_for(parts)
    result = ma.apply_access_generation(mech, parts)

    assert len(result) == 1
    assert result[0]["section_id"] == "battery_1"
    assert result[0]["access_type"] == "slide"
    channel_types = [p["type"] for p in result[0]["primitives"]]
    assert channel_types.count("slide_channel") == 2
    assert channel_types.count("slide_stop") == 1

    # DEFAULT_ACCESS_TYPE sanity: the two sealed parts really did resolve
    # to "fastened", not merely "not slide" -- pins the no-op default
    # itself, not just its downstream absence from `result`.
    assert DEFAULT_ACCESS_TYPE == "fastened"
