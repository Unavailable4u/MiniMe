"""
eo/mech_cutouts.py — Phase 5, Patches 5.2/5.3/5.4 of the Mech/Enclosure
implementation guide: the pure, deterministic cutout-geometry functions
that open the housing shell (Phase 1) up to the outside world for every
part that needs to be seen, heard, pressed, or plugged into from
outside the enclosure (Master Guide, Phase 5 "Goal": gap #3).

Same build-order reasoning every earlier "pure function(s) first" patch
in this tree already established (eo/mech_enclosure.py's own
compute_housing_footprint() before apply_enclosure_generation(), eo/
mech_supports.py's own compute_standoffs()/compute_screw_bosses()
before apply_supports_generation()): land the mechanical, side-effect-
free geometry here, independently testable with plain dict inputs,
before Patch 5.6 wires an apply_cutout_generation() wrapper into the
pipeline's mutate-in-place convention.

Reads two pieces of Patch 5.1 config from eo/enclosure_spec.py:
  - CUTOUT_TABLE: keyword (lowercase substring of a part's own
    "generic_name") -> cutout descriptor (cutout_type/shape/sizing).
  - CUTOUT_ELIGIBLE_CATEGORIES: the coarse category pre-filter
    ({"mcu","sensor","actuator","power","module"}) applied BEFORE ever
    running CUTOUT_TABLE's own keyword scan -- see that module's own
    docstring for why category alone can never be the dispatch key.

Input shape: every function in this module takes a "part" dict shaped
like eo/mech_supports.py's own `_joined_section_members()` output --
{"part_id","x","y","z","w","h","d","category","generic_name",...}, a
placement already joined with its BOM part's own category/generic_name.
Patch 5.6's own apply_cutout_generation() is what actually does that
join for this module, the same split eo/mech_supports.py's own Patch
2.4 already establishes relative to Patch 2.2/2.3's pure functions --
this module never reads `mech` or `parts` directly.

Axis convention (matching eo/mech_device.py's own documented
convention throughout this tree): "w" spans the x axis, "h" spans the
y axis (the front/center/edge in-plane axis), "d" spans the z axis
(the vertical stacking axis a part's own "z" position sits on). A
housing/part footprint dict is always {"x","y","z","w","h","d"} in
that same shape -- eo/mech_enclosure.py's own compute_housing_footprint()
"inner"/"outer"/"lid" results, and every placement in mech["placements"],
already share it.
"""

from eo.enclosure_spec import (
    CUTOUT_ELIGIBLE_CATEGORIES,
    CUTOUT_TABLE,
    DEFAULT_MATERIAL,
    ENCLOSURE_SPEC,
    MATERIAL_PROPERTIES,
)
from eo.mech_material import resolve_material
from eo.mech_sections import subsections_for_section
from eo.mech_subsections import members_for_subsection
from eo.mech_swept_volume import is_exclusion
from eo.mech_thermal import lookup_thermal

# Patch E.3 (Phase E, "Material awareness"): which BOM part_id prefix
# identifies the shared structural part whose wall this module's own
# cutouts are drilled through -- same "duplicated here rather than
# imported since eo/mech_enclosure.py is a peer, not a dependency, of
# this module" convention every other cross-module id-prefix constant
# in this tree already follows (see eo/mech_enclosure.py's own
# _HOUSING_ID_PREFIX docstring comment for the identical reasoning).
_HOUSING_ID_PREFIX = "housing"

# ---------------------------------------------------------------------------
# Patch 5.2 -- nearest_exterior_face()
# ---------------------------------------------------------------------------
#
# Which of the housing's six walls a part's own footprint (its
# placement, not the housing) sits closest to -- literal Master Guide
# wording ("nearest_exterior_face(part_footprint, housing_inner) ->
# str -- pure geometry comparison, returns which wall a part is closest
# to"). This is Phase 5's own hardest piece of pure logic (the patch
# breakdown's own words: "the trickiest bit of Phase 5 logic"), so it's
# kept isolated and independently testable before Patch 5.3/5.4's own
# cutout-shape code ever depends on its output.

# Face label -> which of the three footprint dimension keys ("w"/"h"/
# "d") is that face's own surface NORMAL -- i.e. the axis a cutout
# through that face is drilled ALONG. The other two dimension keys are
# the face's own in-plane (width/height-of-the-opening) axes -- see
# _in_plane_extents() below, shared by Patch 5.3/5.4.
_FACE_NORMAL_DIM = {
    "+x": "w", "-x": "w",
    "+y": "h", "-y": "h",
    "+z": "d", "-z": "d",
}

# Stable iteration order used only to break an exact tie between two
# faces at equal distance (float equality on planned geometry is rare
# but not impossible, e.g. a part perfectly centered on one axis) --
# same "never let ambiguous input make the result non-deterministic"
# posture every pure-planning function in this tree already holds
# itself to (eo/mech_supports.py's own _CORNER_FRACTIONS ordering is
# the same idea one function over).
_FACE_ORDER = ("-x", "+x", "-y", "+y", "-z", "+z")

# Dimension key ("w"/"h"/"d") -> its own matching position key ("x"/"y"/
# "z") -- shared by nearest_exterior_face()'s own gap math above and
# Patch 5.5's check_min_wall_thickness() below, so both read the exact
# same axis pairing rather than each re-deriving it.
_DIM_TO_POS = {"w": "x", "h": "y", "d": "z"}


