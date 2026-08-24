"""
eo/mech_swept_volume.py — Mech View standalone implementation guide,
Phase B, Patches B.3/B.4/B.5 (+ the B.6 wiring glue this module also
owns): turns a moving part's own motion parameters (Patch B.1/B.2's
`eo/mech_motion.py`) into a cheap bounding-volume "exclusion box" other
geometry must not be generated inside — Part 1's own gap #2 ("No
modeling of moving parts... nothing prevents generated geometry from
physically colliding with a part mid-motion").

Per Phase B's own module-level framing in the guide: "Uses a cheap
bounding-volume approximation, not precise sector/arc geometry:
cylinders for continuous rotation, elongated boxes for linear motion,
and cardinal-angle-corrected bounding boxes for arcs — all ultimately
reduced to plain AABBs so the existing collision-checking code needs
no new geometry math." Every function below returns that same plain
{"x","y","z","w","h","d"} AABB shape every other placement/footprint in
this tree already uses (eo/mech_device.py's own documented axis
convention: "w" spans x, "h" spans y, "d" spans z; "x"/"y"/"z" is
always the box's own MIN corner, matching eo/mech_cutouts.py's own
{"x","y","w","h"} rectangle convention and every worked placement
example in eo/mech_device.py's own docstring).

Build order within this module mirrors the guide's own patch split:
  - B.3 (below): swept_aabb_rotational() / swept_aabb_linear() — the
    two motion families with no angular-crossing subtlety.
  - B.4 (below): swept_aabb_arc() — isolated on its own per the guide's
    own reasoning ("the one place a naive approximation is not just
    imprecise but unsafe").
  - B.5 (below): apply_tolerance() — the safety-margin step, kept
    independent of the raw shape math above it (mirrors how
    ENCLOSURE_SPEC["clearance_mm"] is applied AFTER the raw device
    footprint elsewhere in this pipeline, per that patch's own
    reasoning), plus the `"shape_kind": "exclusion"` tag every
    downstream consumer (Patch B.6, this module's own
    compute_swept_volumes()/apply_swept_volume_generation() below, and
    eo/mech_manufacturability.py's/eo/mech_cutouts.py's own B.6
    changes) keys off of.
  - B.6 wiring glue (below, NOT listed among B.6's own files in the
    guide, which names only eo/mech_manufacturability.py,
    eo/mech_cutouts.py, and "STL export path"): B.6's own "done when"
    criterion — "A housing wall generated too close to a servo's swept
    arc is flagged as a violation by the existing collision checker" —
    is only true once SOMETHING has actually read `mech["placements"]`,
    looked up each member's motion via B.1/B.2, dispatched to the right
    swept_aabb_*() function above, tolerance-expanded it, and stashed
    the result somewhere eo/mech_manufacturability.py can see. No other
    patch in Phase B claims that step, and every other phase in this
    tree keeps its own "read placements, build the phase's own derived
    list, stash it on `mech`" wrapper in the same module that owns the
    geometry math (eo/mech_supports.py's own apply_supports_generation(),
    eo/mech_cutouts.py's own apply_cutout_generation()) rather than in
    the module that consumes the result — so that same wrapper lives
    here, not in eo/mech_manufacturability.py.

Where the result lives: a NEW flat list on `mech["exclusions"]`
(mirrors `mech["cutouts"]`'s own flat-list-of-primitives shape from
Patch 5.6), not appended into `mech["placements"]` itself. Swept
volumes can only be computed AFTER a part's position is FINAL — same
"don't act on placements the validator hasn't signed off on yet"
ordering eo/mech_supports.py's own apply_supports_generation() already
documents for itself — which is after run_level_3_4_repair(), long
after mech["sections"]/mech["subsections"] (and therefore
mech["placements"]'s own section/subsection membership) were already
built earlier in the pipeline. Appending a late entry into
mech["placements"] at that point would never be resolvable back through
subsections_for_section()/members_for_subsection() (both keyed off
subsection membership decided much earlier), so a separate list is the
only shape that actually works here, not a stylistic choice.
"""
import math

from eo.mech_motion import lookup_motion
from eo.mech_sections import subsections_for_section
from eo.mech_subsections import members_for_subsection

