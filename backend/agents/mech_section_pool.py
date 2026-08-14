"""
agents/mech_section_pool.py — G3f-1 (Master Guide, "G3/G4. Hierarchical
parallel build + validate", Level 2->3 "Sections", the "generate" half):
fans one LLM call out PER SECTION via eo/worker_pool.py, same worker-pool
pattern agents/mech_subsection_pool.py (G3e-2) already established one
level down -- "same idea, one level up," per the Master Guide's own
description of every level above Level 0->1: "Placement generation is
split by zone/subsystem instead of one monolithic call... solve each
group's relative placement in parallel (same worker-pool pattern)."

Unit of work, and why it's NOT one call per subsection pair: G3e-2's
unit of work was one call per (part, its own mount) pair -- always
exactly 2 members, so "propose the mount's offset relative to the part"
fully described the job. A SECTION can hold more than two subsections at
once (e.g. a Sensing section with three independent sensor subsections
that all need to sit clear of each other, not just clear of their own
mounts), so this level's unit of work is one call per SECTION, proposing
every non-anchor subsection's offset relative to that section's own
anchor subsection all at once -- the LLM needs every member in view
simultaneously to keep them mutually clear of each other, the same
reason the original monolithic Call 2 (SYSTEM_PROMPT_WIRING) needed the
whole device in view before G3 started splitting it up. Parallelism
happens ACROSS sections (one worker thread per section, same
ThreadPoolExecutor shape as every other pool in this codebase), not
within one.

Anchor subsection, and why it's picked here instead of by the LLM: the
subsection with the largest footprint volume in the section (ties broken
by `subsection_id` alphabetically, for a repeatable answer) is fixed at
its own current absolute position, and every OTHER subsection in the
section gets an LLM-proposed offset relative to it -- the same "one
member fixed, the rest placed relative to it" shape G3e-2 already uses
(there: the part is fixed, the mount is offset from it). Picking the
anchor deterministically in Python, not asking the LLM to name one,
means eo/mech_repair.py's future run_level_2_3_repair() (G3f-2, NOT this
patch) always knows which subsection in a section is the fixed reference
frame on every regeneration round, instead of the anchor potentially
drifting between attempts. A section with only one subsection has
nothing to place relative to anything and is skipped entirely, mirroring
agents/mech_subsection_pool.py's own `_needs_llm_relative_placement()`
singleton short-circuit one level down.

Footprint input, not raw part dimensions: each subsection's own
`footprint` (w/h/d/x/y/z), set onto `mech["subsections"]` by
eo/mech_repair.py's run_level_1_2_repair() once Level 1->2 settles, is
what this module sends the LLM -- a subsection's real occupied volume
includes its mount, and Level 2->3 needs to keep whole ASSEMBLIES clear
of each other, not just anchor parts. A subsection with no `footprint`
yet (Level 1->2 hasn't reached it, or repair is still outstanding on it)
is skipped for this pass rather than guessed at with incomplete data --
same "nothing to check/place yet is not a violation, just not-ready"
posture eo/mech_validator.py's own `_checkable_placements()` already
holds.

Unlike content_adapter_pool.py, this isn't a standalone top-level agent
dispatched by eo/executor.py -- like agents/mech_subsection_pool.py, it's
one more mutator step meant to run synchronously from agents/
hardware_speccer.py's own run_hardware_speccer() pipeline (once G3f-2
wires the Level 2->3 driver in, same "run() called directly, mutates
spec/mech in place" shape as every prior mech pool in this tree), called
with `spec`/`parts` directly.

Worker selection reuses eo/worker_pool.py's shared, role_tag-parameterized
helper with role_tag="mech_section", registered onto the same Cerebras
Code-Writer-Pool accounts "mech_primitive"/"mech_subsection" already
share (eo/registry.py) -- not a new pool of keys, same "quota-aware
fairness spreads load regardless of which tag(s) an account carries"
reasoning that pool's own comment there already documents.

Deferred imports of agents.hardware_speccer: N/A for this module --
unlike agents/mech_subsection_pool.py, this module never needs to read
`mount_spec` or any other hardware_speccer-owned parsing, so it has no
circular-import edge to route around in the first place.

regenerate_section() (bottom of this module, before run()) is this
level's `regenerate_node_fn` -- built now, alongside the rest of this
level's "generate" half, for the exact same reason agents/
mech_subsection_pool.py's own regenerate_subsection() was built inside
G3e-2 rather than deferred to G3e-4: it shares _generate_section_offsets()
below with run()'s own first-proposal pass instead of duplicating the
generate_text() call, and G3f-2's own eo/mech_repair.py integration
(NOT this patch) just needs something to import and wire into
run_repair_loop(), not something to write from scratch.
"""