def nearest_exterior_face(part_footprint: dict, housing_inner: dict) -> str:
    """Returns one of "+x"/"-x"/"+y"/"-y"/"+z"/"-z" -- the housing_inner
    wall `part_footprint` sits closest to, measured as the plain gap
    between the part's own footprint edge and that wall's own plane on
    the matching axis (e.g. the "-x" gap is `part.x - inner.x`; the
    "+x" gap is `(inner.x + inner.w) - (part.x + part.w)`), same
    "measure from the two facing edges, not centers" shape every other
    clearance/collision check in this tree already uses (eo/
    mech_enclosure.py's own `clearance_mm` is exactly this same
    edge-to-edge gap, one level up, for the whole device footprint
    rather than one part).

    `housing_inner` is meant to be `mech["housing"]["inner"]` -- the
    CAVITY footprint eo/mech_enclosure.py's own
    compute_housing_footprint() already returns, not "outer" (a part
    is never placed against the outer shell's own face; it's always
    placed against the inner wall a cutout is drilled outward FROM).

    Missing x/y/z/w/h/d keys on either dict default to 0 -- same
    "tolerant of a partial dict, never raises" posture every pure
    function in this tree already holds itself to toward its own
    inputs (eo/mech_enclosure.py's own `_expand()`, eo/mech_supports.py's
    own `_corner_primitives()`).

    Negative gaps (the part's footprint already extends past that
    wall, e.g. because an earlier repair pass hasn't run yet) are
    clamped to 0 before comparison -- this function never returns a
    face based on how far a part has ALREADY penetrated a wall, only
    which wall it's nearest to; a part flush against or past a wall is,
    by definition, "nearest" to that wall at a floor of 0.

    Ties (two faces at the exact same clamped gap) resolve by
    `_FACE_ORDER`'s own fixed sequence -- deterministic, never
    input-order- or dict-iteration-order-dependent.
    """
    def _f(d: dict, key: str) -> float:
        return float((d or {}).get(key) or 0)

    px, py, pz = _f(part_footprint, "x"), _f(part_footprint, "y"), _f(part_footprint, "z")
    pw, ph, pd = _f(part_footprint, "w"), _f(part_footprint, "h"), _f(part_footprint, "d")
    ix, iy, iz = _f(housing_inner, "x"), _f(housing_inner, "y"), _f(housing_inner, "z")
    iw, ih, idd = _f(housing_inner, "w"), _f(housing_inner, "h"), _f(housing_inner, "d")

    gaps = {
        "-x": px - ix,
        "+x": (ix + iw) - (px + pw),
        "-y": py - iy,
        "+y": (iy + ih) - (py + ph),
        "-z": pz - iz,
        "+z": (iz + idd) - (pz + pd),
    }
    gaps = {face: max(gap, 0.0) for face, gap in gaps.items()}

    best_gap = min(gaps.values())
    for face in _FACE_ORDER:
        if gaps[face] == best_gap:
            return face
    return _FACE_ORDER[0]  # unreachable -- _FACE_ORDER covers every gaps key


# ---------------------------------------------------------------------------
# Shared helpers -- used by both Patch 5.3's generate_cutout() and Patch
# 5.4's generate_port_cutout(). Kept private/un-exported: eo/
# enclosure_spec.py's own CUTOUT_TABLE docstring is explicit that
# "Patch 5.2/5.3 (not this patch) are what actually lowercase-substring-
# match a part's generic_name against this table's own keys" -- both
# patches share exactly one matching implementation here rather than
# each hand-rolling their own, same de-duplication reasoning eo/
# mech_supports.py's own private `_corner_primitives()` already applies
# to compute_standoffs()/compute_screw_bosses().
# ---------------------------------------------------------------------------

def _match_cutout_descriptor(part: dict):
    """Returns `(keyword, descriptor)` for the first CUTOUT_TABLE entry
    whose key appears as a case-insensitive substring of `part`'s own
    "generic_name" -- e.g. a part BOM'd "Piezo Buzzer Module" matches
    the "buzzer" keyword/descriptor pair. Returns `None` if `part`
    fails the category pre-filter, has no usable "generic_name", or
    matches no keyword at all.

    Category gate applied FIRST (cheap, exact-match against
    CUTOUT_ELIGIBLE_CATEGORIES) before the keyword scan -- literal
    ordering eo/enclosure_spec.py's own CUTOUT_ELIGIBLE_CATEGORIES
    docstring already specifies ("Patch 5.2/5.3 apply this FIRST...
    before ever running CUTOUT_TABLE's own keyword scan"): a
    3D_PRINT/MISC part (housing, lid, standoffs, fasteners) is never
    cutout-eligible regardless of what its generic_name happens to
    contain.

    Iterates CUTOUT_TABLE in its own declared (insertion) order, first
    match wins -- deterministic, and matches eo/enclosure_spec.py's own
    documented resolution for "led"/"indicator" being kept as two
    separate literal keys rather than one ordering-dependent alias.

    Never raises on a malformed `part` (missing keys, non-string
    generic_name, etc.) -- same fail-safe, "no match found" posture
    every other pure lookup in this tree already holds toward
    incomplete input.
    """
    if not isinstance(part, dict):
        return None
    if part.get("category") not in CUTOUT_ELIGIBLE_CATEGORIES:
        return None

    generic_name = part.get("generic_name")
    if not isinstance(generic_name, str) or not generic_name.strip():
        return None
    generic_name = generic_name.lower()

    for keyword, descriptor in CUTOUT_TABLE.items():
        if keyword in generic_name:
            return keyword, descriptor
    return None


def _in_plane_extents(part: dict, face: str) -> tuple:
    """Returns `(dim1, dim2)` -- `part`'s own footprint extent along
    the two dimension keys ("w"/"h"/"d") that lie IN the plane of
    `face` (i.e. every dimension key except `_FACE_NORMAL_DIM[face]`,
    the axis a cutout through that face is drilled along). Dict
    iteration over `{"w", "h", "d"} - {normal}` is not itself ordered,
    so this returns a fixed, deterministic pairing keyed off a literal
    axis triple rather than a set difference, so two calls with the
    same `part`/`face` always return dims in the same order.
    """
    normal = _FACE_NORMAL_DIM[face]
    ordered_dims = [dim for dim in ("w", "h", "d") if dim != normal]
    return tuple(float(part.get(dim) or 0) for dim in ordered_dims)


