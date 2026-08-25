"""
eo/mech_dfm.py — Phase G, Patch G.2 of the Mech View standalone
implementation guide: the first landed check of build-plate fit
("No print-orientation / build-plate size awareness" -- Part 1's own
gap #7, "Nothing checks whether generated geometry actually fits a
real printer's bed, or would need splitting").

Same build-order reasoning every earlier "pure function first" patch
in this tree already established (eo/mech_enclosure.py's own
compute_housing_footprint() before apply_enclosure_generation(), eo/
mech_manufacturability.py's own check_standoff_wall_clearance() before
check_feature_collisions()): land the plain measure-and-report check
here, on its own, testable with a plain `mech` dict, before Patch G.3
wires an actual auto-split decision on top of a violation this module
reports.

Reads exactly one piece of Patch G.1 config from eo/enclosure_spec.py:
  - PRINT_BED_MM: the single global `{"x", "y", "z"}` build-volume
    constant every dimension below is compared against. See that
    constant's own docstring for why it is a flat global rather than
    per-project configurable.

Input: `mech["housing"]`, the same `None | {"outer": {...}} |
{"outer": {...}, "inner": {...}, "lid": {...}}` shape eo/
mech_enclosure.py's own apply_enclosure_generation() already stashes
there (Patch 1.3, extended by Patch A.5 for `partial`/`none` archetype
modes) -- this module never recomputes a footprint itself, only reads
the one already-computed structure, same "read two already-computed
structures, don't invent a third" posture eo/mech_manufacturability.py's
own module docstring already holds itself to.

Patch G.3 (below, this same module) extends this with the actual
auto-split decision -- gated on Phase D's `generate_hinge()` landing
first, which it now has (see eo/mech_access.py). It reuses that
function's own knuckle/pin geometry to lay out the split seam rather
than inventing a second joint-primitive shape, per this guide's own
"reusing that phase's geometry rather than inventing a new joint type"
wording for this patch.

What this patch does NOT do (deferred to later Phase G patches):
  - No print-orientation reasoning (which face sits on the bed) or
    bed-rotation search (whether rotating the part 90 degrees in plan
    view would clear an axis it doesn't clear unrotated) -- this check
    is a direct, un-rotated axis-to-axis comparison against
    PRINT_BED_MM, matching this whole phase's own stated "simplicity
    over per-project configurability" posture (see PRINT_BED_MM's own
    docstring). A future patch can widen this into a rotation search
    if a real project's own housing is bed-limited only in its
    un-rotated orientation.
"""

from eo.enclosure_spec import PRINT_BED_MM
from eo.mech_access import generate_hinge


