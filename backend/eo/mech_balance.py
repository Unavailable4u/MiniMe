"""
eo/mech_balance.py — Mech View standalone implementation guide, Phase
C, Patch C.3: turns Patch C.1/C.2's per-part mass values
(eo/mech_mass.py) plus each part's already-placed position into a
mass-weighted center-of-gravity, and separates out the ground-contact
footprint a mobile device would actually balance on — Part 1's own
gap #3 ("No mass or center-of-gravity modeling... nothing checks
whether a mobile device (wheeled/legged) would actually balance").

Two pure functions, both walking the SAME section->subsection->member
resolution every other Phase A/B module in this tree already
establishes for reading `mech["placements"]` (eo/mech_supports.py's
own `_joined_section_members()`, eo/mech_cutouts.py's own
`_joined_cutout_members()`, eo/mech_swept_volume.py's own
`_joined_motion_members()`):

  - compute_cog(mech, parts): mass-weighted centroid of every placed
    part's own footprint center.
  - compute_support_polygon(mech, parts): the 2D (x/y plan-view)
    convex hull of every ground-contact part's own footprint center —
    literal Patch C.4 wording ("convex hull of ground-contact points —
    wheel or leg positions"), landed here (not in Patch C.4's own
    listed files) since it shares the identical join/mass-vocabulary
    machinery compute_cog() above already needs, same "each module
    owns its own join" precedent eo/mech_swept_volume.py's own top
    docstring already sets for itself relative to its sibling modules.

Signature note (deviation from the guide's own literal wording): the
guide's own Patch C.3 text states `compute_cog(mech: dict) -> dict`
with no `parts` argument. In practice, resolving "using C.1/C.2's
mass values" (eo/mech_mass.py's own `lookup_mass()`, keyed on a part's
canonical `generic_name`) needs a BOM to join placements against —
`mech["placements"]` alone never carries `generic_name` (see
eo/mech_swept_volume.py's own `_joined_motion_members()` docstring,
which resolves the identical need the identical way). So both
functions here take `(mech, parts)`, matching the `(mech, parts)`
call shape every other Phase A/B join-needing function in this tree
already uses (compute_swept_volumes(), apply_cutout_generation(),
apply_supports_generation()) rather than the guide's own shorthand.

Deliberately calls ONLY eo/mech_mass.py's own `lookup_mass()` (C.1's
free, deterministic table lookup) here, NEVER `estimate_mass()` (C.2's
LLM fallback) — same "keep the pure geometry function pure, wire an
actual LLM call in as a later, separately-reviewable decision" posture
eo/mech_swept_volume.py's own `compute_swept_volumes()` already
documents for itself toward `estimate_motion()`. A part with no
curated MASS_TABLE entry still needs SOME mass value to not silently
vanish from the centroid (unlike a Phase B part with no motion entry,
which correctly contributes nothing), so it falls back to
`_DEFAULT_UNKNOWN_MASS_G` below — a fixed placeholder value, not a
live LLM estimate, kept a deliberately separate literal from
eo/mech_mass.py's own `estimate_mass()` placeholder (even though both
happen to equal 5.0g today) so this module's own purity never
accidentally depends on that other module's own internal fallback
value changing.
"""
import math

from eo.mech_mass import lookup_mass
from eo.mech_sections import subsections_for_section
from eo.mech_subsections import members_for_subsection

# Fallback mass (grams) for a placed part whose `generic_name` has no
# eo/mech_mass.py MASS_TABLE entry — deliberately the same numeric
# value as that module's own estimate_mass() placeholder (see this
# module's own top docstring for why it's still kept a SEPARATE
# literal rather than an import of that value), a small, clearly-
# labeled "unknown, don't let this part vanish from the centroid"
# stand-in, not a claim of real accuracy for any specific part.
_DEFAULT_UNKNOWN_MASS_G = 5.0

# Keywords matched as a normalized substring of a placed part's own
# `generic_name` to decide whether it's a ground-contact part for
# compute_support_polygon() below — same "curated keyword substring
# scan, not a dedicated schema field" posture eo/mech_cutouts.py's own
# CUTOUT_TABLE keyword scan already establishes for deciding which
# parts get which cutout, since no part in this codebase carries an
# explicit "touches the ground" field anywhere yet. "wheel" alone
# already covers both eo/mech_motion.py's own MOTION_TABLE "wheel" and
# "caster wheel" entries (substring match); "leg"/"foot" are included
# for the guide's own "legged" mobility_type even though no leg-type
# part exists in any curated table yet today — same "the vocabulary
# gets richer later, the matching mechanism doesn't need to change
# when it does" reasoning CUTOUT_TABLE's own keyword set already
# relies on for its own future growth.
GROUND_CONTACT_KEYWORDS = ("wheel", "leg", "foot")