def _part_id(part: dict):
    """`part`'s own identity for a returned cutout primitive -- prefers
    "part_id" (the field every eo/mech_supports.py-style joined member
    dict carries, per this module's own top docstring on its input
    shape) and falls back to a bare "id" so a caller that happens to
    pass a raw BOM part dict instead still gets a usable identifier
    rather than `None`.
    """
    if not isinstance(part, dict):
        return None
    return part.get("part_id") or part.get("id")


# ---------------------------------------------------------------------------
# Patch 5.5 -- check_min_wall_thickness(): the minimum-wall-thickness
# guard every cutout from Patch 5.3/5.4 gets run through, per the
# breakdown's own wording: "Before accepting any cutout from 5.3/5.4,
# check it doesn't leave a wall segment thinner than
# ENCLOSURE_SPEC['min_feature_mm']; reject/flag violations rather than
# silently emitting bad geometry." Landed as its own function, kept
# deliberately separate from generate_cutout()/generate_port_cutout()
# themselves ("Why this size" in the breakdown: "Cross-cutting
# validation, deliberately separate from the generators themselves so
# the guard can be tested against both shape families uniformly").
# ---------------------------------------------------------------------------

def check_min_wall_thickness(
    part: dict, face: str, cutout: dict, housing_inner: dict, material: str = DEFAULT_MATERIAL
) -> dict:
    """Checks whether `cutout` (a dict already returned by
    generate_cutout() or generate_port_cutout(), for `part` opened
    through `face`) leaves a wall segment on that face thinner than
    `ENCLOSURE_SPEC["min_feature_mm"]` anywhere around its own opening.

    "Wall segment" here means the strip of housing wall BETWEEN the
    cutout's own opening and that face's own outer boundary (the
    housing's own edge on the two in-plane axes) -- not the housing's
    shell thickness itself (`ENCLOSURE_SPEC["wall_thickness_mm"]`,
    the material the cutout is drilled straight THROUGH, which this
    function never checks; a cutout is always a through-hole by
    definition in this pipeline, so there's no wall segment to measure
    along the normal axis). A cutout placed too close to where two
    walls meet (near a housing corner/edge on its own face) is exactly
    the failure mode this guard exists to catch -- literal Master
    Guide wording: "reject/flag any cutout that would leave a wall
    segment thinner than the minimum printable feature."

    Margin math, per in-plane axis (the same two dimension keys
    _in_plane_extents() already isolates for `face`):
      - `part`'s own low-side margin to the housing wall
        (`part_pos - housing_pos`) and high-side margin
        (`(housing_pos + housing_extent) - (part_pos + part_extent)`) --
        the same edge-to-edge gap shape nearest_exterior_face() itself
        already uses, one level up, to pick `face` in the first place.
      - PLUS half of any slack between `part`'s own footprint extent
        on that axis and the cutout's own (usually smaller, per
        bezel/clearance shrinkage) opening extent -- since Patch 5.3/
        5.4 both center the cutout's opening within the part's own
        footprint rather than flush to one edge, that slack is real
        extra margin the cutout's own opening enjoys beyond the part's
        raw position, and ignoring it would under-count the true
        available wall and over-flag violations that aren't real.
      - A `cutout` whose "shape" is "circular" reads its own uniform
        "diameter_mm" on BOTH in-plane axes (a round hole has no
        separate width/height); "rectangular"/"port" shapes read their
        own "width_mm" (first in-plane axis, per
        _in_plane_extents()'s own fixed dimension ordering) and
        "height_mm" (second) respectively.

    Returns {"ok": bool, "margins": {dim: {"low": mm, "high": mm}, ...},
    "violations": [{"axis": dim, "side": "low"|"high", "margin_mm": mm},
    ...]}. `"ok"` is `True` iff `"violations"` is empty. NEVER raises --
    this is a flag, not a hard-fail, same "reject/flag... rather than
    silently emitting bad geometry" wording the breakdown itself uses
    ("flag" being the second, equally valid half of that same
    sentence): a caller that wants to hard-reject a violating cutout
    can inspect `"ok"`/`"violations"` and decide for itself, but this
    function never raises on `part`/`cutout`/`housing_inner` it can't
    fully evaluate -- missing keys default to 0 on every side, same
    "tolerant of a partial dict" posture every pure function in this
    module (and this whole tree) already holds itself to.

    Patch E.3 (Phase E, "Material awareness"): `material` (optional,
    defaults to `DEFAULT_MATERIAL` -- "pla_rigid") selects which
    material's own `MATERIAL_PROPERTIES` override this guard's own
    `min_feature_mm` floor is read from -- the material of the housing
    wall the cutout is actually being drilled through, not the cutout
    `part` itself. Same "material's own override, falling through to
    ENCLOSURE_SPEC's own baseline" lookup eo/mech_enclosure.py's own
    `compute_housing_footprint()` already applies to `wall_thickness_mm`
    (Patch E.3), applied here to `min_feature_mm` instead. Today's two
    defined materials (Patch E.1) both leave `min_feature_mm`
    unoverridden -- it's a print-process floor, not a material-
    stiffness one -- so a default/omitted call is numerically
    byte-for-byte unchanged from before this patch; the lookup exists
    so a future material that DOES override it is honored automatically.

    Pure function: never mutates `part`, `cutout`, or `housing_inner`.
    """
    overrides = MATERIAL_PROPERTIES.get(material) or {}
    min_feature = overrides.get("min_feature_mm", ENCLOSURE_SPEC["min_feature_mm"])
    normal = _FACE_NORMAL_DIM.get(face)
    in_plane_dims = [dim for dim in ("w", "h", "d") if dim != normal]
    cutout_shape = (cutout or {}).get("shape")

    margins = {}
    violations = []
    for index, dim in enumerate(in_plane_dims):
        pos_key = _DIM_TO_POS[dim]
        part_pos = float((part or {}).get(pos_key) or 0)
        part_extent = float((part or {}).get(dim) or 0)
        inner_pos = float((housing_inner or {}).get(pos_key) or 0)
        inner_extent = float((housing_inner or {}).get(dim) or 0)

        if cutout_shape == "circular":
            cutout_extent = float((cutout or {}).get("diameter_mm") or 0)
        elif index == 0:
            cutout_extent = float((cutout or {}).get("width_mm") or 0)
        else:
            cutout_extent = float((cutout or {}).get("height_mm") or 0)

        slack = max(part_extent - cutout_extent, 0.0) / 2.0
        low_margin = round(part_pos - inner_pos + slack, 3)
        high_margin = round((inner_pos + inner_extent) - (part_pos + part_extent) + slack, 3)
        margins[dim] = {"low": low_margin, "high": high_margin}

        if low_margin < min_feature:
            violations.append({"axis": dim, "side": "low", "margin_mm": low_margin})
        if high_margin < min_feature:
            violations.append({"axis": dim, "side": "high", "margin_mm": high_margin})

    return {"ok": not violations, "margins": margins, "violations": violations}


