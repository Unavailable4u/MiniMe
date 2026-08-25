"""
eo/mech_enclosure.py — Phase 1, Patch 1.2 of the Mech/Enclosure
implementation guide: the pure, deterministic housing-sizing function
that replaces agents/hardware_speccer.py's current guessed housing/lid
dimensions ("The housing's placement should span the full enclosure
footprint... the lid matches the housing -- same x/y footprint",
SYSTEM_PROMPT) with a computed value derived from what's actually
packed inside the device.

Same build-order reasoning every earlier "pure function first" patch
in this tree already established (eo/mech_device.py's own
plan_device_layout() before apply_device_merge(), eo/mech_sections.py's
group_into_sections() before its *_pool.py sibling): land the
mechanical, side-effect-free sizing logic on its own, testable with
plain dict inputs, before Patch 1.3 wires it into the pipeline's
mutate-in-place convention and Patch 1.4 stops the LLM from being asked
to size the enclosure at all.

Input: `device_footprint`, the same shape eo/mech_device.py's
plan_device_layout() already returns as its own `footprint` key --
{"x","y","z","w","h","d"}, the union bounding box of every zoned
section's post-translation footprint (see that module's own
docstring). This module never computes that bounding box itself; it
only takes it as ground truth and expands it, same "reads a footprint,
doesn't invent one" boundary eo/mech_device.py's own docstring draws
around the Enclosure section's footprint.

Sizing rule (Master Guide's own worked description, Phase 1 "Design"
section):
    housing_outer = device_footprint expanded by (wall_thickness +
                    clearance) on x/y/z
    housing_inner = device_footprint expanded by clearance only
    lid           = same x/y footprint as housing, z = housing's own d

"Expanded by N" means padded N on every side: the returned box's
x/y/z origin moves back by N and its w/h/d each grow by 2*N, so the
device_footprint stays exactly centered inside the result -- same
"pad symmetrically around a fixed footprint" shape eo/mech_validator.py's
own confidence-aware tolerance buffer already uses one level down, just
applied to a whole housing instead of one part.

`inner` is the cavity's own footprint (device_footprint plus
clearance_mm of air on every side, per ENCLOSURE_SPEC) -- what a part
placed anywhere inside it is guaranteed clear of the housing's inner
wall face. `outer` is `inner` plus another wall_thickness_mm of actual
shell material -- the real print boundary. `lid` shares outer's x/y/w/h
(same footprint in plan view, per the Master Guide's own phrasing) and
sits on top of it: lid.z = outer.z + outer.d, a slightly more literal
reading of "z = housing's own d" than a bare offset would give, since
it still stacks the lid correctly even when device_footprint's own z
isn't 0 (matching eo/mech_device.py's own docstring: "h", not "d", is
the in-plane axis -- "d" is left as the vertical stacking axis the lid
sits on top of via its own z). lid.d (the lid's own shell thickness) is
wall_thickness_mm -- the same shell thickness as the housing walls,
absent a separate ENCLOSURE_SPEC field for it.

Deliberately does NOT read eo/mech_device.py, eo/mech_sections.py, or
`mech` at all -- a pure dict-in/dict-out function, no mutation, no I/O,
same "this package never imports agents/, and a -1-suffix module never
reaches past its own inputs" precedent every earlier pure-planning
function in this tree already holds itself to. Patch 1.3's
apply_enclosure_generation() is what actually reads `mech["device"]`'s
footprint and calls this.
"""

from eo.enclosure_spec import DEFAULT_MATERIAL, ENCLOSURE_SPEC, MATERIAL_PROPERTIES
from eo.mech_material import resolve_material
from eo.mech_sections import subsections_for_section
from eo.mech_subsections import members_for_subsection


def _expand(footprint: dict, pad: float) -> dict:
    """Pads `footprint` by `pad` on every side of x/y/z -- see module
    docstring's "Expanded by N" note. Internal helper only; `inner` and
    `outer` are both this same operation at two different pad amounts,
    so the padding math itself lives in exactly one place.
    """
    x = float(footprint.get("x") or 0)
    y = float(footprint.get("y") or 0)
    z = float(footprint.get("z") or 0)
    w = float(footprint.get("w") or 0)
    h = float(footprint.get("h") or 0)
    d = float(footprint.get("d") or 0)
    return {
        "x": round(x - pad, 3), "y": round(y - pad, 3), "z": round(z - pad, 3),
        "w": round(w + 2 * pad, 3), "h": round(h + 2 * pad, 3), "d": round(d + 2 * pad, 3),
    }


