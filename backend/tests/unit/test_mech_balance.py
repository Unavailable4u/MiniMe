"""
tests/unit/test_mech_balance.py — Mech View standalone implementation
guide, Phase C, Patch C.6: covers eo/mech_balance.py (Patches C.1-C.3's
own consumer, C.3/C.4), eo/mech_validator.py's check_balance() (C.4),
and eo/mech_repair.py's repair_balance() (C.5).

  - compute_cog() (C.3): mass-weighted centroid across every placed
    part's own footprint center, using eo/mech_mass.py's curated
    MASS_TABLE; a part with no curated entry still contributes (via
    eo/mech_balance.py's own _DEFAULT_UNKNOWN_MASS_G), never vanishes.
  - compute_support_polygon() (C.4 geometry, landed in mech_balance.py
    per that module's own top docstring): convex hull of every
    ground-contact (wheel/leg/foot keyword) part's own footprint
    center.
  - check_balance() (C.4 validator gate): a balanced wheeled layout
    passes; an unbalanced one fails with a `cog_outside_support_polygon`
    violation; a `static`/`handheld`/`wearable`/`flying` archetype with
    the EXACT SAME unbalanced mass distribution never triggers the
    check at all (`skipped=True`) -- confirms the archetype gate
    itself, not just the underlying math.
  - repair_balance() (C.5): an unbalanced layout gets exactly one
    corrective reposition of its heaviest contributing part (the
    battery) toward the support polygon's own centroid, and is
    reported `repaired=True` once that attempt actually fixes it; a
    layout that's still unbalanced after that single attempt is
    surfaced as a flagged violation (`repaired=False`, `ok=False`)
    rather than retried further; a balanced layout, or a non-checked
    archetype, is a pure no-op (`attempted=False`), never mutating
    `mech["placements"]`.

No LLM, no FreeCAD, no sandbox -- every function under test here is a
pure/deterministic geometry or bookkeeping function (compute_cog(),
compute_support_polygon(), check_balance() are all explicitly
"pure scan, no mutation, no I/O, no FreeCAD" per their own docstrings;
repair_balance() only ever mutates `mech["placements"]` directly, no
external calls), so no mock_llm/fake_bus fixtures needed -- same
posture tests/unit/test_mech_swept_volume.py's own module docstring
already states for its own sibling Phase B module.
"""
import eo.mech_balance as mb
import eo.mech_repair as mr
import eo.mech_sections as msec
import eo.mech_subsections as msub
import eo.mech_validator as mv

# ---------------------------------------------------------------------------
# Shared fixtures -- a small wheeled-rover BOM/layout, reused (with small
# positional tweaks) across every test below so each test only has to state
# what's DIFFERENT about its own scenario.
# ---------------------------------------------------------------------------

def _wheeled_parts(extra=None):
    parts = [
        {"id": "battery_1", "generic_name": "lipo battery", "category": "power"},
        {"id": "mcu_1", "generic_name": "esp32 dev board", "category": "mcu"},
        {"id": "wheel_1", "generic_name": "wheel", "category": "actuator"},
        {"id": "wheel_2", "generic_name": "wheel", "category": "actuator"},
        {"id": "wheel_3", "generic_name": "wheel", "category": "actuator"},
        {"id": "wheel_4", "generic_name": "wheel", "category": "actuator"},
    ]
    return parts + (extra or [])


def _four_wheel_placements(extra=None):
    # A 90x90mm square footprint of four wheels -- (0,0), (90,0), (0,90),
    # (90,90) footprint centers once w/h are halved -- with an MCU near
    # the middle. `extra` supplies the caller's own battery placement(s).
    return [
        {"part_id": "mcu_1", "x": 40, "y": 40, "z": 0, "w": 10, "h": 10, "d": 5},
        {"part_id": "wheel_1", "x": 0, "y": 0, "z": 0, "w": 5, "h": 5, "d": 20},
        {"part_id": "wheel_2", "x": 90, "y": 0, "z": 0, "w": 5, "h": 5, "d": 20},
        {"part_id": "wheel_3", "x": 0, "y": 90, "z": 0, "w": 5, "h": 5, "d": 20},
        {"part_id": "wheel_4", "x": 90, "y": 90, "z": 0, "w": 5, "h": 5, "d": 20},
    ] + (extra or [])


def _grouped_mech(mobility_type, placements, parts, enclosure_mode="partial"):
    mech = {
        "archetype": {"enclosure_mode": enclosure_mode, "mobility_type": mobility_type},
        "placements": placements,
    }
    msub.apply_subsection_grouping(mech)
    msec.apply_section_grouping(mech, parts)
    return mech


BALANCED_BATTERY = {"part_id": "battery_1", "x": 40, "y": 40, "z": 0, "w": 10, "h": 10, "d": 10}
UNBALANCED_BATTERY = {"part_id": "battery_1", "x": -80, "y": 40, "z": 0, "w": 10, "h": 10, "d": 10}