# ---------------------------------------------------------------------------
# Patch 5.3 -- generate_cutout(): the simple-shape generator (window,
# vent, through-hole, light-pipe). Deliberately excludes "port" --
# Patch 5.4's generate_port_cutout() below is that genuinely different
# code path, per this patch's own "why this size" note in the
# breakdown: "ports are the one shape needing extra shaping (connector
# envelope, not just a primitive hole)."
# ---------------------------------------------------------------------------

def generate_cutout(
    part: dict, face: str, cutout_type: str, housing_inner: dict = None, material: str = DEFAULT_MATERIAL
) -> dict:
    """Returns one cutout primitive dict for `part`, opened through
    `face` (one of nearest_exterior_face()'s own six return values),
    shaped per `cutout_type` -- one of "window" (display), "vent"
    (buzzer/mic), "through_hole" (button), or "light_pipe" (led/
    indicator). "port" is NOT handled here; see generate_port_cutout()
    below.

    `housing_inner` (Patch 5.5, optional, default `None`): when
    provided, this function additionally runs
    check_min_wall_thickness() against the geometry it just computed
    and attaches the result under the returned dict's own
    "wall_thickness_check" key -- {"ok","margins","violations"}, never
    raised as an exception (see that function's own docstring for why
    this is a flag, not a hard-fail). Omitting `housing_inner`
    (the default) skips the check entirely and omits the key, so every
    existing Patch 5.3 caller/test written before Patch 5.5 landed
    keeps returning exactly the same dict shape it always has --
    strictly additive, never a breaking change to this function's own
    established contract.

    `material` (Patch E.3, optional, defaults to `DEFAULT_MATERIAL`):
    forwarded as-is to `check_min_wall_thickness()` when `housing_inner`
    is supplied -- see that function's own docstring for what it
    selects (the housing wall's own material, not this cutout's `part`).
    Has no effect when `housing_inner` is omitted, since no
    wall-thickness check runs at all in that case.

    Re-derives `part`'s own CUTOUT_TABLE descriptor via
    _match_cutout_descriptor() (rather than trusting `cutout_type`
    alone) so a caller can never silently mismatch a part against the
    wrong descriptor's own sizing numbers -- raises `ValueError` if
    `part` doesn't match ANY keyword, or matches one whose OWN
    "cutout_type" disagrees with the `cutout_type` argument (e.g.
    calling this with cutout_type="window" for a part whose
    generic_name only matches the "buzzer" keyword). This is a
    programmer-error guard, not a normal-operation branch -- Patch
    5.6's own wrapper is expected to derive `cutout_type` from this
    same match in the first place, not guess independently.

    Sizing per shape (see eo/enclosure_spec.py's own CUTOUT_TABLE
    docstring for the reasoning behind each number):
      - "window" (rectangular): `part`'s own in-plane footprint on
        `face` (see _in_plane_extents()), each side shrunk by the
        descriptor's own "bezel_margin_mm" -- literal Master Guide
        wording ("rectangular window, sized to part footprint minus
        bezel margin"). Shrunk dims are floored at 0, never negative,
        for a part whose footprint is smaller than twice the bezel.
      - "vent" / "light_pipe" (circular, fixed size): the descriptor's
        own "hole_diameter_mm" -- these never read `part`'s own
        footprint at all, per CUTOUT_TABLE's own docstring ("always
        this table's own fixed size regardless of the part").
      - "through_hole" (circular, part-sized): diameter is the SMALLER
        of `part`'s own two in-plane extents (a button's shaft is
        assumed roughly circular; the smaller in-plane dimension is
        the safer, more conservative diameter to clear) plus the
        descriptor's own "clearance_mm" -- literal Master Guide
        wording ("through-hole matching actuator diameter"), read off
        the part per CUTOUT_TABLE's own docstring rather than a
        table-owned fixed size.

    Returned dict always carries {"part_id","face","cutout_type",
    "shape","keyword"} plus shape-specific sizing fields
    ({"width_mm","height_mm"} for rectangular; {"diameter_mm"} for
    circular, plus "hole_count"/"mesh_clearance_mm" when the matched
    descriptor itself carries them) -- "keyword" (the literal
    CUTOUT_TABLE key matched, e.g. "buzzer" not "mic" even though both
    share cutout_type "vent") is carried through so Patch 5.5's future
    min-wall-thickness guard and Phase 6's future manufacturability
    report can trace a cutout back to exactly which descriptor sized
    it, not just its coarser cutout_type/shape.
    """
    match = _match_cutout_descriptor(part)
    if match is None:
        raise ValueError(
            f"generate_cutout: part {_part_id(part)!r} matches no "
            f"CUTOUT_TABLE keyword (or fails the category pre-filter) -- "
            f"not a cutout-eligible part."
        )
    keyword, descriptor = match

    if descriptor["cutout_type"] != cutout_type:
        raise ValueError(
            f"generate_cutout: part {_part_id(part)!r} matched keyword "
            f"{keyword!r} (cutout_type={descriptor['cutout_type']!r}), "
            f"which disagrees with the requested cutout_type={cutout_type!r}."
        )
    if cutout_type == "port":
        raise ValueError(
            "generate_cutout: 'port' cutouts need a connector envelope, "
            "not just a primitive hole -- use generate_port_cutout() "
            "(Patch 5.4) instead."
        )

    result = {
        "part_id": _part_id(part),
        "face": face,
        "cutout_type": cutout_type,
        "shape": descriptor["shape"],
        "keyword": keyword,
    }

    if descriptor["shape"] == "rectangular":
        bezel = float(descriptor["bezel_margin_mm"])
        dim1, dim2 = _in_plane_extents(part, face)
        result["width_mm"] = round(max(dim1 - 2 * bezel, 0.0), 3)
        result["height_mm"] = round(max(dim2 - 2 * bezel, 0.0), 3)
    else:
        # Circular: "vent"/"light_pipe" use the descriptor's own fixed
        # hole_diameter_mm; "through_hole" derives its diameter from
        # the part's own in-plane footprint instead (see docstring
        # above).
        if cutout_type == "through_hole":
            dim1, dim2 = _in_plane_extents(part, face)
            base_diameter = min(dim1, dim2) if (dim1 and dim2) else max(dim1, dim2)
            clearance = float(descriptor.get("clearance_mm") or 0)
            result["diameter_mm"] = round(base_diameter + clearance, 3)
        else:
            result["diameter_mm"] = float(descriptor["hole_diameter_mm"])
            if "hole_count" in descriptor:
                result["hole_count"] = descriptor["hole_count"]
            if "mesh_clearance_mm" in descriptor:
                result["mesh_clearance_mm"] = descriptor["mesh_clearance_mm"]

    # Patch 5.5: flag (never raise) a minimum-wall-thickness violation
    # when the caller supplied `housing_inner` -- see this function's
    # own docstring above and check_min_wall_thickness()'s own
    # docstring below for why this is additive/optional.
    if housing_inner is not None:
        result["wall_thickness_check"] = check_min_wall_thickness(
            part, face, result, housing_inner, material=material
        )

    return result