import os
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from relay.emitter import emit_event
from utils.llm_client import generate_text
from eo.worker_pool import _select_workers as _select_workers_for_role
from eo.mech_sections import group_into_sections, subsections_for_section

ROLE_TAG = "mech_section"

# Same "generous slack, numeric-sanity backstop only" reasoning as
# agents/mech_subsection_pool.py's own _MAX_OFFSET_MULTIPLIER -- real
# shape-aware collision is eo/mech_validator.py's future LEVEL_2_3 job
# (G3f-2, not this patch), not this module's.
_MAX_OFFSET_MULTIPLIER = 3

_VALID_AXES = ("x", "y", "z")

SYSTEM_PROMPT = """You are a mechanical layout assistant. You are given \
ONE functional section of a device (e.g. "Sensing", "Power") made up of \
several independent subsections -- each subsection's own aggregate \
bounding box w/h/d in millimeters, already known to be internally free \
of collisions. One subsection is marked as this section's fixed anchor. \
Your only job is deciding where every OTHER subsection sits RELATIVE to \
the anchor so that no two subsections' bounding boxes overlap; you do \
not resize or redesign any subsection.

Coordinate convention: the anchor subsection sits at this section's own \
local origin (0,0,0), corner-origin like a CSS box model -- x maps to \
width (w), y maps to height (h), z maps to depth (d), the same axis \
convention already used everywhere else in this pipeline. Propose ONLY \
each non-anchor subsection's own offset {x,y,z} in this local frame, \
spacing subsections out (side by side, stacked, or front-to-back) so \
their bounding boxes stay clear of the anchor and of each other -- a \
small, sensible gap is fine, but keep the overall arrangement compact.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{"subsection_offsets": {"<subsection_id>": {"x": 0, "y": 0, "z": 0}, ...}}
One entry per non-anchor subsection given to you, keyed by its own \
subsection_id.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return text


def _as_float(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _footprint_volume(footprint: dict) -> float:
    footprint = footprint or {}
    w = _as_float(footprint.get("w"))
    h = _as_float(footprint.get("h"))
    d = _as_float(footprint.get("d"))
    return w * h * d


def _pick_anchor(subsection_ids: list, footprints_by_id: dict) -> str:
    """Largest footprint volume wins; ties (including "no footprint at
    all", volume 0) broken alphabetically by subsection_id -- see module
    docstring's "Anchor subsection" section on why this is decided here,
    deterministically, rather than left to the LLM."""
    return max(
        subsection_ids,
        key=lambda sid: (_footprint_volume(footprints_by_id.get(sid)), sid),
    )


def _default_offset_for_index(index: int, anchor_footprint: dict) -> dict:
    """Fallback used on any parse failure, and as this module's plain
    "place it somewhere sane" default -- lines non-anchor subsections up
    beside the anchor along x, each past the previous one, same spirit
    as agents/mech_subsection_pool.py's own _default_relative_offset()
    (edge-to-edge, no overlap, no LLM required to be merely valid)."""
    anchor_w = _as_float((anchor_footprint or {}).get("w")) or 10.0
    return {"x": round(anchor_w * (index + 1), 2), "y": 0.0, "z": 0.0}


def _parse_section_offsets(raw_text: str, non_anchor_ids: list,
                            footprints_by_id: dict, anchor_footprint: dict) -> dict:
    """Parses/clamps the LLM's response into {subsection_id: {x,y,z}},
    one entry per `non_anchor_ids`. Any subsection missing from the
    response, or a response that fails to parse at all, falls back to
    _default_offset_for_index() for just that subsection -- never raises,
    same fail-safe convention agents/mech_subsection_pool.py's own
    _parse_relative_offset() already uses."""
    try:
        parsed = json.loads(_strip_fences(raw_text))
        raw_offsets = parsed.get("subsection_offsets")
        if not isinstance(raw_offsets, dict):
            raise ValueError("no subsection_offsets in response")
    except (json.JSONDecodeError, AttributeError, ValueError):
        raw_offsets = {}

    max_extent = max(
        [_as_float((footprints_by_id.get(sid) or {}).get(dim))
         for sid in non_anchor_ids + [None] for dim in ("w", "h", "d")] + [1.0]
    ) * _MAX_OFFSET_MULTIPLIER

    offsets = {}
    for index, subsection_id in enumerate(non_anchor_ids):
        raw_offset = raw_offsets.get(subsection_id)
        if not isinstance(raw_offset, dict):
            offsets[subsection_id] = _default_offset_for_index(index, anchor_footprint)
            continue
        offset = {}
        for axis in _VALID_AXES:
            value = _as_float(raw_offset.get(axis))
            offset[axis] = round(min(max(value, -max_extent), max_extent), 2)
        offsets[subsection_id] = offset
    return offsets


def _generate_section_offsets(section_id: str, anchor_id: str, non_anchor_ids: list,
                               footprints_by_id: dict, key_env: str, agent_name: str,
                               session_id: str = None, path: str = None, domain: str = None,
                               violation_issue: str = None) -> dict:
    """The actual generate_text() call, factored out so both this
    module's pool path (run(), first proposal) and its repair path
    (regenerate_section(), G3f-2's future regeneration context) share one
    implementation -- same split agents/mech_subsection_pool.py's own
    _generate_relative_placement() already establishes one level down.

    Returns {subsection_id: {x,y,z}} for every id in `non_anchor_ids`.
    """
    anchor_footprint = footprints_by_id.get(anchor_id) or {}
    payload = {
        "section_id": section_id,
        "anchor": {
            "subsection_id": anchor_id,
            "w": anchor_footprint.get("w") or 1,
            "h": anchor_footprint.get("h") or 1,
            "d": anchor_footprint.get("d") or 1,
        },
        "subsections": [
            {
                "subsection_id": sid,
                "w": (footprints_by_id.get(sid) or {}).get("w") or 1,
                "h": (footprints_by_id.get(sid) or {}).get("h") or 1,
                "d": (footprints_by_id.get(sid) or {}).get("d") or 1,
            }
            for sid in non_anchor_ids
        ],
    }
    if violation_issue:
        payload["previous_attempt_failed_because"] = violation_issue
    user_content = json.dumps(payload)

    chain = [{"provider": "cerebras", "model": "gpt-oss-120b", "key_env": key_env}]

    try:
        raw = generate_text(
            SYSTEM_PROMPT, user_content, chain,
            agent_name=agent_name, session_id=session_id, path=path, domain=domain,
        )
        return _parse_section_offsets(raw, non_anchor_ids, footprints_by_id, anchor_footprint)
    except RuntimeError:
        # Every provider in the chain failed -- fall back to the same
        # safe "line them up beside the anchor" default this module's
        # own parse failure already falls back to, so a quota/outage
        # blip degrades this section to a sane default, never a crash of
        # the whole pool.
        return {
            sid: _default_offset_for_index(i, anchor_footprint)
            for i, sid in enumerate(non_anchor_ids)
        }


def _footprints_by_id(mech: dict) -> dict:
    return {
        s.get("subsection_id"): s.get("footprint")
        for s in (mech or {}).get("subsections") or []
        if isinstance(s, dict) and isinstance(s.get("footprint"), dict)
    }


def _apply_offsets(mech: dict, anchor_id: str, offsets: dict, footprints_by_id: dict) -> None:
    """Shifts every MEMBER of each non-anchor subsection by the delta
    between that subsection's newly-proposed absolute position (anchor's
    own absolute footprint origin + the proposed local offset) and its
    current absolute position (its own footprint origin) -- moving the
    subsection as a rigid whole so the internal part/mount geometry
    Level 1->2 already validated is preserved, exactly the same "shift
    every member together" reasoning eo/mech_repair.py's future Level
    2->3 driver (G3f-2) will need for its own regeneration path, applied
    here for run()'s first-proposal pass.

    A subsection with no footprint on either end (anchor or the member
    being shifted) is left untouched -- nothing to compute a delta from,
    same "don't guess with incomplete data" posture the rest of this
    module holds to.
    """
    from eo.mech_subsections import group_into_subsections, members_for_subsection

    anchor_footprint = footprints_by_id.get(anchor_id)
    if not isinstance(anchor_footprint, dict):
        return

    subsections_by_id = {
        s.get("subsection_id"): s for s in group_into_subsections(mech)
        if isinstance(s, dict) and s.get("subsection_id")
    }

    for subsection_id, offset in offsets.items():
        current_footprint = footprints_by_id.get(subsection_id)
        if not isinstance(current_footprint, dict):
            continue
        new_x = (anchor_footprint.get("x") or 0) + offset.get("x", 0)
        new_y = (anchor_footprint.get("y") or 0) + offset.get("y", 0)
        new_z = (anchor_footprint.get("z") or 0) + offset.get("z", 0)
        delta_x = new_x - (current_footprint.get("x") or 0)
        delta_y = new_y - (current_footprint.get("y") or 0)
        delta_z = new_z - (current_footprint.get("z") or 0)

        subsection = subsections_by_id.get(subsection_id)
        if subsection is None:
            continue
        for member in members_for_subsection(mech, subsection):
            member["x"] = round((member.get("x") or 0) + delta_x, 2)
            member["y"] = round((member.get("y") or 0) + delta_y, 2)
            member["z"] = round((member.get("z") or 0) + delta_z, 2)


def _place_section(section: dict, footprints_by_id: dict, key_env: str, worker_id: int,
                    session_id: str = None, path: str = None, domain: str = None) -> tuple:
    """Runs on one worker thread with one fixed account. Returns
    (section_id, anchor_id, offsets). Mirrors agents/
    mech_subsection_pool.py's own
    _generate_relative_placement_for_subsection() almost exactly -- same
    generate_text()/agent_start/agent_done shape, one level up."""
    section_id = section.get("section_id")
    subsection_ids = [sid for sid in section.get("subsection_ids") or [] if sid in footprints_by_id]
    anchor_id = _pick_anchor(subsection_ids, footprints_by_id)
    non_anchor_ids = [sid for sid in subsection_ids if sid != anchor_id]

    agent_name = f"mech_section_{worker_id}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Mech Section {worker_id} — {section_id} ({len(non_anchor_ids)} placed)"})
    started = time.monotonic()

    offsets = _generate_section_offsets(
        section_id, anchor_id, non_anchor_ids, footprints_by_id, key_env, agent_name,
        session_id=session_id, path=path, domain=domain,
    )

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": f"placed {len(offsets)} subsection(s) around anchor {anchor_id}",
                        "duration_ms": duration_ms})
    return section_id, anchor_id, offsets


def regenerate_section(mech: dict, node_id: str, violation: dict, attempt: int,
                        parts: list, key_override=None, session_id: str = None,
                        path: str = None, domain: str = None) -> None:
    """G3f-2's future `regenerate_node_fn` for Level 2->3 -- the exact
    `(mech, node_id, violation, attempt)` -> None shape eo/mech_repair.py's
    run_repair_loop() requires (see that module's docstring), wired to
    THIS pool's own single-section generation call
    (_generate_section_offsets() above) instead of duplicating it. Built
    now, alongside the rest of this level's "generate" half -- see module
    docstring's own note on why -- but not yet CALLED from anywhere: that
    wiring is G3f-2's job (eo/mech_repair.py's future
    run_level_2_3_repair()), not this patch's.

    `node_id` is a `section_id` (one of eo/mech_sections.py's
    _SECTION_ORDER values). Re-derives the section's current
    subsection_ids and footprints from `mech` itself (mutated in place by
    prior generate/repair rounds), same "mech is always the current
    source of truth" contract agents/mech_subsection_pool.py's own
    regenerate_subsection() already documents for itself, rather than
    requiring the caller to pass the section's membership back in.

    `parts` must be supplied by the caller (this module has no other way
    to re-derive category groupings from `mech` alone) -- see
    eo/mech_sections.py's own module docstring on why `parts` is always
    caller-supplied rather than fetched internally.
    """
    sections_by_id = {
        s.get("section_id"): s for s in group_into_sections(mech, parts)
        if isinstance(s, dict) and s.get("section_id")
    }
    section = sections_by_id.get(node_id)
    if section is None:
        raise ValueError(f"regenerate_section: no section found for node_id={node_id!r}")

    footprints_by_id = _footprints_by_id(mech)
    subsection_ids = [sid for sid in section.get("subsection_ids") or [] if sid in footprints_by_id]
    if len(subsection_ids) < 2:
        raise ValueError(
            f"regenerate_section: node_id={node_id!r} has fewer than 2 checkable "
            "subsections to reposition relative to each other"
        )
    anchor_id = _pick_anchor(subsection_ids, footprints_by_id)
    non_anchor_ids = [sid for sid in subsection_ids if sid != anchor_id]

    agent_name = f"mech_section_repair_{node_id}"
    key_env = key_override
    if key_env is None:
        selected = _select_workers_for_role(ROLE_TAG, 1, None, session_id=session_id, agent_name=agent_name)
        key_env = selected[0] if selected else None

    offsets = _generate_section_offsets(
        node_id, anchor_id, non_anchor_ids, footprints_by_id, key_env, agent_name,
        session_id=session_id, path=path, domain=domain,
        violation_issue=(violation or {}).get("issue"),
    )
    _apply_offsets(mech, anchor_id, offsets, footprints_by_id)


def run(spec: dict, parts: list, session_id: str = None, path: str = None,
        domain: str = None, key_override=None, expanded: bool = False) -> dict:
    """G3f-1's entry point -- meant to be called synchronously from
    agents.hardware_speccer.run_hardware_speccer() once G3f-2 wires Level
    2->3 into the pipeline, the same in-process shape agents/
    mech_subsection_pool.py's own run() already uses one level down.
    Mutates every non-anchor subsection's member placements in `mech`
    in place and returns `spec`.

    Requires eo/mech_repair.py's run_level_1_2_repair() to have already
    run and populated `mech["subsections"][*]["footprint"]` -- a section
    with fewer than two subsections carrying a footprint is skipped
    entirely (nothing this call can safely place), same "no-op on
    not-ready-yet data" posture as the rest of this level.

    key_override: same three-shape contract as agents/
    mech_subsection_pool.py's/mech_primitive_pool.py's run() -- None
    (pick own workers via _select_workers), a single key_env string (use
    only that account for every section), or a list (use exactly those
    accounts as the parallel worker pool).

    No-op (returns `spec` unchanged, no worker-pool call at all) when no
    section actually has 2+ checkable subsections -- same short-circuit
    agents/mech_subsection_pool.py's own run() already uses for an empty
    job.
    """
    mech = spec.get("mech") or {}
    if not isinstance(mech.get("placements"), list):
        return spec

    footprints_by_id = _footprints_by_id(mech)
    sections = group_into_sections(mech, parts)
    targets = []
    for section in sections:
        checkable_ids = [sid for sid in section.get("subsection_ids") or [] if sid in footprints_by_id]
        if len(checkable_ids) >= 2:
            targets.append({"section_id": section.get("section_id"), "subsection_ids": checkable_ids})
    if not targets:
        return spec

    # Fixed pool size, same as agents/mech_subsection_pool.py's/
    # agents/mech_primitive_pool.py's run() -- NOT scaled to
    # len(targets). Keys are reused round-robin below when there are
    # more sections than workers.
    worker_count = 8 if expanded else 5
    key_envs = _select_workers_for_role(ROLE_TAG, worker_count, key_override,
                                        session_id=session_id, agent_name=ROLE_TAG)

    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _place_section, section, footprints_by_id,
                key_envs[i % len(key_envs)], (i % len(key_envs)) + 1,
                session_id=session_id, path=path, domain=domain,
            ): section
            for i, section in enumerate(targets)
        }
        for future in as_completed(futures):
            section_id, anchor_id, offsets = future.result()
            print(f"    [Mech Section Pool] placed {len(offsets)} subsection(s) in {section_id} around anchor {anchor_id}")
            _apply_offsets(mech, anchor_id, offsets, footprints_by_id)

    return spec


if __name__ == "__main__":
    _demo_spec = {
        "mech": {
            "placements": [
                {"part_id": "sensor_1", "x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5},
                {"part_id": "sensor_2", "x": 40, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5},
            ],
            "subsections": [
                {"subsection_id": "sensor_1", "member_ids": ["sensor_1"],
                 "footprint": {"x": 0, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5}},
                {"subsection_id": "sensor_2", "member_ids": ["sensor_2"],
                 "footprint": {"x": 40, "y": 0, "z": 0, "w": 15, "h": 10, "d": 5}},
            ],
        },
    }
    _demo_parts = [
        {"id": "sensor_1", "category": "sensor"},
        {"id": "sensor_2", "category": "sensor"},
    ]
    result = run(_demo_spec, _demo_parts)
    print(json.dumps(result["mech"]["placements"], indent=2))
