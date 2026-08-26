"""
eo/mech_material.py — Phase E, Patch E.2 of the Mech/Enclosure
implementation guide: the pure, deterministic per-part material resolver
that decides which entry of Patch E.1's eo/enclosure_spec.py
MATERIAL_PROPERTIES a given BOM part actually gets built with, per this
guide's own Phase E scope ("No material property modeling... every
structural part is implicitly one rigid, generic 3D-printable plastic"
-- Part 1, item 6 -- the gap this phase closes).

Same build-order reasoning every earlier "pure function first" patch in
this tree already established (eo/mech_supports.py's own
compute_standoffs() before apply_supports_generation(), eo/
mech_access.py's own generate_hinge()/generate_snap_latch()/
generate_slide() before apply_access_generation()): land the
side-effect-free resolution function here, on its own, testable with
plain dict inputs, before Patch E.3 wires a material-aware lookup into
eo/mech_enclosure.py / eo/mech_cutouts.py and actually changes what
those modules build.

Resolution rule (per this patch's own breakdown): defaults to
DEFAULT_MATERIAL ("pla_rigid") for structural parts generally; resolves
to "tpu_flexible" specifically for strap/band-category parts when
`archetype["mobility_type"] == "wearable"`. This codebase's real BOM
category enum (agents/hardware_speccer.py's own SYSTEM_PROMPT_PARTS
"category" line -- "mcu", "sensor", "actuator", "power", "module",
"3D_PRINT", "MISC") has no dedicated "strap"/"band" category of its
own -- a wearable's strap is BOM'd as a 3D_PRINT part like any other
structural/mechanical piece, distinguished only by what its own
`generic_name`/`aliases` actually say ("Wrist Strap", "Wearable Band"),
not by a category value a strap-vs-non-strap 3D_PRINT part would
otherwise share. This is the identical "the categorical field I have
isn't fine-grained enough" situation eo/enclosure_spec.py's own
CUTOUT_TABLE docstring (Patch 5.1) already documents and solves the
same way agents/hardware_speccer.py's own _SHAPE_TO_PRIMITIVE (G1c)
does -- keyword-matching a part's own `generic_name`/`aliases` free-text
fields rather than trusting the coarse category enum to carry a
distinction it was never designed to make. Category is still used here
as a coarse pre-filter (only "3D_PRINT"/"MISC" -- purely mechanical BOM
lines -- are ever strap-eligible in the first place; an electrical part
is never resolved to tpu_flexible regardless of what its generic_name
happens to contain), same two-stage "cheap category check, then
keyword scan" pattern eo/enclosure_spec.py's own
CUTOUT_ELIGIBLE_CATEGORIES + CUTOUT_TABLE pairing already establishes
for cutouts.

Dependency shape: imports ONLY eo/enclosure_spec.py (Patch E.1, already
a peer module in this same package) -- no import of agents/
hardware_speccer.py or any *_pool.py, same "this package never imports
agents/" precedent eo/mech_sections.py's / eo/mech_access.py's own
module docstrings already establish.

Idempotent / side-effect-free: resolve_material() never mutates `part`
or `archetype` -- same read-only contract every other pure-resolution
function in this package already holds itself to.
"""

import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.enclosure_spec import DEFAULT_MATERIAL, MATERIAL_PROPERTIES

# Coarse pre-filter, same "only these categories are ever eligible in the
# first place" role eo/enclosure_spec.py's own CUTOUT_ELIGIBLE_CATEGORIES
# plays for cutouts (see that constant's own docstring). A strap/band is
# always a purely mechanical, 3D-printed structural part -- never an
# electrical one -- so "sensor"/"actuator"/"power"/"mcu"/"module" are
# never strap-eligible regardless of generic_name/aliases content.
_STRAP_ELIGIBLE_CATEGORIES = {"3D_PRINT", "MISC"}

# Keyword scan target, same lowercase-substring-match convention eo/
# enclosure_spec.py's own CUTOUT_TABLE docstring documents for
# agents/hardware_speccer.py's own _SHAPE_TO_PRIMITIVE (G1c) and Patch
# 5.2/5.3's cutout keyword matching. "strap" and "band" are kept as two
# separate keywords (not aliased to one) for the identical reason that
# same CUTOUT_TABLE docstring gives for keeping "led"/"indicator"
# separate: a part BOM'd as e.g. "Wrist Strap" or "Wearable Band" should
# match on whichever word actually appears, without needing the two
# words normalized to each other first.
_STRAP_KEYWORDS = ("strap", "band")


def _is_strap_or_band(part: dict) -> bool:
    """True if `part`'s own `generic_name`/`aliases` free-text fields
    contain a strap/band keyword. Checks BOTH fields -- same "canonical
    generic_name/aliases" pairing eo/enclosure_spec.py's own module
    docstring already documents for eo/hw_reference.py's retrieval
    keying, so a part whose distributor-neutral `generic_name` doesn't
    happen to say "strap" but one of its own `aliases` does (or vice
    versa) still matches, rather than only ever checking one field.

    Missing/non-string/non-list fields default to "no match" rather than
    raising -- same fail-safe posture every other keyword-scan helper in
    this package already holds for a malformed or absent BOM field.
    """
    generic_name = part.get("generic_name")
    text_fields = [generic_name] if isinstance(generic_name, str) else []

    aliases = part.get("aliases")
    if isinstance(aliases, list):
        text_fields.extend(a for a in aliases if isinstance(a, str))

    haystack = " ".join(text_fields).lower()
    return any(keyword in haystack for keyword in _STRAP_KEYWORDS)