# ---------------------------------------------------------------------------
# Patch 5.4 -- generate_port_cutout(): the USB/power-connector special
# case. Genuinely a different code path from generate_cutout() above,
# not a shape variant of it -- a port needs its own mating connector's
# envelope carried through so downstream FreeCAD generation (and
# Phase 6's future manufacturability pass) can distinguish "this
# opening must clear a physical plug sliding in and out" from an
# ordinary static hole.
# ---------------------------------------------------------------------------

def generate_port_cutout(
    part: dict, face: str, housing_inner: dict = None, material: str = DEFAULT_MATERIAL
) -> dict:
    """Returns one port-cutout primitive for `part`, opened through
    `face` -- literal Master Guide wording ("port-shaped cutout at
    part's footprint"). Only matches CUTOUT_TABLE keywords whose own
    "cutout_type" is "port" ("usb", "power_connector") -- raises
    `ValueError` for any part that doesn't match one of those, INCLUDING
    a part that matches a non-port keyword (e.g. calling this on a
    button), same "re-derive and cross-check the descriptor rather
    than trust the caller" posture generate_cutout() already takes
    above.

    `housing_inner` (Patch 5.5, optional, default `None`): identical
    opt-in "flag a violation, never raise" contract generate_cutout()'s
    own `housing_inner` parameter already documents above -- when
    supplied, the returned dict gains a "wall_thickness_check" key;
    omitted (the default), this function's return shape is unchanged
    from Patch 5.4's own original contract.

    `material` (Patch E.3, optional, defaults to `DEFAULT_MATERIAL`):
    identical forward-to-check_min_wall_thickness()-when-housing_inner-
    is-supplied contract generate_cutout()'s own `material` parameter
    already documents above.

    Envelope size is `part`'s own in-plane footprint on `face` (see
    _in_plane_extents()) expanded on every side by the matched
    descriptor's own "clearance_mm" -- literal "read the real
    footprint, don't guess a size here" reasoning eo/enclosure_spec.py's
    own CUTOUT_TABLE docstring already establishes for both "usb" and
    "power_connector"'s rows, with a more generous per-side clearance
    than "button"'s own through-hole case since a connector's mating
    plug slides in and out repeatedly rather than clearing a static
    shaft once.

    Returns both a flat {"width_mm","height_mm"} pair (matching
    generate_cutout()'s own rectangular-shape return shape, so a
    downstream consumer that doesn't care about the port/window
    distinction can still read a uniform width/height) AND a nested
    "connector_envelope_mm": {"width","height"} dict carrying the same
    two numbers under the semantically distinct names Master Guide
    Phase 5 itself calls out ("needs the connector's own envelope, not
    just a hole") -- the duplication is deliberate, not an oversight:
    it lets Phase 6's future manufacturability pass check specifically
    for envelope violations without also having to special-case which
    top-level keys mean "envelope" versus "opening" across every other
    cutout_type this module returns.
    """
    match = _match_cutout_descriptor(part)
    if match is None or match[1]["cutout_type"] != "port":
        raise ValueError(
            f"generate_port_cutout: part {_part_id(part)!r} does not "
            f"match a port-type CUTOUT_TABLE keyword ('usb' or "
            f"'power_connector')."
        )
    keyword, descriptor = match

    clearance = float(descriptor.get("clearance_mm") or 0)
    dim1, dim2 = _in_plane_extents(part, face)
    width = round(dim1 + 2 * clearance, 3)
    height = round(dim2 + 2 * clearance, 3)

    result = {
        "part_id": _part_id(part),
        "face": face,
        "cutout_type": "port",
        "shape": "port",
        "keyword": keyword,
        "width_mm": width,
        "height_mm": height,
        "connector_envelope_mm": {"width": width, "height": height},
    }

    # Patch 5.5: see generate_cutout()'s own identical block above for
    # why this is additive/opt-in and never raises.
    if housing_inner is not None:
        result["wall_thickness_check"] = check_min_wall_thickness(
            part, face, result, housing_inner, material=material
        )

    return result


