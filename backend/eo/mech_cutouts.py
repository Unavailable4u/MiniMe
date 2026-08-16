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

from eo.enclosure_spec import CUTOUT_TABLE, CUTOUT_ELIGIBLE_CATEGORIES, ENCLOSURE_SPEC
from eo.mech_sections import subsections_for_section
from eo.mech_subsections import members_for_subsection

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

def check_min_wall_thickness(part: dict, face: str, cutout: dict, housing_inner: dict) -> dict:
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

    Pure function: never mutates `part`, `cutout`, or `housing_inner`.
    """
    min_feature = ENCLOSURE_SPEC["min_feature_mm"]
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

def generate_cutout(part: dict, face: str, cutout_type: str, housing_inner: dict = None) -> dict:
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
            part, face, result, housing_inner
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

def generate_port_cutout(part: dict, face: str, housing_inner: dict = None) -> dict:
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
            part, face, result, housing_inner
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
    """
    if not isinstance(mech, dict) or not mech.get("sections"):
        if isinstance(mech, dict):
            mech["cutouts"] = []
        return []

    housing = mech.get("housing")
    housing_inner = housing.get("inner") if isinstance(housing, dict) else None
    if not isinstance(housing_inner, dict):
        mech["cutouts"] = []
        return []

    cutouts = []
    for member in _joined_cutout_members(mech, parts):
        match = _match_cutout_descriptor(member)
        if match is None:
            continue
        _keyword, descriptor = match

        face = nearest_exterior_face(member, housing_inner)
        if descriptor["cutout_type"] == "port":
            cutout = generate_port_cutout(member, face, housing_inner=housing_inner)
        else:
            cutout = generate_cutout(member, face, descriptor["cutout_type"], housing_inner=housing_inner)
        cutouts.append(cutout)

    mech["cutouts"] = cutouts
    return cutouts