# Default outward expansion applied by apply_tolerance() (B.5) when a
# caller doesn't override it — literal Patch B.5 wording ("expands a
# computed AABB outward by the tolerance on all sides... tolerance_mm:
# float = 1.5"). Kept as a module constant (not pulled from
# ENCLOSURE_SPEC) since Phase B's own patch text hard-codes this
# specific default rather than pointing at the shared config object —
# a real project wanting a different margin passes its own
# `tolerance_mm` through compute_swept_volumes()/
# apply_swept_volume_generation() below rather than this module
# silently reading a value from elsewhere.
DEFAULT_TOLERANCE_MM = 1.5


# ---------------------------------------------------------------------------
# Patch B.3 — cylinder and linear swept-AABB functions.
# ---------------------------------------------------------------------------

def swept_aabb_rotational(part: dict, motion: dict) -> dict:
    """Bounds the full swept cylinder of a `"rotational_continuous"`
    part (a wheel, a continuous-rotation servo/motor shaft — see
    eo/mech_motion.py's own MOTION_TABLE worked examples) — literal
    Patch B.3 wording: "returns a box bounding the full swept
    cylinder."

    The part spins about its own vertical (z) axis, in place, at its
    own current x/y footprint center — `part`'s own "x"/"y"/"w"/"h"
    (its already-placed min-corner footprint, same convention every
    other placement in this tree already uses) locate that center;
    `motion["radius_mm"]` is the cylinder's own swept radius. The
    swept cylinder's own z-extent is the part's own unchanged "z"/"d"
    (a spinning wheel doesn't grow or shrink in height by spinning),
    so this is otherwise a plain "circle bounding box" computation: a
    square of side `2 * radius_mm` centered on the part's own
    footprint center.

    Missing "x"/"y"/"w"/"h"/"z"/"d" on `part`, or a missing/non-numeric
    "radius_mm" on `motion`, default to 0 — same "tolerant of a partial
    dict, never raises" posture every pure function in this tree
    already holds itself to (see e.g.
    eo/mech_manufacturability.py's own check_standoff_wall_clearance()
    docstring).

    Pure function: never mutates `part` or `motion`.
    """
    part = part or {}
    motion = motion or {}

    center_x = float(part.get("x") or 0) + float(part.get("w") or 0) / 2.0
    center_y = float(part.get("y") or 0) + float(part.get("h") or 0) / 2.0
    radius = float(motion.get("radius_mm") or 0)

    return {
        "x": round(center_x - radius, 3),
        "y": round(center_y - radius, 3),
        "z": float(part.get("z") or 0),
        "w": round(radius * 2, 3),
        "h": round(radius * 2, 3),
        "d": float(part.get("d") or 0),
    }


def swept_aabb_linear(part: dict, motion: dict) -> dict:
    """Bounds a `"linear"` part's own travel (a linear actuator's rod,
    a slide-out drawer — see eo/mech_motion.py's own MOTION_TABLE
    worked example) — literal Patch B.3 wording: "part's own footprint
    extruded along travel_mm on the declared axis."

    Starts from `part`'s own already-placed footprint box (its current
    "x"/"y"/"z"/"w"/"h"/"d", unchanged on every axis except the one
    declared) and extends ONLY the size component of the declared
    `motion["axis"]` ("x"->"w", "y"->"h", "z"->"d") outward by
    `motion["travel_mm"]`, from the part's own CURRENT position as the
    retracted end of travel — same "the part's already-placed position
    is one real end of its own motion range, not an arbitrary
    reference point" assumption eo/mech_motion.py's own worked
    "linear actuator" entry already implies by only carrying a single
    scalar `travel_mm`, not a `[min, max]` pair.

    An unrecognized/missing `motion["axis"]` (not one of "x"/"y"/"z")
    returns `part`'s own unchanged footprint box (zero-length
    extrusion) rather than raising or guessing an axis — same
    "tolerant of malformed input, never raises" posture this whole
    module holds itself to.

    Pure function: never mutates `part` or `motion`.
    """
    part = part or {}
    motion = motion or {}

    box = {
        "x": float(part.get("x") or 0),
        "y": float(part.get("y") or 0),
        "z": float(part.get("z") or 0),
        "w": float(part.get("w") or 0),
        "h": float(part.get("h") or 0),
        "d": float(part.get("d") or 0),
    }

    dim_key = {"x": "w", "y": "h", "z": "d"}.get(motion.get("axis"))
    if dim_key is not None:
        travel = float(motion.get("travel_mm") or 0)
        box[dim_key] = round(box[dim_key] + travel, 3)

    return box