# ---------------------------------------------------------------------------
# Patch F.3 (Phase F, Mech View standalone implementation guide) --
# automatic vent cutout for `thermal_class == "hot"` parts, INDEPENDENT of
# CUTOUT_TABLE's own keyword-matched descriptors above -- Part 1's own gap
# #4 ("nothing generates automatic ventilation"). A part like a linear
# voltage regulator or a stepper driver IC is curated "hot" in eo/
# mech_thermal.py's own THERMAL_TABLE (Patch F.1) but matches no
# CUTOUT_TABLE keyword at all today (it isn't a display/buzzer/mic/button/
# led/port), so apply_cutout_generation() below silently produced zero
# cutouts for it before this patch -- literal Patch F.3 wording: "producing
# a vent cutout even for a part category that wouldn't otherwise get one."
#
# Deliberately its own `cutout_type` ("thermal_vent", not "vent") even
# though the underlying shape is identical (circular, fixed hole size/
# count/mesh-clearance) to a CUTOUT_TABLE "vent" descriptor (buzzer/mic) --
# a thermal vent's job (bulk airflow for a heat-dissipating part) is a
# different sizing problem from a buzzer/mic's small acoustic port (bigger
# holes, more of them, no dust mesh -- screening a thermal vent would
# defeat its own purpose), so it gets its own literal sizing here rather
# than reusing "buzzer"/"mic"'s own CUTOUT_TABLE numbers. Kept local to
# this module (not added to eo/enclosure_spec.py's own CUTOUT_TABLE)
# because it is never reached via _match_cutout_descriptor()'s own
# keyword scan -- apply_cutout_generation() below calls
# generate_thermal_vent_cutout() directly, off `lookup_thermal()`, never
# off a CUTOUT_TABLE match.
_THERMAL_VENT_DESCRIPTOR = {
    "cutout_type": "thermal_vent",
    "shape": "circular",
    "hole_diameter_mm": 3.0,
    "hole_count": 6,
    "mesh_clearance_mm": 0.0,
}


def generate_thermal_vent_cutout(
    part: dict, face: str, housing_inner: dict = None, material: str = DEFAULT_MATERIAL
) -> dict:
    """Patch F.3's own generator -- a fixed-size circular vent cutout for
    `part`, opened through `face`, driven by `_THERMAL_VENT_DESCRIPTOR`
    above rather than a CUTOUT_TABLE keyword match. Unlike generate_cutout()
    above, this never calls `_match_cutout_descriptor()` and never raises
    on a part that matches no CUTOUT_TABLE keyword -- that's the entire
    point of this being a SEPARATE, independent code path: a caller
    (apply_cutout_generation() below) reaches this off `lookup_thermal(part)
    == "hot"` alone, regardless of what (if anything) `part`'s own
    generic_name matches in CUTOUT_TABLE.

    `housing_inner` (optional, default `None`): same Patch 5.5
    wall-thickness-check attachment generate_cutout()/generate_port_cutout()
    already provide -- when supplied, the returned dict additionally
    carries a `"wall_thickness_check"` key, never raised as an exception.

    `material` (Patch E.4, optional, default `DEFAULT_MATERIAL`): forwarded
    into check_min_wall_thickness() exactly like generate_cutout()/
    generate_port_cutout() already do, so a thermal vent's own wall-
    thickness floor is read from the same housing material as every other
    cutout on this part rather than silently defaulting.

    Returned dict shape mirrors generate_cutout()'s own circular-shape
    output: `{"part_id", "face", "cutout_type": "thermal_vent",
    "shape": "circular", "keyword": "thermal_hot", "diameter_mm",
    "hole_count", "mesh_clearance_mm"}` -- "keyword" is the fixed literal
    "thermal_hot" (not a CUTOUT_TABLE key, since none was matched) so a
    downstream manufacturability/report pass can still trace this cutout
    back to why it exists, same "keyword" traceability purpose
    generate_cutout()'s own docstring already documents for its own
    CUTOUT_TABLE-derived keyword.
    """
    result = {
        "part_id": _part_id(part),
        "face": face,
        "cutout_type": _THERMAL_VENT_DESCRIPTOR["cutout_type"],
        "shape": _THERMAL_VENT_DESCRIPTOR["shape"],
        "keyword": "thermal_hot",
        "diameter_mm": _THERMAL_VENT_DESCRIPTOR["hole_diameter_mm"],
        "hole_count": _THERMAL_VENT_DESCRIPTOR["hole_count"],
        "mesh_clearance_mm": _THERMAL_VENT_DESCRIPTOR["mesh_clearance_mm"],
    }

    if housing_inner is not None:
        result["wall_thickness_check"] = check_min_wall_thickness(
            part, face, result, housing_inner, material=material
        )

    return result


# ---------------------------------------------------------------------------
# Patch 5.6 -- pipeline-integration half of Phase 5.
# ---------------------------------------------------------------------------
#
# Everything above this line is Patch 5.2/5.3/5.4/5.5's pure functions,
# deliberately ignorant of `mech`/`parts`/eo/mech_sections.py -- same
# boundary this module's own top docstring already draws. This half is
# the opposite: it reads `mech["housing"]["inner"]` (Patch 1.3's own
# apply_enclosure_generation() output) and `mech["placements"]`/`parts`
# (via the same two-hop section->subsection->member join eo/
# mech_supports.py's own Patch 2.4 already established), calls
# nearest_exterior_face() + generate_cutout()/generate_port_cutout()
# per cutout-eligible member, and mutates `mech` in place -- same
# "mutate AND return" wrapper convention every apply_* function in this
# package already follows.

