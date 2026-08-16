"""
eo/mech_wiring_weight.py — Phase 4, Patch 4.1 of the Mech/Enclosure
implementation guide ("Wiring-weighted placement", gap #6, refined):
the pure, deterministic edge-weight map builder Patch 4.2 needs before
it can change eo/mech_device.py's own packing-loop iteration order --
same "land the pure function first" build-order reasoning every earlier
"-1"/"2.2"-shaped patch in this tree already established (eo/
mech_supports.py's own compute_standoffs() before Patch 2.4's
apply_supports_generation(), eo/mech_enclosure.py's own
compute_housing_footprint() before apply_enclosure_generation()).

Goal (Master Guide, Phase 4 "Goal"): "Within a zone, order parts by
actual wiring adjacency instead of arbitrary/declaration order." This
module only builds the weight map that ordering reads from -- it never
touches `mech["sections"]`, `mech["placements"]`, or packing order
itself; that's Patch 4.2's own job in eo/mech_device.py.

Input shape: `mech["wiring"]["edges"]`, a list of
{"from": part_id, "to": part_id, "kind": ..., "from_pin": ..., "to_pin":
...} dicts -- the same shape agents/hardware_speccer.py's own
SYSTEM_PROMPT documents and stashes onto `custom["wiring"]`/eventually
`mech["wiring"]` (Patch 9.2's own "Render mech.wiring edges" is the
first thing that actually surfaces this key to the frontend, but
nothing about that patch changes the edge shape itself -- this module
reads the same "from"/"to" fields whether or not 9.2 has landed yet).
Only "from"/"to" matter here -- "kind"/"from_pin"/"to_pin" are never
read, since Phase 4's own scope is adjacency counting, not per-signal
weighting (see this module's own "Explicit scope limit" note below).

Section resolution: a wiring edge names PARTS ("mcu_1", "sensor_1"),
not sections -- this module resolves each side to its owning Level-3
section by walking `mech["sections"]` -> eo/mech_sections.py's own
subsections_for_section() -> eo/mech_subsections.py's own
members_for_subsection(), the same two-hop section->subsection->member
resolution eo/mech_supports.py's own _joined_section_members() (Patch
2.4) and eo/mech_device.py's own apply_device_merge() already use for
the identical reason: `mech["placements"]` entries never carry a
`section_id` of their own, only `mech["sections"]`'s own nested
`subsection_ids`/`member_ids` do. This module deliberately doesn't take
a `parts` argument (unlike eo/mech_supports.py's own
apply_supports_generation()) -- section membership is something
`mech["sections"]` already resolved by the time Level 3->4 runs, so
there's nothing left to join against a BOM for here, just a shape to
walk.

Symmetric, section-order-independent keys: a wiring edge has a
direction ("from"/"to"), but section ADJACENCY doesn't -- two edges
between the same section pair, wired in opposite directions, count as
the same adjacency, not two different ones. Every key returned is
`tuple(sorted((section_a, section_b)))`, so `weights[("Compute",
"Sensing")]` and a caller accidentally querying `("Sensing",
"Compute")` never silently miss each other -- see section_pair_weight()
below, the one sanctioned way Patch 4.2 should read this map, so no
caller has to re-derive the sorting convention itself.

Explicit scope limit (mirrors the Master Guide's own Phase 4 "Explicit
scope limit"): this is an unweighted-by-signal-type EDGE COUNT between
two sections, not a true wire-length or a per-net criticality score --
a "power" edge and a "data" edge both count as 1. Refining that is out
of scope unless a later phase specifically calls for it, same posture
the Master Guide already takes toward NOT building a true
minimum-wirelength global solver here.

Pure function: never mutates `mech`, never does I/O. Two calls with the
same input always return the same output -- same "idempotent by
construction" contract every other pure-planning function in this tree
already holds itself to (eo/mech_supports.py's own compute_standoffs(),
eo/mech_enclosure.py's own compute_housing_footprint()).
"""

from eo.mech_sections import subsections_for_section
from eo.mech_subsections import members_for_subsection