# ---------------------------------------------------------------------------
# compute_cog() (Patch C.3)
# ---------------------------------------------------------------------------

def test_compute_cog_is_mass_weighted_toward_the_heavier_part():
    parts = _wheeled_parts()
    placements = _four_wheel_placements([BALANCED_BATTERY])
    mech = _grouped_mech("wheeled", placements, parts)

    cog = mb.compute_cog(mech, parts)

    # Battery (60g, curated) dominates the four 8g wheels + one 9g MCU
    # (total 60 + 4*8 + 9 = 101g); with everything at/near (40-45, 40-45)
    # the centroid should land close to that same cluster, not pulled out
    # toward the far wheel corners.
    assert cog["total_mass_g"] == 101.0
    assert 20 < cog["x"] < 60
    assert 20 < cog["y"] < 60


def test_compute_cog_unlisted_part_still_contributes_default_mass():
    parts = [{"id": "mystery_1", "generic_name": "unobtainium widget", "category": "MISC"}]
    placements = [{"part_id": "mystery_1", "x": 10, "y": 10, "z": 0, "w": 10, "h": 10, "d": 10}]
    mech = _grouped_mech("static", placements, parts, enclosure_mode="full")

    cog = mb.compute_cog(mech, parts)

    # Unlisted part still contributes eo/mech_balance.py's own
    # _DEFAULT_UNKNOWN_MASS_G (5.0g) rather than vanishing from the centroid.
    assert cog["total_mass_g"] == 5.0
    assert cog["x"] == 15.0 and cog["y"] == 15.0


def test_compute_cog_no_sections_is_zeroed_no_op():
    assert mb.compute_cog({}, []) == {"x": 0.0, "y": 0.0, "z": 0.0, "total_mass_g": 0.0}


# ---------------------------------------------------------------------------
# compute_support_polygon() (Patch C.4 geometry)
# ---------------------------------------------------------------------------

def test_support_polygon_hull_covers_only_ground_contact_parts():
    parts = _wheeled_parts()
    placements = _four_wheel_placements([BALANCED_BATTERY])
    mech = _grouped_mech("wheeled", placements, parts)

    hull = mb.compute_support_polygon(mech, parts)
    hull_points = {(p["x"], p["y"]) for p in hull}

    # Four wheel footprint centers (2.5, 2.5), (92.5, 2.5), (2.5, 92.5),
    # (92.5, 92.5) -- exactly the square's four corners, no more, no less.
    assert hull_points == {(2.5, 2.5), (92.5, 2.5), (2.5, 92.5), (92.5, 92.5)}
    # The battery and MCU are not ground-contact parts -- never in the hull.
    assert len(hull) == 4