def check_bed_fit(mech: dict) -> dict:
    """Compares the final housing/baseplate's own build dimensions
    against `PRINT_BED_MM`, returns a pass/violation result.

    Reads `mech["housing"]`, never recomputes a footprint. Handles
    each of the three shapes eo/mech_enclosure.py's own
    apply_enclosure_generation() can stash there:

      - `None` (Patch A.5's `none`-mode no-op, OR nothing generated
        yet because `mech["device"]["footprint"]` doesn't exist yet):
        nothing built, nothing to violate -- returns `{"ok": True,
        "dims_mm": None, "bed_mm": PRINT_BED_MM, "violations": []}`.
        Same "nothing to derive from yet is a no-op, not a failure"
        posture apply_enclosure_generation() itself already takes
        toward a missing device footprint.
      - `{"outer": {...}}` (Patch A.5's `partial`-mode baseplate, no
        "lid" key): build height is `outer["d"]` alone -- a baseplate
        has no matching lid stacked on top of it (see
        compute_baseplate_footprint()'s own docstring: "a baseplate
        never gets a matching lid").
      - `{"outer": {...}, "inner": {...}, "lid": {...}}` (`full`-mode
        housing): build height is `outer["d"] + lid["d"]` -- the lid
        is a SEPARATE printed piece that sits externally on top of the
        housing shell (`lid.z = outer.z + outer.d`, per
        compute_housing_footprint()'s own docstring), so the tallest
        single print in this pair is the housing body alone, but the
        COMBINED stack height (what actually has to have existed on
        the bed across the housing's print plus the lid's own print)
        is what a real build-plate constraint cares about here --
        conservative by construction, since checking the sum can only
        ever flag a violation the two individual prints wouldn't have
        flagged on their own, never miss one.

    Axis mapping (matching eo/mech_cutouts.py's own documented
    convention throughout this tree): "w" spans the x axis, "h" spans
    the y axis, "d" spans the z axis -- so `outer["w"]`/`["h"]`/["d"]
    (plus `lid["d"]` for the full-mode case above) map directly onto
    `PRINT_BED_MM`'s own "x"/"y"/"z" keys, no relabeling needed.

    Missing "w"/"h"/"d" on `outer` (or "d" on `lid`) default to 0,
    same "tolerant of a partial dict, never raises" posture every pure
    function in this tree already holds itself to.

    Returns `{"ok": bool, "dims_mm": {"x", "y", "z"} | None,
    "bed_mm": PRINT_BED_MM, "violations": [{"axis": "x"|"y"|"z",
    "extent_mm": mm, "bed_mm": mm, "over_mm": mm}, ...]}`. `"ok"` is
    `True` iff `"violations"` is empty -- same "flag, never raise"
    contract every other check_* function in this package already
    documents, for the identical reason: a caller (Patch G.3's future
    auto-split, or Patch G.4's own test suite) decides what to do with
    a violation, this function only measures and reports one. A
    dimension exactly equal to its own bed axis is NOT a violation
    (`>`, not `>=` -- fits exactly is still a fit).

    Pure function: never mutates `mech`, never does I/O.
    """
    housing = (mech or {}).get("housing")

    if not isinstance(housing, dict) or not isinstance(housing.get("outer"), dict):
        return {"ok": True, "dims_mm": None, "bed_mm": PRINT_BED_MM, "violations": []}

    outer = housing["outer"]
    lid = housing.get("lid")

    w = float(outer.get("w") or 0)
    h = float(outer.get("h") or 0)
    d = float(outer.get("d") or 0)
    if isinstance(lid, dict):
        d += float(lid.get("d") or 0)

    dims_mm = {"x": round(w, 3), "y": round(h, 3), "z": round(d, 3)}

    violations = []
    for axis, extent in dims_mm.items():
        bed_extent = float(PRINT_BED_MM.get(axis) or 0)
        if extent > bed_extent:
            violations.append(
                {
                    "axis": axis,
                    "extent_mm": extent,
                    "bed_mm": bed_extent,
                    "over_mm": round(extent - bed_extent, 3),
                }
            )

    return {"ok": not violations, "dims_mm": dims_mm, "bed_mm": PRINT_BED_MM, "violations": violations}


# ---------------------------------------------------------------------------
# Patch G.3 -- auto-split on a check_bed_fit() violation.
# ---------------------------------------------------------------------------
#
# Maps each real-world axis ("x"/"y"/"z", the same keys check_bed_fit()'s
# own "dims_mm"/"bed_mm" already use) onto the {"pos", "extent"} key pair
# that axis reads/writes on a housing "outer"/"lid"-shaped box -- same
# "w" <-> x, "h" <-> y, "d" <-> z convention check_bed_fit()'s own
# docstring above already documents.
_AXIS_DIM_KEYS = {
    "x": ("x", "w"),
    "y": ("y", "h"),
    "z": ("z", "d"),
}

# Literal per this guide's own Patch G.3 wording ("a fastened joint,
# reusing that phase's geometry rather than inventing a new joint type")
# -- a single, fixed name for the one split seam a project's own single
# housing/baseplate (Part 1's own item 9, multi-body assembly, is out of
# scope for this whole guide) can ever produce, same "one shared
# structural part, no per-project disambiguation needed" reasoning eo/
# mech_cutouts.py's own _HOUSING_ID_PREFIX/eo/mech_enclosure.py's own
# _LID_ID_PREFIX already apply to their own single fixed ids.
_SPLIT_SEAM_ID = "housing_split_seam"


def _pick_longest_axis(dims_mm: dict, exclude: str = None) -> str:
    """Returns whichever of `dims_mm`'s own "x"/"y"/"z" keys (excluding
    `exclude`, if given) has the largest extent -- ties broken x, then
    y, then z, same deterministic left-to-right tie-break precedent
    eo/mech_supports.py's own corner-primitive ordering already uses
    for its own ties. Internal helper: used once to pick the SPLIT axis
    (`exclude=None`, across all three), and once more to pick which of
    the two REMAINING axes the seam itself runs along (`exclude=`
    the already-chosen split axis).
    """
    candidates = [a for a in ("x", "y", "z") if a != exclude]
    return max(candidates, key=lambda a: dims_mm.get(a, 0))