def compute_housing_footprint(device_footprint: dict, material: str = DEFAULT_MATERIAL) -> dict:
    """Returns {"outer": {...}, "inner": {...}, "lid": {...}}, each an
    {"x","y","z","w","h","d"} dict -- see module docstring for the
    sizing rule and the reasoning behind each of the three.

    Missing x/y/z/w/h/d keys on `device_footprint` default to 0 (same
    "tolerant of a partial dict" posture eo/mech_validator.py's own
    _build_payload() already takes toward missing dims), so this never
    raises on a still-incomplete footprint -- it just returns a
    correspondingly degenerate (but still well-shaped) result.

    Patch E.3 (Phase E, "Material awareness"): `material` (optional,
    defaults to `DEFAULT_MATERIAL` -- "pla_rigid") selects which
    Patch E.1 `MATERIAL_PROPERTIES` entry's own overrides this shell is
    sized from. `wall_thickness_mm` is read via that material's own
    partial-override dict FIRST, falling through to
    `ENCLOSURE_SPEC["wall_thickness_mm"]` for any key the material
    doesn't override -- literal "Patch E.3's material-aware lookup
    falls through to ENCLOSURE_SPEC's own baseline value" wording
    `eo/enclosure_spec.py`'s own `MATERIAL_PROPERTIES` docstring already
    documents for this exact call site. `pla_rigid`'s own entry is an
    empty override dict, so the default call (no `material` argument,
    same as every pre-E.3 caller) is numerically byte-for-byte
    unchanged from before this patch -- only a caller that explicitly
    passes a non-default material (e.g. `"tpu_flexible"`) ever sees a
    different wall thickness out of this function.

    Pure function: never mutates `device_footprint`, never touches
    `mech`, never does I/O. Two calls with the same input always
    return the same output -- the "idempotent by construction" property
    Patch 1.5's own idempotency test checks for at the pipeline level
    depends on this holding true here first.
    """
    overrides = MATERIAL_PROPERTIES.get(material) or {}
    clearance = ENCLOSURE_SPEC["clearance_mm"]
    wall = overrides.get("wall_thickness_mm", ENCLOSURE_SPEC["wall_thickness_mm"])

    inner = _expand(device_footprint, clearance)
    outer = _expand(device_footprint, wall + clearance)

    lid = {
        "x": outer["x"], "y": outer["y"], "w": outer["w"], "h": outer["h"],
        "z": round(outer["z"] + outer["d"], 3),
        "d": wall,
    }

    return {"outer": outer, "inner": inner, "lid": lid}


# ---------------------------------------------------------------------------
# Patch A.5 (Mech View standalone implementation guide, Phase A): the
# `partial`-mode sibling of compute_housing_footprint() above -- a
# wheeled/legged/flying device (Part 1, gap #1) gets a structural
# baseplate its subsystems mount to, never a sealed housing/lid pair.
# ---------------------------------------------------------------------------


def compute_baseplate_footprint(device_footprint: dict, material: str = DEFAULT_MATERIAL) -> dict:
    """Returns {"outer": {...}}, an {"x","y","z","w","h","d"} dict --
    the `partial`-mode analog of compute_housing_footprint()'s own
    "outer" box, sized for a single flat structural plate rather than a
    shell that fully encloses the device.

    Unlike the `full`-mode housing, an open frame has no wall to expand
    x/y/z by -- there's no shell material to leave room for, only the
    same `clearance_mm` dimensional-tolerance margin
    compute_housing_footprint()'s own "inner" cavity already pads by
    (Part 1's own "no dead space beyond wall_thickness + clearance"
    reasoning applies here minus the wall_thickness term, since a
    baseplate never gets a matching lid). The plate's own thickness
    (its "d") is `wall_thickness_mm` -- the same shell-thickness number
    every other structural part in this pipeline already resolves
    through ENCLOSURE_SPEC, reused here rather than inventing a second
    "how thick is 3D-printed structural plastic" constant.

    The plate sits directly beneath `device_footprint` (its own z is
    `device_footprint`'s z minus the plate's own thickness, so parts
    are placed resting on top of it, not overlapping it) and is padded
    by `clearance_mm` in x/y only -- z is left unpadded since there is
    no ceiling to clear, only a mounting surface.

    Missing x/y/z/w/h/d keys on `device_footprint` default to 0, same
    tolerant-of-a-partial-dict posture compute_housing_footprint()
    already takes toward its own input.

    Patch E.3: `material` (optional, defaults to `DEFAULT_MATERIAL`)
    is resolved the same "material's own override, falling through to
    ENCLOSURE_SPEC's own baseline" way compute_housing_footprint()'s
    own `material` parameter already is -- see that function's own
    docstring for the full reasoning; a default/omitted call is
    numerically unchanged from before this patch.

    Pure function: no I/O, no LLM call, no mutation of
    `device_footprint` -- same purity guarantee every other function in
    this module already holds itself to.
    """
    overrides = MATERIAL_PROPERTIES.get(material) or {}
    clearance = ENCLOSURE_SPEC["clearance_mm"]
    wall = overrides.get("wall_thickness_mm", ENCLOSURE_SPEC["wall_thickness_mm"])

    x = float(device_footprint.get("x") or 0)
    y = float(device_footprint.get("y") or 0)
    z = float(device_footprint.get("z") or 0)
    w = float(device_footprint.get("w") or 0)
    h = float(device_footprint.get("h") or 0)

    outer = {
        "x": round(x - clearance, 3),
        "y": round(y - clearance, 3),
        "z": round(z - wall, 3),
        "w": round(w + 2 * clearance, 3),
        "h": round(h + 2 * clearance, 3),
        "d": wall,
    }

    return {"outer": outer}