def _joined_cutout_members(mech: dict, parts: list) -> list:
    """Resolves every placement across EVERY section of `mech` into
    member dicts, joined with its BOM part's own "category"/
    "generic_name" by matching member["part_id"] against part["id"] --
    the same two-hop section->subsection->member resolution and
    "gate on the data, not a hardcoded section list" posture eo/
    mech_supports.py's own `_joined_section_members()` (Patch 2.4)
    already establishes, extended to also carry "generic_name" (which
    that function never needed, since SUPPORT_CATEGORIES is a pure
    category gate, but _match_cutout_descriptor() above needs it for
    its own keyword scan).

    Returns a NEW list of shallow-copied member dicts -- never mutates
    `mech["placements"]` or `parts`, same read-only posture
    `_joined_section_members()` already holds toward its own inputs.
    """
    parts_by_id = {
        p.get("id"): p for p in (parts or []) if isinstance(p, dict) and p.get("id")
    }

    joined = []
    for section in mech.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for subsection in subsections_for_section(mech, section):
            for member in members_for_subsection(mech, subsection):
                if not isinstance(member, dict):
                    continue
                part = parts_by_id.get(member.get("part_id"))
                merged = dict(member)
                if isinstance(part, dict):
                    merged["category"] = part.get("category")
                    merged["generic_name"] = part.get("generic_name")
                joined.append(merged)
    return joined


# ---------------------------------------------------------------------------
# Patch E.3 (Phase E, "Material awareness"): resolves the material of the
# housing wall every cutout in this module is drilled through -- same
# "join the placement against `parts` by id, then resolve_material()"
# pattern eo/mech_enclosure.py's own _resolve_structural_material()
# (Patch E.3) already establishes for that module's own housing/baseplate
# sizing, mirrored here since this module never imports that one (see
# this module's own top docstring: "never reads `mech` or `parts`
# directly" for the pure functions above -- this helper is part of this
# module's OWN Patch 5.6 pipeline-integration half, same boundary).
# ---------------------------------------------------------------------------

def _resolve_housing_material(mech: dict, parts: list, archetype: dict) -> str:
    """Finds the `_HOUSING_ID_PREFIX`-matched placement in `mech`'s own
    Enclosure section, joins it against `parts` by id, and resolves its
    material via Patch E.2's `resolve_material()`. Falls through to
    `DEFAULT_MATERIAL` when the housing hasn't been placed yet or isn't
    present in `parts` -- same fail-safe posture every other archetype-
    reading helper in this tree already holds itself to. Only ever
    called from `full`-mode `apply_cutout_generation()` (the only mode
    that reaches this point -- `partial`/`none` both short-circuit
    before any cutout is generated), so there is always exactly one
    housing wall's material to resolve here, never a baseplate's.
    """
    parts_by_id = {
        p.get("id"): p for p in (parts or []) if isinstance(p, dict) and p.get("id")
    }

    section = next(
        (s for s in (mech.get("sections") or [])
         if isinstance(s, dict) and s.get("section_id") == "Enclosure"),
        None,
    )
    if section is None:
        return DEFAULT_MATERIAL

    for subsection in subsections_for_section(mech, section):
        for member in members_for_subsection(mech, subsection):
            if not isinstance(member, dict):
                continue
            part_id = member.get("part_id") or ""
            if part_id.startswith(_HOUSING_ID_PREFIX):
                part = parts_by_id.get(part_id)
                return resolve_material(part, archetype) if isinstance(part, dict) else DEFAULT_MATERIAL

    return DEFAULT_MATERIAL