def test_support_polygon_no_ground_contact_parts_is_empty():
    parts = [{"id": "mcu_1", "generic_name": "esp32 dev board", "category": "mcu"}]
    placements = [{"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 10, "h": 10, "d": 5}]
    mech = _grouped_mech("wheeled", placements, parts)

    assert mb.compute_support_polygon(mech, parts) == []


# ---------------------------------------------------------------------------
# check_balance() (Patch C.4 validator gate)
# ---------------------------------------------------------------------------

def test_check_balance_balanced_layout_passes():
    parts = _wheeled_parts()
    placements = _four_wheel_placements([BALANCED_BATTERY])
    mech = _grouped_mech("wheeled", placements, parts)

    result = mv.check_balance(mech, parts)

    assert result == {
        "ok": True, "skipped": False, "violations": [],
        "cog": result["cog"], "support_polygon": result["support_polygon"],
    }
    assert result["cog"] is not None
    assert len(result["support_polygon"]) == 4


def test_check_balance_unbalanced_layout_fails_with_reason():
    parts = _wheeled_parts()
    placements = _four_wheel_placements([UNBALANCED_BATTERY])
    mech = _grouped_mech("wheeled", placements, parts)

    result = mv.check_balance(mech, parts)

    assert result["ok"] is False
    assert result["skipped"] is False
    assert len(result["violations"]) == 1
    violation = result["violations"][0]
    assert violation["reason"] == "cog_outside_support_polygon"
    assert violation["clearance_mm"] < 0
    assert violation["required_margin_mm"] == mv.BALANCE_MARGIN_MM


def test_check_balance_skips_non_wheeled_legged_archetypes():
    parts = _wheeled_parts()
    # Identical unbalanced mass distribution as the failing test above.
    placements = _four_wheel_placements([UNBALANCED_BATTERY])

    for mobility_type in ("static", "handheld", "wearable", "flying"):
        mech = _grouped_mech(mobility_type, placements, parts, enclosure_mode="full")
        result = mv.check_balance(mech, parts)
        assert result == {
            "ok": True, "skipped": True, "violations": [],
            "cog": None, "support_polygon": [],
        }, f"mobility_type={mobility_type} should skip the balance check entirely"


def test_check_balance_insufficient_ground_contact_points():
    # Only 2 wheels -- fewer than 3 hull points, no real support base.
    parts = _wheeled_parts()
    placements = [
        {"part_id": "wheel_1", "x": 0, "y": 0, "z": 0, "w": 5, "h": 5, "d": 20},
        {"part_id": "wheel_2", "x": 90, "y": 0, "z": 0, "w": 5, "h": 5, "d": 20},
        BALANCED_BATTERY,
    ]
    mech = _grouped_mech("wheeled", placements, parts)

    result = mv.check_balance(mech, parts)

    assert result["ok"] is False
    assert result["violations"] == [{"reason": "insufficient_ground_contact_points"}]


# ---------------------------------------------------------------------------
# repair_balance() (Patch C.5)
# ---------------------------------------------------------------------------

def test_repair_balance_balanced_layout_is_a_no_op():
    parts = _wheeled_parts()
    placements = _four_wheel_placements([dict(BALANCED_BATTERY)])
    mech = _grouped_mech("wheeled", placements, parts)
    battery_before = dict(next(p for p in mech["placements"] if p["part_id"] == "battery_1"))

    result = mr.repair_balance(mech, parts)

    assert result["ok"] is True
    assert result["skipped"] is False
    assert result["attempted"] is False
    assert result["repaired"] is False
    battery_after = next(p for p in mech["placements"] if p["part_id"] == "battery_1")
    assert battery_after == battery_before  # untouched -- nothing to repair


def test_repair_balance_non_checked_archetype_is_a_no_op():
    parts = _wheeled_parts()
    # Same unbalanced distribution that fails check_balance() under
    # "wheeled" -- proves the skip, not just a coincidentally-passing layout.
    placements = _four_wheel_placements([dict(UNBALANCED_BATTERY)])
    mech = _grouped_mech("static", placements, parts, enclosure_mode="full")
    battery_before = dict(next(p for p in mech["placements"] if p["part_id"] == "battery_1"))

    result = mr.repair_balance(mech, parts)

    assert result == {
        "ok": True, "skipped": True, "attempted": False, "repaired": False,
        "violations": [], "cog": None, "support_polygon": [],
    }
    battery_after = next(p for p in mech["placements"] if p["part_id"] == "battery_1")
    assert battery_after == battery_before  # untouched


def test_repair_balance_unbalanced_layout_gets_one_fix_and_is_repaired():
    parts = _wheeled_parts()
    placements = _four_wheel_placements([dict(UNBALANCED_BATTERY)])
    mech = _grouped_mech("wheeled", placements, parts)

    before = mv.check_balance(mech, parts)
    assert before["ok"] is False  # sanity: genuinely unbalanced going in

    result = mr.repair_balance(mech, parts)

    assert result["attempted"] is True
    assert result["repaired"] is True
    assert result["ok"] is True
    assert result["violations"] == []

    # The heaviest part (the battery) actually moved off its original
    # far-off-to-one-side position, toward the support polygon.
    battery_after = next(p for p in mech["placements"] if p["part_id"] == "battery_1")
    assert battery_after["x"] != UNBALANCED_BATTERY["x"]

    # Re-checking from scratch confirms the mutated layout is now genuinely
    # balanced, not just self-reported as such.
    after = mv.check_balance(mech, parts)
    assert after["ok"] is True


def test_repair_balance_still_unbalanced_after_one_attempt_is_flagged_not_looped():
    # Two batteries on the SAME far-off extreme -- repositioning only the
    # single heaviest one can't fully correct the CoG in one attempt.
    parts = _wheeled_parts([
        {"id": "battery_2", "generic_name": "lipo battery", "category": "power"},
    ])
    far_battery = {"part_id": "battery_1", "x": -300, "y": 40, "z": 0, "w": 10, "h": 10, "d": 10}
    far_battery_2 = {"part_id": "battery_2", "x": -300, "y": 40, "z": 0, "w": 10, "h": 10, "d": 10}
    placements = _four_wheel_placements([far_battery, far_battery_2])
    mech = _grouped_mech("wheeled", placements, parts)

    result = mr.repair_balance(mech, parts)

    assert result["attempted"] is True
    assert result["repaired"] is False
    assert result["ok"] is False
    assert len(result["violations"]) == 1
    assert result["violations"][0]["reason"] == "cog_outside_support_polygon"

    # Confirms this is a single-attempt cap, not an unbounded retry loop:
    # calling check_balance() again externally still reports the same
    # still-failing state repair_balance() itself returned -- it didn't
    # keep retrying internally past the one attempt.
    after = mv.check_balance(mech, parts)
    assert after["ok"] is False