# ---------------------------------------------------------------------------
# Patch 1.3 -- pipeline-integration half of Phase 1.
# ---------------------------------------------------------------------------
#
# Everything above this line is Patch 1.2's pure function, deliberately
# ignorant of `mech`/`parts`/eo/mech_sections.py -- see the module
# docstring's "Deliberately does NOT read..." paragraph. This half is the
# opposite: it reads `mech["device"]` (Patch 1.5 wires it to run
# immediately after eo/mech_device.py's own apply_device_merge(), which is
# what actually populates that key), calls compute_housing_footprint()
# above, and mutates `mech` in place -- same "mutate AND return" wrapper
# convention apply_device_merge() itself already established.

# A housing/lid placement is matched by `part_id` PREFIX, not literal
# equality -- same "a model-authored id only ever needs to start with the
# right word" convention eo/mech_subsections.py's own MOUNT_ID_PREFIX
# ("mount_") already establishes, for the identical reason: agents/
# hardware_speccer.py's SYSTEM_PROMPT_PARTS shows "housing_1"/"lid_1" as
# its worked example, not a literal contract the model is guaranteed to
# match exactly.
_HOUSING_ID_PREFIX = "housing"
_LID_ID_PREFIX = "lid"

# Patch A.5: the `partial`-mode part id -- matches agents/
# hardware_speccer.py's SYSTEM_PROMPT_PARTS `partial`-mode worked
# example (`"id": "baseplate_1"`, Patch A.4), same prefix-not-literal
# match convention _HOUSING_ID_PREFIX/_LID_ID_PREFIX already use above.
_BASEPLATE_ID_PREFIX = "baseplate"

# The one section every housing/lid placement lives in -- same constant
# eo/mech_device.py's own _CONTAINER_SECTION_ID names, duplicated here
# rather than imported since eo/mech_device.py is a peer, not a
# dependency, of this module (this module still never imports it).
_CONTAINER_SECTION_ID = "Enclosure"


# ---------------------------------------------------------------------------
# Patch E.3: resolves which MATERIAL_PROPERTIES key the pipeline's single
# shared structural part (housing/lid in `full` mode, baseplate in
# `partial` mode) should be sized with, via Patch E.2's own
# resolve_material() -- so this module never hand-rolls a second,
# divergent material-lookup rule of its own.
# ---------------------------------------------------------------------------

def _resolve_structural_material(mech: dict, parts: list, archetype: dict, id_prefix: str) -> str:
    """Finds the structural placement in `mech`'s own Enclosure section
    whose `part_id` starts with `id_prefix` (`_HOUSING_ID_PREFIX` or
    `_BASEPLATE_ID_PREFIX`, same prefix-not-literal match convention
    `_apply_dims()`'s own callers already use below), joins it against
    `parts` by id to get the real `category`/`generic_name`/`aliases`
    fields a placement entry alone never carries, and resolves its
    material via `resolve_material()`.

    Falls through to `DEFAULT_MATERIAL` when the structural part hasn't
    been placed yet, isn't present in `parts`, or `mech`/`parts` is
    missing/malformed -- same "never let a bad/missing upstream field
    produce anything other than today's unchanged default behavior"
    posture every other archetype-reading function in this tree already
    holds itself to. In practice, a housing/baseplate's own
    `generic_name` is never strap/band-flavored, so this resolves to
    `DEFAULT_MATERIAL` for every real project today -- the wiring exists
    so a future material rule (or an unusual, explicitly-authored
    structural part) is honored without a second code path.
    """
    if not isinstance(mech, dict):
        return DEFAULT_MATERIAL

    parts_by_id = {
        p.get("id"): p for p in (parts or []) if isinstance(p, dict) and p.get("id")
    }

    section = next(
        (s for s in (mech.get("sections") or [])
         if isinstance(s, dict) and s.get("section_id") == _CONTAINER_SECTION_ID),
        None,
    )
    if section is None:
        return DEFAULT_MATERIAL

    for subsection in subsections_for_section(mech, section):
        for member in members_for_subsection(mech, subsection):
            if not isinstance(member, dict):
                continue
            part_id = member.get("part_id") or ""
            if part_id.startswith(id_prefix):
                part = parts_by_id.get(part_id)
                return resolve_material(part, archetype) if isinstance(part, dict) else DEFAULT_MATERIAL

    return DEFAULT_MATERIAL