def resolve_material(part: dict, archetype: dict) -> str:
    """Resolves a single BOM `part` dict to a MATERIAL_PROPERTIES key
    (Patch E.1) -- "pla_rigid" for every part/archetype combination
    except a strap/band-category part on a `wearable`-mobility archetype,
    which resolves to "tpu_flexible".

    `archetype` is the same `{"enclosure_mode", "mobility_type"}` shape
    Phase A's classify_archetype()/resolve_ambiguous_archetype() produce
    and Patch A.3 stashes onto `mech["archetype"]` -- this function reads
    only `archetype.get("mobility_type")` off it, tolerant of `archetype`
    being `None`/missing/malformed (resolves to the safe DEFAULT_MATERIAL
    in that case, same "never let a bad/missing upstream field produce
    anything other than today's unchanged default behavior" posture
    Patch D.1's own ACCESS_TYPES fallback and Patch D.5's own
    DEFAULT_ACCESS_TYPE fallback already establish for this same class of
    "field might not be there yet" situation).

    Never mutates `part` or `archetype`. Always returns a key that is
    guaranteed present in MATERIAL_PROPERTIES (either "pla_rigid" or
    "tpu_flexible", both defined by Patch E.1) -- never an arbitrary
    string a caller would need to re-validate before indexing
    MATERIAL_PROPERTIES with it.
    """
    if not isinstance(part, dict):
        return DEFAULT_MATERIAL

    mobility_type = archetype.get("mobility_type") if isinstance(archetype, dict) else None
    category = part.get("category")

    if (
        mobility_type == "wearable"
        and category in _STRAP_ELIGIBLE_CATEGORIES
        and _is_strap_or_band(part)
    ):
        return "tpu_flexible"

    return DEFAULT_MATERIAL


# ---------------------------------------------------------------------------
# Patch K.2 (MiniMe reliability guide, Phase K -- Pricing Pipeline): a
# 3D_PRINT-category BOM part (enclosure housing/lid, brackets, sensor
# holders -- anything printed, not purchased) has no real-world retail
# listing for agents/part_price_finder.py's find_price() to ever
# legitimately return. Routing it through that market search anyway
# wastes an LLM extraction call per part AND can surface a stray,
# unrelated retail hit as if it were a real vendor price for something
# nobody sells. estimate_print_cost_bdt() below is the deterministic,
# LLM-free replacement agents/hardware_speccer.py's _populate_prices()
# and api/routes/workspace_data.py's refresh_part_prices() both call
# instead, for exactly this category -- same "pure function first"
# build order this package's other estimation helpers
# (resolve_material() above, eo/mech_mass.py's estimate_mass()) already
# follow.
# ---------------------------------------------------------------------------

# Phase E's own MATERIAL_PROPERTIES (eo/enclosure_spec.py) has no
# cost-per-gram key yet -- per this patch's own guide wording ("reusing
# Phase E's own MATERIAL_PROPERTIES cost-per-gram if present, else a
# documented flat estimate"), this is that documented flat estimate: a
# rough, approximate PLA filament retail cost in Bangladesh (roughly
# 1200-1500 BDT/kg for a hobbyist-grade spool) plus a modest per-gram
# allowance for printer wear/electricity, rounded to one easy-to-audit
# number. _cost_per_gram_bdt() below already checks a material's own
# MATERIAL_PROPERTIES override first, so a future patch that adds a real
# "cost_per_gram_bdt" key to a specific material entry (e.g. tpu_flexible
# costs more per gram than pla_rigid in real life) takes priority over
# this global fallback without needing any change here.
PRINT_COST_BDT_PER_GRAM = 3.5

# FDM prints are never solid -- typical hobbyist slicer settings (15-20%
# infill, a handful of perimeter shells) put actual extruded material at
# roughly a third of a part's bounding-box volume for the small,
# thin-walled mechanical parts this BOM category covers (enclosures,
# brackets, mounts). A coarse, documented approximation, not a real
# slice -- good enough to rank a print's cost sanely against a purchased
# part's real price, not good enough to quote a customer.
_FDM_FILL_FACTOR = 0.3

# PLA density, g/cm^3 -- the same "every structural part is implicitly
# one rigid, generic 3D-printable plastic" baseline this whole Phase E
# package already assumes (see this module's own docstring above, and
# eo/enclosure_spec.py's DEFAULT_MATERIAL = "pla_rigid").
_PLA_DENSITY_G_PER_CM3 = 1.24