def _split_outer_box(outer: dict, split_axis: str) -> tuple:
    """Splits `outer` (a housing/baseplate `{"x","y","z","w","h","d"}`
    box) into two equal halves along `split_axis`, at its own midpoint
    -- literal Patch G.3 wording ("split... into two halves along its
    longest axis"). Every field the two halves don't differ on (the two
    axes NOT being split, plus any other key `outer` happens to carry)
    is copied through unchanged from `outer` on both halves, same
    "copy, then override only what changed" posture eo/mech_access.py's
    own `_section_box()` establishes for a defaulted-fields copy one
    level up.

    Returns `(half_a, half_b)`, each shaped identically to `outer`.
    `half_a` keeps `outer`'s own origin along `split_axis`; `half_b`
    starts at the midpoint. Both get exactly half of `outer`'s own
    extent along `split_axis` -- the two halves' own footprints
    together exactly reconstruct `outer`'s own original footprint, no
    gap and no overlap.
    """
    pos_key, extent_key = _AXIS_DIM_KEYS[split_axis]

    origin = float(outer.get(pos_key) or 0)
    extent = float(outer.get(extent_key) or 0)
    half_extent = round(extent / 2.0, 3)
    midpoint = round(origin + half_extent, 3)

    half_a = dict(outer)
    half_a[extent_key] = half_extent

    half_b = dict(outer)
    half_b[pos_key] = midpoint
    half_b[extent_key] = half_extent

    return half_a, half_b


def _generate_split_seam(outer: dict, split_axis: str, seam_axis: str) -> dict:
    """Generates the split-seam geometry at `outer`'s own `split_axis`
    midpoint, running along `seam_axis` -- by calling eo/mech_access.py's
    own `generate_hinge()` (Patch D.2) directly, per this patch's own
    "reusing that phase's geometry rather than inventing a new joint
    type" wording, rather than a hand-rolled interlocking-tooth shape.

    `generate_hinge()` itself is hard-coded to space its own knuckles
    along whatever it's given as `section["w"]`, starting from
    `section["x"]`, at a fixed `section["y"]`/`section["z"]` -- it has
    no notion of which REAL axis that spacing represents. This function
    is the adapter: it builds a `section` argument whose own "w"/"x"
    slots carry `seam_axis`'s own extent/origin (so the knuckle spacing
    lands on the correct real axis), and whose "y"/"z" slots carry the
    split midpoint (`split_axis`) and the fixed remaining axis
    (`fixed_axis`) respectively -- then, since `generate_hinge()`'s own
    output primitives always come back keyed literally "x"/"y"/"z"
    (its own field names, not axis-aware), remaps each primitive's
    three coordinate fields back onto the REAL "x"/"y"/"z" keys they
    actually represent before returning. Every non-coordinate field on
    each primitive (`"type"`, `"member"`, `"diameter_mm"`,
    `"length_mm"`, `"bore_diameter_mm"`) passes through unchanged.

    Returns `{"section_id": _SPLIT_SEAM_ID, "access_type":
    "fastened_split_seam", "fastened": True, "primitives": [...]}` --
    deliberately a DIFFERENT `"access_type"` label than
    `generate_hinge()`'s own `"hinged"` (even though the underlying
    knuckle/pin primitives are byte-for-byte its own output, just
    coordinate-remapped), and an explicit `"fastened": True` flag: this
    seam is glued/bolted permanently shut once assembled (it exists to
    let an oversized print exist on a real bed at all, not to let a
    finished device be opened), never a functional pivoting joint --
    same "print-in-place-clearance hinge, not a living hinge"
    knuckle/pin geometry ACCESS_GEOMETRY["hinged"]'s own docstring
    already describes, repurposed here as a permanent registration
    feature rather than a moving one. A downstream consumer (Phase 6's
    future manufacturability pass, MechView.jsx) can tell this apart
    from a real declared hinge by this label alone, without needing to
    inspect individual primitive types.
    """
    fixed_axis = next(a for a in ("x", "y", "z") if a not in (split_axis, seam_axis))

    seam_pos_key, seam_extent_key = _AXIS_DIM_KEYS[seam_axis]
    split_pos_key, split_extent_key = _AXIS_DIM_KEYS[split_axis]
    fixed_pos_key, _ = _AXIS_DIM_KEYS[fixed_axis]

    split_midpoint = round(
        float(outer.get(split_pos_key) or 0) + float(outer.get(split_extent_key) or 0) / 2.0, 3
    )

    section_arg = {
        "section_id": _SPLIT_SEAM_ID,
        "x": float(outer.get(seam_pos_key) or 0),
        "w": float(outer.get(seam_extent_key) or 0),
        "y": split_midpoint,
        "z": float(outer.get(fixed_pos_key) or 0),
    }

    hinge_result = generate_hinge(section_arg)

    # generate_hinge()'s own primitive "x"/"y"/"z" fields (in that
    # order) map onto (seam_axis, split_axis, fixed_axis) respectively
    # -- see this function's own docstring. Remap each one onto the
    # REAL axis key it represents, same order, before handing it back.
    remapped = []
    for primitive in hinge_result.get("primitives", []):
        new_primitive = dict(primitive)
        new_primitive[seam_axis] = primitive.get("x")
        new_primitive[split_axis] = primitive.get("y")
        new_primitive[fixed_axis] = primitive.get("z")
        # Drop the original generic keys where a real axis key didn't
        # already overwrite them (e.g. seam_axis == "x" leaves "x" as
        # both the generic AND the real key -- a harmless no-op
        # overwrite in that case, but seam_axis == "y" or "z" would
        # otherwise leave a stale, meaningless "x" key behind).
        for generic_key in ("x", "y", "z"):
            if generic_key not in (seam_axis, split_axis, fixed_axis):
                new_primitive.pop(generic_key, None)
        remapped.append(new_primitive)

    return {
        "section_id": _SPLIT_SEAM_ID,
        "access_type": "fastened_split_seam",
        "fastened": True,
        "primitives": remapped,
    }