# ---------------------------------------------------------------------------
# Patch B.4 — arc swept-AABB with cardinal-angle correction.
# ---------------------------------------------------------------------------

# The four cardinal directions swept_aabb_arc() below checks
# `range_deg` against — literal Patch B.4 wording ("any 0°/90°/180°/
# 270°-equivalent angle that falls inside range_deg"). "-equivalent"
# is handled by _cardinal_crossings() checking a small window of
# +/-360-degree-shifted copies of each value below, not by this tuple
# itself carrying every possible equivalent form.
_CARDINAL_ANGLES_DEG = (0, 90, 180, 270)

# How many extra +/-360-degree turns _cardinal_crossings() checks each
# cardinal angle at, beyond its own literal 0-270 value. A single part's
# own declared swing (eo/mech_motion.py's own MOTION_TABLE worked
# examples top out at 180 degrees) never plausibly spans more than one
# full turn, so +/-1 is generous headroom for a `range_deg` written with
# a negative start (this patch's own worked example, -45->45) or a
# start/end pair that isn't pre-normalized into 0-360 by whatever
# produced it (B.1's curated table, or B.2's LLM fallback), without
# scanning an unbounded range for a case that can't occur in practice.
_CARDINAL_WRAP_RANGE = (-1, 0, 1)


def _cardinal_crossings(low_deg: float, high_deg: float) -> list:
    """Every cardinal angle (see `_CARDINAL_ANGLES_DEG` above, at any
    +/-360-degree-shifted equivalent within `_CARDINAL_WRAP_RANGE`)
    that falls inside the inclusive `[low_deg, high_deg]` window —
    shared helper for swept_aabb_arc() below, split out only so its own
    "which cardinals are inside this range" logic is independently
    readable/testable from the point-generation it feeds.

    Deduplicated and sorted for a stable, deterministic output order —
    same "never let ambiguous/degenerate input make the result
    non-deterministic" posture eo/mech_cutouts.py's own `_FACE_ORDER`
    tie-break already documents for itself one module over.
    """
    found = set()
    for cardinal in _CARDINAL_ANGLES_DEG:
        for turn in _CARDINAL_WRAP_RANGE:
            candidate = cardinal + 360 * turn
            if low_deg <= candidate <= high_deg:
                found.add(candidate)
    return sorted(found)