def apply_cutout_generation(mech: dict, parts: list) -> list:
    """Patch 5.6: wires nearest_exterior_face() + generate_cutout()/
    generate_port_cutout() into the pipeline. agents/hardware_speccer.py's
    own G3g call site runs this AFTER apply_supports_generation() --
    literal breakdown wording: "call after apply_supports_generation()
    (needs standoff positions available for the overlap awareness
    described in the master guide)." That "overlap awareness" is
    Phase 6's own future check ("Any cutout overlapping a standoff or
    another cutout?" -- Master Guide, Phase 6 "Design"), not something
    this function computes itself; this function only needs to run
    AFTER `mech["supports"]` exists so that a later Phase 6 pass has
    both `mech["supports"]` and `mech["cutouts"]` populated together,
    rather than checking cutouts against a supports key that isn't
    there yet.

    For every member resolved by `_joined_cutout_members()`, resolves
    its own cutout eligibility/keyword via `_match_cutout_descriptor()`
    (the same shared match Patch 5.3/5.4's own functions already use
    internally) -- a member that fails the category pre-filter or
    matches no CUTOUT_TABLE keyword is silently skipped, same "not
    every part gets a cutout" posture the category/keyword gate itself
    already establishes, not an error condition. For every member that
    DOES match, computes its own nearest exterior face via
    `nearest_exterior_face()` against `mech["housing"]["inner"]`, then
    dispatches to `generate_port_cutout()` (for a "port"-type match) or
    `generate_cutout()` (every other cutout_type) -- always passing
    `housing_inner` through so Patch 5.5's own wall-thickness flag rides
    along on every cutout this function emits, per this whole phase's
    own "reject/flag... rather than silently emitting bad geometry"
    posture.

    Patch F.3 (Mech View standalone implementation guide, Phase F):
    for EVERY cutout-eligible member (independent of, and in addition
    to, any CUTOUT_TABLE keyword match above), also checks
    `eo/mech_thermal.py`'s own `lookup_thermal(member["generic_name"])`
    -- a member curated `"hot"` there gets its own extra
    `generate_thermal_vent_cutout()` cutout appended, even when it
    matched no CUTOUT_TABLE keyword at all (or already got a different
    cutout from one). This is why a single member can now contribute
    up to TWO entries to the returned list, not just zero-or-one.
    Gated on the same `CUTOUT_ELIGIBLE_CATEGORIES` pre-filter
    `_match_cutout_descriptor()` itself already applies, so a
    3D_PRINT/MISC structural part is never thermal-vented regardless
    of its own `generic_name`. Deliberately calls ONLY
    `lookup_thermal()` (Patch F.1's free, deterministic table lookup),
    NEVER `estimate_thermal_and_vibration()` (Patch F.2's LLM
    fallback) -- same "keep the pure geometry function pure, wire an
    actual LLM call in as a later, separately-reviewable decision"
    posture eo/mech_balance.py's own `compute_cog()`/
    `compute_support_polygon()` already document for themselves toward
    eo/mech_mass.py's own `estimate_mass()`.

    Stashes the resulting list on the new `mech["cutouts"]` key (mirrors
    `mech["supports"]`'s own flat-list-of-primitives shape from Patch
    2.4, just one list instead of a `{"standoffs","bosses"}` split,
    since -- unlike standoffs/bosses -- there is only ever one cutout
    shape family active per member here, not two competing variants
    that need to be told apart) -- same "mutate AND return" convention
    every apply_* function in this package already follows.

    Returns `[]` (and stashes that same empty result onto
    `mech["cutouts"]`) when `mech` has no sections yet, OR when
    `mech["housing"]["inner"]` isn't populated yet (nothing to project
    an exterior face against) -- same "nothing to derive from yet"
    no-op posture `apply_supports_generation()` already takes toward
    `mech["sections"]`.

    Patch A.5 (Mech View standalone implementation guide, Phase A):
    also returns `[]` outright whenever
    `mech["archetype"]["enclosure_mode"]` is anything other than
    `full` (missing archetype reads back as `full`, same default every
    other A.5 call site uses) -- a part on an open frame (`partial`) or
    with no shared structural part at all (`none`) has no wall to cut a
    window into, so this is checked BEFORE the `mech["housing"]["inner"]`
    no-op below rather than left to fall out of it incidentally: in
    `partial` mode eo/mech_enclosure.py's own compute_baseplate_footprint()
    populates `mech["housing"]["outer"]` with no "inner" key, which
    would already no-op the check below, but gating explicitly here
    keeps that outcome a stated contract of this function rather than
    an accident of what the `partial`-mode housing shape happens to
    omit.

    Patch E.3 (Phase E, "Material awareness"): resolves the housing
    wall's own material once via `_resolve_housing_material()` and
    forwards it to every `generate_cutout()`/`generate_port_cutout()`
    call this function makes, so `check_min_wall_thickness()`'s own
    `min_feature_mm` floor (attached under each cutout's own
    "wall_thickness_check" key) is read from that material's own
    `MATERIAL_PROPERTIES` override where one exists. A housing's own
    `generic_name` is never strap/band-flavored in practice, so this
    resolves to `DEFAULT_MATERIAL` -- numerically unchanged from before
    this patch -- for every project today.
    """
    archetype = (mech or {}).get("archetype") or {}
    if archetype.get("enclosure_mode", "full") != "full":
        if isinstance(mech, dict):
            mech["cutouts"] = []
        return []

    if not isinstance(mech, dict) or not mech.get("sections"):
        if isinstance(mech, dict):
            mech["cutouts"] = []
        return []

    housing = mech.get("housing")
    housing_inner = housing.get("inner") if isinstance(housing, dict) else None
    if not isinstance(housing_inner, dict):
        mech["cutouts"] = []
        return []

    material = _resolve_housing_material(mech, parts, archetype)

    cutouts = []
    for member in _joined_cutout_members(mech, parts):
        # Patch B.6 (Mech View standalone implementation guide, Phase B)
        # leakage guard: a swept-volume exclusion box (eo/
        # mech_swept_volume.py's own Patch B.3/B.4/B.5 output, tagged
        # "shape_kind": "exclusion") is never a real, printable part
        # and must never become a cutout target. Exclusions are never
        # actually written into `mech["placements"]` today (see
        # eo/mech_swept_volume.py's own top docstring on why they live
        # on the separate `mech["exclusions"]` key instead), so this
        # check is currently defensive insurance against a future
        # refactor rather than something the CURRENT data flow can
        # trigger -- same "cheap, always-correct-to-call guard against
        # a future change" posture eo/mech_swept_volume.py's own
        # is_exclusion() docstring already documents for itself.
        if is_exclusion(member):
            continue
        match = _match_cutout_descriptor(member)
        if match is not None:
            _keyword, descriptor = match
            face = nearest_exterior_face(member, housing_inner)
            if descriptor["cutout_type"] == "port":
                cutout = generate_port_cutout(member, face, housing_inner=housing_inner, material=material)
            else:
                cutout = generate_cutout(
                    member, face, descriptor["cutout_type"], housing_inner=housing_inner, material=material
                )
            cutouts.append(cutout)

        # Patch F.3: independent of the CUTOUT_TABLE match above -- a
        # member never eligible for the category pre-filter is never
        # thermal-vented either (same pre-filter _match_cutout_descriptor()
        # itself already applies), everything else is checked regardless
        # of whether it also matched (or missed) a CUTOUT_TABLE keyword.
        if member.get("category") in CUTOUT_ELIGIBLE_CATEGORIES:
            if lookup_thermal(member.get("generic_name")) == "hot":
                face = nearest_exterior_face(member, housing_inner)
                cutouts.append(
                    generate_thermal_vent_cutout(member, face, housing_inner=housing_inner, material=material)
                )

    mech["cutouts"] = cutouts
    return cutouts