# Used whenever a part has no full {"w","h","d"} bounding box to compute
# a volume from -- no curated-table dimension match yet, an
# LLM-estimated sizing that only resolved some axes, or a Cylindrical
# part whose "d" is legitimately null per
# agents/component_dimension_table.py's own "null means not applicable
# to this shape" convention. A small, clearly-labeled placeholder rather
# than refusing to price the line item, or reaching for a network/LLM
# call just to get a number -- same "never leave a BOM line silently
# unresolved" posture eo/mech_mass.py's own estimate_mass() fallback
# already takes for mass, kept LLM-free here so this stays cheap and
# instant across a whole parts list.
_FLAT_ESTIMATE_BDT = 45.0


def _cost_per_gram_bdt(material: str) -> float:
    """A material's own MATERIAL_PROPERTIES override dict wins if it
    ever defines a "cost_per_gram_bdt" key (none do yet -- see this
    section's own module-level comment) -- falls back to the documented
    flat PRINT_COST_BDT_PER_GRAM otherwise. Mirrors MATERIAL_PROPERTIES'
    own "partial override" convention (Patch E.1's docstring) rather
    than requiring every material entry to define a cost the moment this
    function starts existing.
    """
    overrides = MATERIAL_PROPERTIES.get(material) or {}
    cost = overrides.get("cost_per_gram_bdt")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return cost
    return PRINT_COST_BDT_PER_GRAM


def estimate_print_cost_bdt(part: dict, material: str = None) -> float:
    """K.2 entry point. Deterministic, LLM-free per-unit estimate (BDT)
    of what a single 3D_PRINT-category BOM part costs to print --
    multiplying by `qty` is the caller's job, same as every other price
    source in this pipeline (part_price_finder.py's own listings are
    per-unit too).

    `material`: a MATERIAL_PROPERTIES key (typically resolve_material()'s
    own return value) when the caller has archetype context to resolve
    one -- agents/hardware_speccer.py's _populate_prices() does. None
    (the default) when it doesn't -- api/routes/workspace_data.py's
    refresh_part_prices() has no archetype threaded through it -- in
    which case this falls back to DEFAULT_MATERIAL, same fallback
    resolve_material() itself uses for a missing/malformed archetype.
    Any value that isn't a real MATERIAL_PROPERTIES key (typo, stale
    caller) falls back the same way rather than raising a KeyError.

    Uses `part["dimensions_mm"]` (the same `{"w","h","d"}` shape
    agents/component_dimension_table.py's _row_to_match() and
    agents/hardware_speccer.py's G1c sizing already produce) as a
    bounding-box volume proxy -- but ONLY when all three axes are
    present as positive numbers. A part with just one or two axes (e.g.
    a Cylindrical part's "d", legitimately null per that module's own
    convention) has no well-defined bounding-box volume to multiply
    partial axes into, so it takes the flat-estimate path below instead
    of producing a dimensionally meaningless number.

    Falls back to `_FLAT_ESTIMATE_BDT` whenever `part`/`dimensions_mm`
    is missing, malformed, or incomplete in the way described above --
    never raises, never makes a network or LLM call, same fail-safe
    posture every other estimation function in this package
    (resolve_material() above, eo/mech_mass.py's estimate_mass())
    already holds itself to.
    """
    resolved_material = material if material in MATERIAL_PROPERTIES else DEFAULT_MATERIAL
    cost_per_gram = _cost_per_gram_bdt(resolved_material)

    dims = part.get("dimensions_mm") if isinstance(part, dict) else None
    if not isinstance(dims, dict):
        return _FLAT_ESTIMATE_BDT

    axes = [dims.get(k) for k in ("w", "h", "d")]
    if not all(isinstance(a, (int, float)) and not isinstance(a, bool) and a > 0 for a in axes):
        return _FLAT_ESTIMATE_BDT

    volume_mm3 = axes[0] * axes[1] * axes[2]
    volume_cm3 = (volume_mm3 * _FDM_FILL_FACTOR) / 1000.0
    mass_g = volume_cm3 * _PLA_DENSITY_G_PER_CM3
    return round(mass_g * cost_per_gram, 2)


if __name__ == "__main__":
    _wearable_archetype = {"enclosure_mode": "full", "mobility_type": "wearable"}
    _static_archetype = {"enclosure_mode": "full", "mobility_type": "static"}

    _strap_part = {"id": "strap_1", "category": "3D_PRINT", "generic_name": "Wrist Strap", "aliases": []}
    _housing_part = {"id": "housing_1", "category": "3D_PRINT", "generic_name": "3D-Printed Enclosure Housing", "aliases": []}
    _mcu_part = {"id": "mcu_1", "category": "mcu", "generic_name": "ESP32 Dev Board", "aliases": []}

    print("strap + wearable  ->", resolve_material(_strap_part, _wearable_archetype))
    print("strap + static    ->", resolve_material(_strap_part, _static_archetype))
    print("housing + wearable->", resolve_material(_housing_part, _wearable_archetype))
    print("mcu + wearable    ->", resolve_material(_mcu_part, _wearable_archetype))
    assert MATERIAL_PROPERTIES  # sanity: E.1's table is importable from here too