def swept_aabb_arc(part: dict, motion: dict) -> dict:
    """Bounds a `"rotational_arc"` part's own swing (a standard hobby
    servo horn, a hinged lid — see eo/mech_motion.py's own MOTION_TABLE
    worked examples) — literal Patch B.4 wording: "Compute candidate
    extreme points at the arc's start angle, end angle, and any
    0°/90°/180°/270°-equivalent angle that falls inside range_deg...
    Take min/max x/y across the candidate set (at most 6 points),
    return that as the AABB."

    Candidate points are each `arm_length_mm` away from the part's own
    footprint center (`part`'s own "x"/"y"/"w"/"h" min-corner
    footprint, same center convention swept_aabb_rotational() above
    already uses), at each candidate ANGLE — the two endpoints of
    `motion["range_deg"]` plus every cardinal crossing
    `_cardinal_crossings()` above finds strictly from the geometry, not
    guessed. This deliberately does NOT also include the pivot/center
    point itself as a seventh candidate: the servo/hinge BODY sitting
    at that center is already its own separate, already-placed static
    footprint box, checked by the ordinary (non-swept) collision path
    elsewhere in this pipeline — this function's own job is only to
    bound the EXTRA region the moving arm/horn sweeps beyond that
    already-covered body, matching this whole module's own "cheap
    bounding-volume approximation, not precise sector/arc geometry"
    framing at the top of this file.

    Why the cardinal-crossing correction matters (this patch's own
    "why this is its own patch" note, restated concretely): for
    `range_deg = [-45, 45]` (crossing 0°) with `arm_length_mm = 25`,
    the two ENDPOINT-only candidates land at
    (25*cos(-45), 25*sin(-45)) ~ (17.7, -17.7) and (17.7, 17.7) — an
    endpoint-only box's own x-max would be ~17.7. But the arc's TRUE
    x-max, at 0° exactly (25*cos(0), 25*sin(0)) = (25, 0), is larger —
    a naive two-endpoint box would under-size the swept region there
    and could let generated geometry clip the arm mid-swing at 0°, per
    this patch's own stated risk. The 0°-crossing candidate this
    function adds catches exactly that case.

    Returns the same {"x","y","z","w","h","d"} shape every other
    function in this module returns, with "z"/"d" carried through
    unchanged from `part` (the arm swings in the part's own horizontal
    x/y plane at a fixed height, same z-invariance
    swept_aabb_rotational() above already assumes for a spinning
    wheel).

    Missing/malformed "range_deg" (not a 2-element numeric list) or a
    missing "arm_length_mm" default to a single degenerate candidate at
    0mm/0deg — degrades to a zero-size box centered on the part's own
    footprint center rather than raising, same "tolerant of a partial
    dict" posture this whole module holds itself to.

    Pure function: never mutates `part` or `motion`.
    """
    part = part or {}
    motion = motion or {}

    center_x = float(part.get("x") or 0) + float(part.get("w") or 0) / 2.0
    center_y = float(part.get("y") or 0) + float(part.get("h") or 0) / 2.0
    arm_length = float(motion.get("arm_length_mm") or 0)

    range_deg = motion.get("range_deg")
    if (isinstance(range_deg, (list, tuple)) and len(range_deg) == 2
            and all(isinstance(v, (int, float)) for v in range_deg)):
        start_deg, end_deg = float(range_deg[0]), float(range_deg[1])
    else:
        start_deg = end_deg = 0.0

    low_deg, high_deg = min(start_deg, end_deg), max(start_deg, end_deg)
    candidate_degs = {start_deg, end_deg}
    candidate_degs.update(_cardinal_crossings(low_deg, high_deg))

    xs, ys = [], []
    for deg in candidate_degs:
        rad = math.radians(deg)
        xs.append(center_x + arm_length * math.cos(rad))
        ys.append(center_y + arm_length * math.sin(rad))

    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)

    return {
        "x": round(min_x, 3),
        "y": round(min_y, 3),
        "z": float(part.get("z") or 0),
        "w": round(max_x - min_x, 3),
        "h": round(max_y - min_y, 3),
        "d": float(part.get("d") or 0),
    }


# ---------------------------------------------------------------------------
# Patch B.5 — tolerance expansion + exclusion tagging.
# ---------------------------------------------------------------------------

