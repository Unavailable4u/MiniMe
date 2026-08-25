"""
tests/unit/test_mech_dfm.py — Mech View standalone implementation
guide, Phase G, Patch G.4: covers eo/mech_dfm.py's check_bed_fit()
(G.2) and apply_bed_fit_split() (G.3).

  - check_bed_fit() (G.2): no `mech["housing"]` (or a malformed one) is
    a no-op pass, `dims_mm` None; a `full`-mode housing/lid pair sums
    `outer["d"] + lid["d"]` for its own z extent (the combined-stack
    posture the module's own docstring documents); a `partial`-mode
    baseplate (`outer` only, no `lid` key) uses `outer["d"]` alone; a
    dimension exactly equal to its own bed axis is NOT a violation
    (`>`, not `>=`).
  - apply_bed_fit_split() (G.3): an oversized housing gets split along
    its own longest axis into two equal halves that reconstruct the
    original footprint with no gap/overlap, plus a seam generated via
    eo/mech_access.py's own generate_hinge() (reused geometry, labeled
    `"fastened_split_seam"` / `"fastened": True` rather than
    `"hinged"`); `mech["housing_split"]` is stashed with that result.
    An in-bounds housing is unchanged -- no split applied, and
    `mech["housing_split"]` is explicitly `None`, not left unset.

No LLM, no FreeCAD -- both functions under test are pure/deterministic,
same posture every other check_*/apply_*_split-style module in this
tree already documents for itself.
"""
import eo.mech_dfm as md
from eo.enclosure_spec import PRINT_BED_MM


# ---------------------------------------------------------------------------
# check_bed_fit() (Patch G.2)
# ---------------------------------------------------------------------------

def test_no_housing_is_a_noop_pass():
    assert md.check_bed_fit({}) == {
        "ok": True, "dims_mm": None, "bed_mm": PRINT_BED_MM, "violations": [],
    }
    assert md.check_bed_fit({"housing": None}) == {
        "ok": True, "dims_mm": None, "bed_mm": PRINT_BED_MM, "violations": [],
    }
    assert md.check_bed_fit(None) == {
        "ok": True, "dims_mm": None, "bed_mm": PRINT_BED_MM, "violations": [],
    }


def test_malformed_housing_missing_outer_is_a_noop_pass():
    result = md.check_bed_fit({"housing": {"lid": {"d": 5}}})
    assert result["ok"] is True
    assert result["dims_mm"] is None
    assert result["violations"] == []


def test_in_bounds_full_mode_housing_passes():
    mech = {
        "housing": {
            "outer": {"w": 120, "h": 90, "d": 30},
            "inner": {"w": 110, "h": 80, "d": 27},
            "lid": {"d": 3},
        }
    }
    result = md.check_bed_fit(mech)
    assert result["ok"] is True
    assert result["dims_mm"] == {"x": 120, "y": 90, "z": 33}
    assert result["violations"] == []


def test_full_mode_sums_outer_and_lid_depth_for_z():
    # Oversized only once outer.d + lid.d is summed -- outer.d alone
    # (247) would fit under PRINT_BED_MM["z"] (250), but the combined
    # stack (247 + 10 = 257) does not.
    mech = {"housing": {"outer": {"w": 100, "h": 100, "d": 247}, "lid": {"d": 10}}}
    result = md.check_bed_fit(mech)
    assert result["ok"] is False
    assert result["dims_mm"]["z"] == 257
    violation = next(v for v in result["violations"] if v["axis"] == "z")
    assert violation["extent_mm"] == 257
    assert violation["bed_mm"] == 250
    assert violation["over_mm"] == 7


def test_partial_mode_baseplate_uses_outer_depth_alone_no_lid():
    mech = {"housing": {"outer": {"w": 150, "h": 150, "d": 20}}}
    result = md.check_bed_fit(mech)
    assert result["ok"] is True
    assert result["dims_mm"] == {"x": 150, "y": 150, "z": 20}


def test_exact_bed_match_is_not_a_violation():
    mech = {"housing": {"outer": {"w": 220, "h": 220, "d": 250}}}
    result = md.check_bed_fit(mech)
    assert result["ok"] is True
    assert result["violations"] == []


def test_oversized_on_multiple_axes_reports_all_violations():
    mech = {"housing": {"outer": {"w": 300, "h": 260, "d": 100}}}
    result = md.check_bed_fit(mech)
    assert result["ok"] is False
    axes_violated = {v["axis"] for v in result["violations"]}
    assert axes_violated == {"x", "y"}


def test_missing_dims_default_to_zero_never_raises():
    mech = {"housing": {"outer": {"w": 50}}}
    result = md.check_bed_fit(mech)
    assert result["ok"] is True
    assert result["dims_mm"] == {"x": 50, "y": 0, "z": 0}