def _part_id_to_section_id(mech: dict) -> dict:
    """Resolves every placement across EVERY section of `mech` into a
    flat `{part_id: section_id}` map, via the same two-hop
    section->subsection->member resolution this module's own docstring
    describes. A `part_id` that shows up under more than one section
    (shouldn't happen -- eo/mech_sections.py's own group_into_sections()
    buckets each subsection into exactly one section) keeps whichever
    section it's seen under LAST; not a case this module needs to guard
    harder than that, same "trust the upstream invariant, don't
    re-validate it" posture eo/mech_supports.py's own
    _joined_section_members() already takes toward `mech["sections"]`'s
    own shape.

    Kept private: this mapping is an internal resolution step, not a
    piece of this module's own public surface (build_section_
    adjacency_weights() and section_pair_weight() are) -- a caller that
    wants part->section resolution for its own purposes should walk
    `mech["sections"]` itself the same way every other module in this
    tree already does, not depend on this helper's own name/shape.
    """
    mapping = {}
    for section in (mech or {}).get("sections") or []:
        if not isinstance(section, dict):
            continue
        section_id = section.get("section_id")
        if not section_id:
            continue
        for subsection in subsections_for_section(mech, section):
            for member in members_for_subsection(mech, subsection):
                if not isinstance(member, dict):
                    continue
                part_id = member.get("part_id")
                if part_id:
                    mapping[part_id] = section_id
    return mapping


def build_section_adjacency_weights(mech: dict) -> dict:
    """Returns `{(section_a, section_b): edge_count}` -- one entry per
    unordered section pair with at least one wiring edge between their
    own member parts, `edge_count` the number of `mech["wiring"]["edges"]`
    entries connecting that pair. See module docstring for key
    canonicalization (`tuple(sorted(...))`) and the "unweighted-by-
    signal-type edge count" scope limit.

    An edge is skipped, never counted, when:
      - either side's `part_id` doesn't resolve to any section (not yet
        placed, a mount/fastener that was never wired, or `mech`
        genuinely has no `sections` yet -- "nothing to weight against
        yet" is the same no-op posture every other early-pipeline read
        in this tree already takes, never an error);
      - both sides resolve to the SAME section -- an edge fully inside
        one section says nothing about adjacency BETWEEN sections,
        which is the only thing this map exists to describe (Phase 4's
        own goal is ordering sections/parts relative to OTHER zones,
        not intra-section wiring, which G3e/G3f's own within-subsection
        placement already handles).

    Non-dict edge entries are silently skipped, same fail-safe posture
    every other pure-planning function in this tree already holds
    itself to. Returns `{}` (never raises) when `mech` isn't a dict,
    has no `wiring`/`edges`, or has no `sections` yet.

    Pure function: never mutates `mech`. Two calls with the same input
    always return the same output.
    """
    if not isinstance(mech, dict):
        return {}
    edges = ((mech.get("wiring") or {}).get("edges")) or []
    if not edges:
        return {}

    part_to_section = _part_id_to_section_id(mech)
    if not part_to_section:
        return {}

    weights = {}
    for edge in edges:
        if not isinstance(edge, dict):
            continue
        section_a = part_to_section.get(edge.get("from"))
        section_b = part_to_section.get(edge.get("to"))
        if not section_a or not section_b or section_a == section_b:
            continue
        key = tuple(sorted((section_a, section_b)))
        weights[key] = weights.get(key, 0) + 1
    return weights


def section_pair_weight(weights: dict, section_a: str, section_b: str) -> int:
    """Convenience lookup into `build_section_adjacency_weights()`'s own
    output that applies the same `tuple(sorted(...))` canonicalization
    the builder itself used, so Patch 4.2's own packing-order change in
    eo/mech_device.py never has to re-derive (or risk getting backwards)
    that convention at its own call site -- the one sanctioned way to
    read this map from outside this module, same "expose the accessor,
    not the encoding" posture eo/mech_supports.py's own module keeps
    `_corner_primitives()` private behind compute_standoffs()/
    compute_screw_bosses() for.

    Returns 0 (never raises, never KeyErrors) for any pair with no
    recorded adjacency -- "no wiring between these two sections" is a
    completely ordinary, expected case (most zone-sharing section pairs
    in a small device will have zero direct edges), not a lookup
    failure.
    """
    if not weights or not section_a or not section_b:
        return 0
    key = tuple(sorted((section_a, section_b)))
    return weights.get(key, 0)