def _normalize(text: str) -> str:
    """Case/whitespace-insensitive matching key — same normalization
    eo/mech_mass.py's own _normalize() (and eo/mech_motion.py's own,
    before it) already uses for the identical reason.
    """
    return " ".join((text or "").strip().lower().split())


def _joined_mass_members(mech: dict, parts: list) -> list:
    """Resolves every placement across EVERY section of `mech` into
    member dicts, joined with its BOM part's own `generic_name` --
    same two-hop section->subsection->member resolution and "gate on
    the data, not a hardcoded section list" posture every other
    `_joined_*()` helper in this tree already establishes (see this
    module's own top docstring for the specific siblings this mirrors).

    Returns a NEW list of shallow-copied member dicts -- never mutates
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


def _footprint_center(member: dict) -> tuple:
    """A placed member's own footprint center, (center_x, center_y,
    center_z) -- same "x"/"y"/"z"/"w"/"h"/"d" min-corner convention
    every other placement/footprint helper in this tree already reads
    (eo/mech_swept_volume.py's own swept_aabb_rotational() docstring
    states this convention explicitly). Missing fields default to 0,
    same "tolerant of a partial dict, never raises" posture this whole
    tree holds itself to.
    """
    x = float(member.get("x") or 0) + float(member.get("w") or 0) / 2.0
    y = float(member.get("y") or 0) + float(member.get("h") or 0) / 2.0
    z = float(member.get("z") or 0) + float(member.get("d") or 0) / 2.0
    return (x, y, z)


# ---------------------------------------------------------------------------
# Patch C.3 — center-of-gravity computation.
# ---------------------------------------------------------------------------

def compute_cog(mech: dict, parts: list) -> dict:
    """Mass-weighted centroid of every placed part's own footprint
    center -- literal Patch C.3 wording: "mass-weighted centroid of
    all placed parts' positions (using C.1/C.2's mass values)."

    Every member resolved by `_joined_mass_members()` above
    contributes: its own mass (eo/mech_mass.py's own `lookup_mass()`
    on a curated hit, `_DEFAULT_UNKNOWN_MASS_G` on a miss -- see this
    module's own top docstring for why a miss still contributes a
    placeholder mass rather than being skipped) times its own
    `_footprint_center()` position, summed and divided by the total
    mass.

    Returns `{"x": float, "y": float, "z": float, "total_mass_g":
    float}`. `total_mass_g` is always the literal sum of every
    contributing part's own mass (real total device weight, not just a
    denominator) -- a caller (Patch C.4's future support-polygon
    check, not this patch) can use `total_mass_g == 0` as the "nothing
    to compute a centroid from yet" signal, since a real placed part
    always contributes a strictly positive mass.

    Returns `{"x": 0.0, "y": 0.0, "z": 0.0, "total_mass_g": 0.0}`
    (never raises) for a `mech` with no sections yet, same "nothing to
    derive from yet is a no-op" posture every other pure function in
    this tree already holds toward its own missing inputs.

    Pure function: never mutates `mech` or `parts`. Never calls
    eo/mech_mass.py's own `estimate_mass()` (LLM fallback) -- see this
    module's own top docstring.
    """
    if not isinstance(mech, dict) or not mech.get("sections"):
        return {"x": 0.0, "y": 0.0, "z": 0.0, "total_mass_g": 0.0}

    weighted_x = weighted_y = weighted_z = 0.0
    total_mass = 0.0

    for member in _joined_mass_members(mech, parts):
        mass_entry = lookup_mass(member.get("generic_name"))
        mass_g = mass_entry["mass_g"] if mass_entry else _DEFAULT_UNKNOWN_MASS_G

        center_x, center_y, center_z = _footprint_center(member)

        weighted_x += mass_g * center_x
        weighted_y += mass_g * center_y
        weighted_z += mass_g * center_z
        total_mass += mass_g

    if total_mass <= 0:
        return {"x": 0.0, "y": 0.0, "z": 0.0, "total_mass_g": 0.0}

    return {
        "x": round(weighted_x / total_mass, 3),
        "y": round(weighted_y / total_mass, 3),
        "z": round(weighted_z / total_mass, 3),
        "total_mass_g": round(total_mass, 3),
    }


# ---------------------------------------------------------------------------
# Support-polygon geometry -- shared with Patch C.4's own gated check
# (eo/mech_validator.py's own future check_balance(), not this patch),
# landed here per this module's own top docstring.
# ---------------------------------------------------------------------------

def _is_ground_contact(generic_name: str) -> bool:
    """True if `generic_name`'s own normalized form contains any of
    `GROUND_CONTACT_KEYWORDS` above as a substring -- same keyword-
    substring-scan posture eo/mech_cutouts.py's own CUTOUT_TABLE-driven
    matching already establishes, just against a small keyword tuple
    instead of a dict of descriptors (there's only one output shape
    here -- "is or isn't a ground-contact point" -- not a table of
    per-keyword cutout parameters to look up).
    """
    if not isinstance(generic_name, str):
        return False
    normalized = _normalize(generic_name)
    if not normalized:
        return False
    return any(keyword in normalized for keyword in GROUND_CONTACT_KEYWORDS)


def _cross(o: tuple, a: tuple, b: tuple) -> float:
    """2D cross product of (a - o) and (b - o) -- standard convex-hull
    turn-direction test (positive = counter-clockwise turn at `a`).
    """
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _convex_hull_2d(points: list) -> list:
    """Andrew's monotone-chain convex hull -- standard O(n log n)
    algorithm, no external geometry library needed (nothing in this
    tree pulls in numpy/scipy -- see eo/mech_swept_volume.py's own
    `math`-only import list for the same "no new dependency for a
    single self-contained computation" precedent). Returns hull points
    in counter-clockwise order, deduplicated.

    `points`: a list of (x, y) tuples. Degenerate input (0, 1, or 2
    distinct points, or every point collinear/identical) returns
    whatever the true hull of that input actually is -- a single point
    for 1 distinct point, both endpoints for 2, etc. -- rather than
    raising; a caller (Patch C.4's future check, not this patch) is
    responsible for deciding whether a degenerate (fewer than 3 point,
    zero-area) hull is itself a violation.
    """
    unique_points = sorted(set(points))
    if len(unique_points) <= 2:
        return unique_points

    lower = []
    for p in unique_points:
        while len(lower) >= 2 and _cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)

    upper = []
    for p in reversed(unique_points):
        while len(upper) >= 2 and _cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)

    return lower[:-1] + upper[:-1]


def compute_support_polygon(mech: dict, parts: list) -> list:
    """The 2D (x/y plan-view) convex hull of every ground-contact
    part's own footprint center -- literal Patch C.4 wording: "convex
    hull of ground-contact points -- wheel or leg positions." Landed
    in this module (not eo/mech_validator.py, Patch C.4's own only
    listed file for the check itself) since it needs the identical
    `_joined_mass_members()`/`_footprint_center()` machinery
    compute_cog() above already owns -- see this module's own top
    docstring.

    A member counts as "ground-contact" per `_is_ground_contact()`
    above -- its own `generic_name` matching `GROUND_CONTACT_KEYWORDS`
    -- regardless of section/category, since a wheel or leg could in
    principle be grouped under any section depending on how
    eo/mech_sections.py's own category->section table classifies its
    BOM category.

    Returns `[{"x": float, "y": float}, ...]`, the hull points in
    counter-clockwise order -- `[]` for a `mech` with no sections yet
    or with zero ground-contact members found, same "nothing to derive
    from yet is a no-op" posture compute_cog() above already holds.

    Pure function: never mutates `mech` or `parts`.
    """
    if not isinstance(mech, dict) or not mech.get("sections"):
        return []

    ground_points = []
    for member in _joined_mass_members(mech, parts):
        if not _is_ground_contact(member.get("generic_name")):
            continue
        center_x, center_y, _center_z = _footprint_center(member)
        ground_points.append((round(center_x, 3), round(center_y, 3)))

    if not ground_points:
        return []

    hull = _convex_hull_2d(ground_points)
    return [{"x": x, "y": y} for x, y in hull]