# ---------------------------------------------------------------------------
# apply_bed_fit_split() (Patch G.3)
# ---------------------------------------------------------------------------

def test_in_bounds_housing_unchanged_no_split_applied():
    mech = {"housing": {"outer": {"x": 0, "y": 0, "z": 0, "w": 120, "h": 90, "d": 30}}}
    original_outer = dict(mech["housing"]["outer"])

    result = md.apply_bed_fit_split(mech)

    assert result is None
    assert mech["housing_split"] is None
    assert mech["housing"]["outer"] == original_outer


def test_no_housing_at_all_is_a_noop_split():
    mech = {}
    result = md.apply_bed_fit_split(mech)
    assert result is None
    assert mech["housing_split"] is None


def test_oversized_housing_split_along_longest_axis():
    # x=300 is the single largest extent -- must be the chosen split axis,
    # even though only x itself violates PRINT_BED_MM.
    outer = {"x": 0, "y": 0, "z": 0, "w": 300, "h": 90, "d": 30}
    mech = {"housing": {"outer": outer}}

    result = md.apply_bed_fit_split(mech)

    assert result["split"] is True
    assert result["axis"] == "x"
    assert mech["housing_split"] is result


def test_split_halves_reconstruct_original_footprint_no_gap_or_overlap():
    outer = {"x": 10, "y": 5, "z": 0, "w": 300, "h": 90, "d": 30}
    mech = {"housing": {"outer": outer}}

    result = md.apply_bed_fit_split(mech)
    half_a, half_b = result["halves"]

    assert half_a["w"] == 150
    assert half_b["w"] == 150
    assert half_a["x"] == outer["x"]
    assert half_b["x"] == outer["x"] + 150
    # Non-split axes/fields pass through unchanged on both halves.
    for half in (half_a, half_b):
        assert half["y"] == outer["y"]
        assert half["h"] == outer["h"]
        assert half["z"] == outer["z"]
        assert half["d"] == outer["d"]


def test_split_seam_reuses_generate_hinge_geometry_with_fastened_label():
    outer = {"x": 0, "y": 0, "z": 0, "w": 300, "h": 90, "d": 30}
    mech = {"housing": {"outer": outer}}

    result = md.apply_bed_fit_split(mech)
    seam = result["seam"]

    assert seam["section_id"] == "housing_split_seam"
    assert seam["access_type"] == "fastened_split_seam"
    assert seam["fastened"] is True
    # Same knuckle/pin primitive types generate_hinge() itself emits,
    # not a hand-rolled joint shape.
    prim_types = {p["type"] for p in seam["primitives"]}
    assert prim_types == {"hinge_knuckle", "hinge_pin"}
    assert len(seam["primitives"]) > 0


def test_split_seam_runs_along_the_larger_of_the_two_remaining_axes():
    # Split axis is x (300, the longest). Of the two remaining axes,
    # y (90) is larger than z (30), so the seam must run along y.
    outer = {"x": 0, "y": 0, "z": 0, "w": 300, "h": 90, "d": 30}
    mech = {"housing": {"outer": outer}}

    result = md.apply_bed_fit_split(mech)
    seam = result["seam"]

    # Knuckles/pin vary along the seam axis (y) at a fixed split-axis
    # position (x, the split midpoint) and fixed remaining axis (z).
    knuckles = [p for p in seam["primitives"] if p["type"] == "hinge_knuckle"]
    assert len({k["y"] for k in knuckles}) > 1
    assert len({k["x"] for k in knuckles}) == 1
    assert len({k["z"] for k in knuckles}) == 1


def test_split_seam_ties_broken_x_then_y_then_z():
    # z is the longest axis here (uniquely), so split_axis == "z".
    # Of the two remaining (x, y), both equal 50 -- tie broken to x.
    outer = {"x": 0, "y": 0, "z": 0, "w": 50, "h": 50, "d": 260}
    mech = {"housing": {"outer": outer}}

    result = md.apply_bed_fit_split(mech)

    assert result["axis"] == "z"
    knuckles = [p for p in result["seam"]["primitives"] if p["type"] == "hinge_knuckle"]
    assert len({k["x"] for k in knuckles}) > 1
    assert len({k["y"] for k in knuckles}) == 1


def test_split_does_not_mutate_mech_housing_or_enclosure_keys():
    mech = {
        "housing": {"outer": {"x": 0, "y": 0, "z": 0, "w": 300, "h": 90, "d": 30}},
        "enclosure": {"w": 300, "h": 90, "d": 30},
    }
    original_housing = {k: dict(v) if isinstance(v, dict) else v
                         for k, v in mech["housing"].items()}
    original_enclosure = dict(mech["enclosure"])

    md.apply_bed_fit_split(mech)

    assert mech["housing"] == original_housing
    assert mech["enclosure"] == original_enclosure
    assert "housing_split" in mech