def apply_bed_fit_split(mech: dict) -> dict:
    """Patch G.3: on a `check_bed_fit()` violation, splits
    `mech["housing"]["outer"]` into two halves along its own longest
    axis and generates a fastened split seam across the cut, per this
    guide's own literal Patch G.3 wording.

    Calls `check_bed_fit()` above internally rather than re-deriving
    the same dims/violation logic a second time -- same "don't
    duplicate a check that already exists" posture Patch 6.2's own
    aggregation (eo/mech_manufacturability.py's own
    `build_manufacturability_report()`) already takes toward its own
    upstream per-cutout/per-standoff checks.

      - No violation (`check_bed_fit(mech)["ok"]` is `True`, whether
        because the housing already fits or because there's no
        housing at all yet): stashes `None` onto the new
        `mech["housing_split"]` key and returns `None` -- literal
        Patch G.4 wording this patch sets up for ("in-bounds housing
        -> unchanged, no split applied").
      - A violation: picks the LONGEST of the three axes in
        `check_bed_fit()`'s own `"dims_mm"` (independent of which
        specific axis/axes actually violated `PRINT_BED_MM` -- literal
        "split... along its longest axis" wording, not "along its
        violating axis"), splits `mech["housing"]["outer"]` in half
        along that axis via `_split_outer_box()`, and generates one
        seam across the cut via `_generate_split_seam()` -- the seam's
        own run direction is whichever of the two REMAINING axes has
        the larger extent (ties broken x, then y, then z), giving the
        longest practical span for the knuckle/pin registration
        feature to spread across.

    Deliberately does NOT touch `mech["housing"]` or `mech["enclosure"]`
    itself -- both keys' existing shape is read by other, already-landed
    consumers (eo/mech_cutouts.py, eo/mech_supports.py,
    frontend/app/components/MechView.jsx's own flat `enclosure.w`/`.h`/
    `.d` reads), so overwriting either with a two-piece shape here would
    silently break them, the same regression eo/mech_enclosure.py's own
    `apply_enclosure_generation()` docstring already explicitly guards
    against for its own `mech["enclosure"]` write. This patch instead
    stashes its own result on the new, non-colliding `mech["housing_split"]`
    key -- same "new key, not an overload of an existing one" posture
    Phase B's own `mech["exclusions"]` and Phase D's own `mech["access"]`
    already established for themselves.

    Returns (and stashes onto `mech["housing_split"]`)
    `{"split": True, "axis": "x"|"y"|"z", "halves": [outer_a, outer_b],
    "seam": {...see _generate_split_seam()...}}` on a violation, or
    `None` (see above) otherwise.

    Missing/malformed `mech["housing"]["outer"]` degrades to the same
    `None` no-op `check_bed_fit()` itself already returns for that
    input -- never raises.
    """
    fit = check_bed_fit(mech)

    if fit["ok"]:
        if isinstance(mech, dict):
            mech["housing_split"] = None
        return None

    outer = mech["housing"]["outer"]

    split_axis = _pick_longest_axis(fit["dims_mm"])
    seam_axis = _pick_longest_axis(fit["dims_mm"], exclude=split_axis)

    half_a, half_b = _split_outer_box(outer, split_axis)
    seam = _generate_split_seam(outer, split_axis, seam_axis)

    result = {"split": True, "axis": split_axis, "halves": [half_a, half_b], "seam": seam}

    if isinstance(mech, dict):
        mech["housing_split"] = result

    return result