def _apply_dims(placement: dict, dims: dict) -> None:
    """Mutates `placement`'s own x/y/z/w/h/d in place to match `dims`
    (one of compute_housing_footprint()'s own "outer"/"lid" results) --
    same "mutate the SAME dict a caller reading mech["placements"]
    afterward already holds" posture eo/mech_device.py's own
    apply_device_merge() uses for a section's `footprint`, just applied
    to one placement entry instead of a whole section.
    """
    for key in ("x", "y", "z", "w", "h", "d"):
        if key in dims:
            placement[key] = dims[key]


def apply_enclosure_generation(mech: dict, parts: list) -> dict:
    """Patch 1.3: wires compute_housing_footprint() into the pipeline.
    MUST run immediately after eo/mech_device.py's own
    apply_device_merge() (Patch 1.5 wires that ordering into agents/
    hardware_speccer.py's G3g call site) -- reads the `device_footprint`
    apply_device_merge() already computed and stashed on
    `mech["device"]["footprint"]`, never recomputes it itself, same
    "reads a footprint, doesn't invent one" boundary this module's own
    top docstring draws around compute_housing_footprint()'s input.

    Stashes the full {"outer","inner","lid"} result on the new
    `mech["housing"]` key -- same "mutate in place AND still return the
    value" convention every apply_* function in this package already
    follows -- so Phase 2/5/6's later checkers (standoffs, cutouts,
    manufacturability) have the real x/y/z origin boxes to work from,
    not just a size.

    Deliberately does NOT overwrite the existing `mech["enclosure"]` key
    with that same nested result, even though the patch breakdown's own
    shorthand ("stashes result on mech['enclosure']") reads that way at
    a glance: frontend/app/components/MechView.jsx's PartBox/
    isShellPlacement/wireframe-hull code all read `mech["enclosure"]` as
    a FLAT {"w","h","d"} hull size (`enclosure.w`, `enclosure.h`,
    `enclosure.d` -- never a nested lookup), and agents/
    hardware_speccer.py's own _ensure_electrical_placements()/
    _clamp_placements_to_enclosure() read that same flat shape earlier
    in the pipeline. Replacing it with this function's nested dict would
    turn every one of those `enclosure.w` reads into `undefined` and
    silently break the 3D wireframe render -- a regression this patch
    avoids by instead REFRESHING `mech["enclosure"]` in its existing
    flat shape (now sourced from the real computed outer box instead of
    the LLM's stale guess -- safe to overwrite here because both
    _ensure_electrical_placements() and _clamp_placements_to_enclosure()
    already ran and fully consumed that guess earlier in
    run_hardware_speccer(), well before Level 0->1 of the repair tree
    this function is part of even starts) and adding the full breakdown
    under the new, non-colliding `mech["housing"]` key instead.

    Also overwrites the Enclosure section's own housing_1/lid_1
    placement entries in `mech["placements"]` (resolved the same
    two-hop way eo/mech_device.py's own apply_device_merge() already
    resolves section->subsection->member) to match `outer`/`lid`
    respectively -- the exact downstream target Patch 1.4 stops the LLM
    from authoring in the first place, so a placement that's already
    correctly computed here is never subsequently overwritten by
    anything else later in the pipeline.

    Returns None (and stashes None onto both `mech["housing"]` and,
    unlike housing, leaves `mech["enclosure"]` untouched) when
    `mech["device"]` itself is missing or has no `footprint` -- nothing
    to derive a housing from yet, same "nothing to merge yet" no-op
    posture apply_device_merge() already takes.

    Patch A.5 (Mech View standalone implementation guide, Phase A):
    gated on `mech["archetype"]["enclosure_mode"]` (stashed by A.3/A.4,
    read the same "missing reads back as the safe `full` default" way
    agents/hardware_speccer.py's own Call 1 already reads it) --

      - `full` (or archetype absent entirely, e.g. a `mech` dict this
        function's own pre-Phase-A test suite already builds by hand):
        everything below this note is byte-for-byte the pre-Patch-A.5
        behavior -- housing/lid pair, regression-safe per this whole
        guide's own "full mode must not drift" posture (Patch A.4's
        own done-when).
      - `partial`: computes a baseplate via
        compute_baseplate_footprint() instead of a housing/lid pair,
        stashes it on `mech["housing"]` in the SAME `{"outer": ...}`
        shape (just without an "inner"/"lid" key -- a baseplate has
        neither a cavity nor a matching lid), and overwrites a
        baseplate_1-prefixed placement (Patch A.4's own worked
        parts-prompt example) instead of housing_1/lid_1.

    Patch E.3 (Phase E, "Material awareness"): both the `full` and
    `partial` branches now resolve the shared structural part's own
    material via `_resolve_structural_material()` and pass it through
    to `compute_housing_footprint()`/`compute_baseplate_footprint()`,
    so a housing/baseplate whose own BOM entry resolves to a
    non-default material (Patch E.2's `resolve_material()`) is sized
    with that material's own `MATERIAL_PROPERTIES` overrides rather
    than the flat `ENCLOSURE_SPEC` defaults. A housing/baseplate's own
    `generic_name` is never strap/band-flavored in practice, so this
    resolves to `DEFAULT_MATERIAL` for every project today -- this is
    additive wiring, not a change in today's numeric output.
      - `none`: no shared structural part at all (Part 1, gap #1) --
        skips entirely. `mech["housing"]` is stashed None (same
        "device missing" no-op shape above) and any stale
        `mech["enclosure"]` key from an earlier run is removed rather
        than left in place, so a `none`-mode run genuinely produces no
        enclosure output, not just an unrefreshed leftover from before
        the archetype was known.
    """
    archetype = (mech or {}).get("archetype") or {}
    enclosure_mode = archetype.get("enclosure_mode", "full")

    device = (mech or {}).get("device")
    if not isinstance(device, dict) or not isinstance(device.get("footprint"), dict):
        if isinstance(mech, dict):
            mech["housing"] = None
        return None

    if enclosure_mode == "none":
        if isinstance(mech, dict):
            mech["housing"] = None
            mech.pop("enclosure", None)
        return None

    if enclosure_mode == "partial":
        material = _resolve_structural_material(mech, parts, archetype, _BASEPLATE_ID_PREFIX)
        result = compute_baseplate_footprint(device["footprint"], material=material)

        if isinstance(mech, dict):
            mech["housing"] = result
            outer = result["outer"]
            mech["enclosure"] = {"w": outer["w"], "h": outer["h"], "d": outer["d"]}

        section = next(
            (s for s in (mech.get("sections") or [])
             if isinstance(s, dict) and s.get("section_id") == _CONTAINER_SECTION_ID),
            None,
        )
        if section is not None:
            for subsection in subsections_for_section(mech, section):
                for member in members_for_subsection(mech, subsection):
                    if not isinstance(member, dict):
                        continue
                    part_id = member.get("part_id") or ""
                    if part_id.startswith(_BASEPLATE_ID_PREFIX):
                        _apply_dims(member, result["outer"])

        return result

    # `full` (or no archetype recorded yet) -- unchanged from before A.5.
    material = _resolve_structural_material(mech, parts, archetype, _HOUSING_ID_PREFIX)
    result = compute_housing_footprint(device["footprint"], material=material)

    if isinstance(mech, dict):
        mech["housing"] = result
        # See docstring above on why this stays flat rather than nested.
        outer = result["outer"]
        mech["enclosure"] = {"w": outer["w"], "h": outer["h"], "d": outer["d"]}

    section = next(
        (s for s in (mech.get("sections") or [])
         if isinstance(s, dict) and s.get("section_id") == _CONTAINER_SECTION_ID),
        None,
    )
    if section is not None:
        for subsection in subsections_for_section(mech, section):
            for member in members_for_subsection(mech, subsection):
                if not isinstance(member, dict):
                    continue
                part_id = member.get("part_id") or ""
                if part_id.startswith(_HOUSING_ID_PREFIX):
                    _apply_dims(member, result["outer"])
                elif part_id.startswith(_LID_ID_PREFIX):
                    _apply_dims(member, result["lid"])

    return result