def apply_tolerance(aabb: dict, tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> dict:
    """Expands `aabb` (any of swept_aabb_rotational()/
    swept_aabb_linear()/swept_aabb_arc()'s own {"x","y","z","w","h","d"}
    output, B.3/B.4 above) outward by `tolerance_mm` on every side, on
    all three axes, and tags the result `"shape_kind": "exclusion"` —
    literal Patch B.5 wording, applied as a SEPARATE step after the raw
    shape computation, mirroring how ENCLOSURE_SPEC["clearance_mm"] is
    applied after the raw device footprint elsewhere in this pipeline
    (eo/enclosure_spec.py's own docstring for that key), keeping
    geometry math and safety margin independently tunable.

    All three axes (not just the in-plane x/y this pipeline's other
    2D-plan-view checks mostly work in) are expanded, since a swept
    volume is a real 3D safety margin around a moving part, not just a
    plan-view outline — a low tolerance on z/d could otherwise let a
    housing floor or lid sit right against a part's own moving
    envelope with no real clearance at all.

    Returns a NEW dict (never mutates `aabb`), same "never hand a
    caller a live reference into shared/reused data" caution
    eo/mech_motion.py's own lookup_motion() already documents for
    itself — a caller batching many exclusions (Patch B.6's
    compute_swept_volumes() below) can safely reuse one raw `aabb`
    dict as a template without this function's own output aliasing it.

    Missing/non-numeric position or size fields on `aabb` default to 0,
    same "tolerant of a partial dict" posture this whole module holds
    itself to.
    """
    aabb = aabb or {}
    expanded = dict(aabb)

    for pos_key, size_key in (("x", "w"), ("y", "h"), ("z", "d")):
        pos = float(aabb.get(pos_key) or 0)
        size = float(aabb.get(size_key) or 0)
        expanded[pos_key] = round(pos - tolerance_mm, 3)
        expanded[size_key] = round(size + 2 * tolerance_mm, 3)

    expanded["shape_kind"] = "exclusion"
    return expanded


def is_exclusion(entry: dict) -> bool:
    """Shared `"shape_kind": "exclusion"` predicate — the one guard
    every leakage-prevention check Patch B.6 asks for (eo/mech_cutouts.py's
    own part-iteration guard below, and any future STL/geometry-export
    step) should call, so "what counts as an exclusion entry" is
    defined in exactly one place rather than each caller re-checking
    the raw dict key itself. NOT currently used to filter
    `mech["placements"]` (exclusions are never written there — see this
    module's own top docstring on why) — it exists as the same kind of
    defensive, explicitly-named guard eo/mech_cutouts.py's own Patch
    5.x category/keyword gates already are: cheap, always-correct-to-
    call insurance against a FUTURE change (a refactor that starts
    merging `mech["exclusions"]` into `mech["placements"]`, or a new
    STL/geometry-export module reading straight off `mech["placements"]`
    without knowing this convention) silently starting to treat a
    swept-volume exclusion box as a real, printable part. There is no
    STL export module anywhere in this tree yet (confirmed against the
    checked-out commit this guide is audited against) — this function
    is the guard Patch B.6's own text asks be added "in... the STL
    export path" once that module exists; until then it has exactly
    one real caller (eo/mech_cutouts.py's apply_cutout_generation()
    below).

    Never raises: any non-dict `entry` (including `None`) is simply not
    an exclusion.
    """
    return isinstance(entry, dict) and entry.get("shape_kind") == "exclusion"


# ---------------------------------------------------------------------------
# Patch B.6 wiring glue — NOT one of B.6's own listed files in the
# guide (see this module's own top docstring for why it lives here
# anyway): reads `mech["placements"]` + Patch B.1/B.2's motion lookup,
# dispatches to the right B.3/B.4 shape function, tolerance-expands via
# B.5, and stashes the result on the new `mech["exclusions"]` key so
# eo/mech_manufacturability.py's own B.6 changes (check_feature_collisions())
# have real data to check against.
# ---------------------------------------------------------------------------

def _joined_motion_members(mech: dict, parts: list) -> list:
    """Resolves every placement across EVERY section of `mech` into
    member dicts, joined with its BOM part's own "generic_name" by
    matching member["part_id"] against part["id"] — same two-hop
    section->subsection->member resolution and "gate on the data, not
    a hardcoded section list" posture every other `_joined_*()` helper
    in this tree already establishes (eo/mech_supports.py's own
    `_joined_section_members()`, eo/mech_cutouts.py's own
    `_joined_cutout_members()`) — kept as this module's OWN copy of
    that pattern rather than importing either sibling's private helper,
    same "each module owns its own join" precedent those two modules
    already set relative to each other.

    Returns a NEW list of shallow-copied member dicts — never mutates
    `mech["placements"]` or `parts`.
    """
    parts_by_id = {
        p.get("id"): p for p in (parts or []) if isinstance(p, dict) and p.get("id")
    }

    joined = []
    for section in (mech or {}).get("sections") or []:
        if not isinstance(section, dict):
            continue
        for subsection in subsections_for_section(mech, section):
            for member in members_for_subsection(mech, subsection):
                if not isinstance(member, dict):
                    continue
                part = parts_by_id.get(member.get("part_id"))
                merged = dict(member)
                if isinstance(part, dict):
                    merged["generic_name"] = part.get("generic_name")
                joined.append(merged)
    return joined


# Motion type -> which of B.3/B.4's own shape functions handles it.
# Deliberately a plain dispatch dict (not an if/elif chain) so adding a
# future fourth motion family is a one-line addition here, matching
# this codebase's own preference for small dispatch tables over
# growing conditionals elsewhere (e.g. eo/mech_cutouts.py's own
# CUTOUT_TABLE-driven dispatch).
_SHAPE_FN_BY_MOTION_TYPE = {
    "rotational_continuous": swept_aabb_rotational,
    "rotational_arc": swept_aabb_arc,
    "linear": swept_aabb_linear,
}


def compute_swept_volumes(mech: dict, parts: list,
                           tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> list:
    """Pure function: for every placed member with a resolvable Patch
    B.1 curated motion entry (`eo/mech_motion.py`'s own
    `lookup_motion(generic_name)`), computes its B.3/B.4 swept AABB and
    B.5 tolerance-expands + exclusion-tags it, returning the flat list
    Patch B.6's own wiring (apply_swept_volume_generation() below)
    stashes on `mech["exclusions"]`.

    Deliberately calls ONLY `lookup_motion()` (B.1's free, deterministic
    table lookup) here, NOT `estimate_motion()` (B.2's LLM fallback) —
    kept out of THIS patch's own scope on purpose, so wiring an actual
    LLM call into the main generate/validate/repair pipeline is a
    later, separately-reviewable decision alongside Phase B's own B.7
    tests, not folded silently into B.3-B.6's own geometry-only scope.
    A part with no curated MOTION_TABLE entry today simply gets no
    exclusion box yet, same "most parts are static, and that should be
    the default outcome" posture eo/mech_motion.py's own
    estimate_motion() docstring already establishes for the LLM path
    itself.

    A member whose motion `"type"` isn't one of B.3/B.4's own three
    recognized shapes is skipped (should not happen — `lookup_motion()`
    only ever returns one of those three — but never assumed).

    Returns `[]` (never raises) for a `mech` with no sections yet, same
    "nothing to derive from yet is a no-op" posture every other
    apply_*() generator in this tree already holds toward its own
    missing inputs.

    Pure function: never mutates `mech` or `parts`.
    """
    volumes = []
    if not isinstance(mech, dict) or not mech.get("sections"):
        return volumes

    for member in _joined_motion_members(mech, parts):
        motion = lookup_motion(member.get("generic_name"))
        if motion is None:
            continue

        shape_fn = _SHAPE_FN_BY_MOTION_TYPE.get(motion.get("type"))
        if shape_fn is None:
            continue

        raw_aabb = shape_fn(member, motion)
        exclusion = apply_tolerance(raw_aabb, tolerance_mm=tolerance_mm)
        exclusion["part_id"] = member.get("part_id")
        volumes.append(exclusion)

    return volumes


def apply_swept_volume_generation(mech: dict, parts: list) -> list:
    """Convenience wrapper matching this pipeline's usual mutate-in-place
    call shape (every other apply_*() generator in this tree — e.g.
    eo/mech_supports.py's own apply_supports_generation(), eo/
    mech_cutouts.py's own apply_cutout_generation() — works this way):
    computes compute_swept_volumes(mech, parts) and stashes it on the
    new `mech["exclusions"]` key, still returning the same list for a
    caller (tests, or a future top-level driver) that wants the
    pure-function shape.

    Pipeline call site (agents/hardware_speccer.py's own G3g chain):
    runs AFTER apply_supports_generation() and BEFORE
    apply_cutout_generation() — same "act only on placements the
    validator has already signed off on" ordering
    apply_supports_generation() itself documents for its own position
    relative to run_level_3_4_repair(), plus this function's own
    output needing to already exist on `mech["exclusions"]` before
    eo/mech_manufacturability.py's own build_manufacturability_report()
    runs last in that same chain.

    Returns `[]` (and stashes that same empty result onto
    `mech["exclusions"]`) for a `mech` with no sections yet, same
    no-op posture compute_swept_volumes() itself already holds.
    """
    volumes = compute_swept_volumes(mech, parts)
    if isinstance(mech, dict):
        mech["exclusions"] = volumes
    return volumes
