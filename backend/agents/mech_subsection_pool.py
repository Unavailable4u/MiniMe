"""
agents/mech_subsection_pool.py — G3e-2 (Master Guide, "G3/G4.
Hierarchical parallel build + validate", Level 1->2 "Subsections", the
"generate" half): fans one small LLM call out per subsection via
eo/worker_pool.py, same worker-pool pattern agents/mech_primitive_pool.py
(G3b) already uses for Level 0->1 -- "same idea, one level up" per the
Master Guide's own description of this whole tier: "Placement generation
is split by zone/subsystem instead of one monolithic call... solve each
group's relative placement in parallel (same worker-pool pattern)."

Scope: for every subsection eo/mech_subsections.py's
group_into_subsections() reports with an actual mount member (a
singleton subsection -- a part with no mount -- has nothing to place
relative to anything, so it's never in scope here), propose where that
mount sits RELATIVE to its part's own local origin. This is deliberately
narrower than "decide the whole subsection's absolute position" -- Level
2->3 (G3f) is what places a subsection relative to its section, this
module only ever settles the *internal* geometry of one part + its own
mount.

Skips subsections already grounded in real data: a part whose
`mount_spec` parses into a rect/c-c pattern already had its mount's size
AND position set deterministically off that real hole spec by
agents/hardware_speccer.py's `_resize_mount_parts_from_mount_spec()`
(a G3a side effect) -- running an LLM guess on top of real geometry would
replace known-good data with a worse guess, the same "don't guess where
you already know" split G3a/G3b already apply at Level 0->1. A `thread`-
pattern mount_spec (a single threaded boss, no separate bracket to size)
is NOT touched by that deterministic step, so it stays in scope here.

Unlike content_adapter_pool.py, this isn't a standalone top-level agent
dispatched by eo/executor.py -- like agents/mech_primitive_pool.py, it's
one more mutator step meant to run synchronously from
agents/hardware_speccer.py's own run_hardware_speccer() pipeline (once
G3e-4 wires the Level 1->2 driver in), called with `spec`/`parts`
directly and mutating spec["mech"]["placements"] in place.

Worker selection reuses eo/worker_pool.py's shared, role_tag-parameterized
helper with role_tag="mech_subsection" -- registered onto the same
Cerebras Code-Writer-Pool accounts "mech_primitive"/"content_writer"
already share (eo/registry.py), not a new pool of keys, same "quota-aware
fairness spreads load regardless of which tag(s) an account carries"
reasoning that pool's own comment there already documents.

Deferred imports of agents.hardware_speccer inside the functions below
(never at module level): same circular-import fix agents/
mech_primitive_pool.py's own module docstring documents, for the same
reason (agents/hardware_speccer.py will call this module's run()).
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
from eo.mech_subsections import group_into_subsections

ROLE_TAG = "mech_subsection"

# Generous slack around the part's own largest dimension -- big enough
# that a legitimate "beside" or "behind" placement never gets clipped,
# small enough to catch a genuinely broken/runaway LLM number. Shape-
# aware collision itself is G3e-3's job (real FreeCAD), not this
# module's -- this is only a numeric-sanity backstop, same spirit as
# agents/mech_primitive_pool.py's own _clamp_primitive() bound.
_MAX_OFFSET_MULTIPLIER = 3

_VALID_AXES = ("x", "y", "z")

SYSTEM_PROMPT = """You are a mechanical layout assistant. You are given \
ONE part and its own mount/holder (the small mechanical piece that \
holds it inside the enclosure) as a subsection -- their own bounding \
box w/h/d in millimeters each. Your only job is deciding where the \
mount sits RELATIVE to the part; you do not redesign either shape.

Coordinate convention: the part sits at this subsection's own local \
origin (0,0,0), corner-origin like a CSS box model -- x maps to width \
(w), y maps to height (h), z maps to depth (d), the same axis \
convention already used everywhere else in this pipeline (e.g. a lid's \
z is set to the housing's own d so it stacks directly on top). Propose \
ONLY the mount's own offset {x,y,z} in this local frame so the mount \
sits directly adjacent to the part -- below, beside, or behind it, with \
little or no gap -- and does not overlap the part's own bounding box.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{"mount_offset": {"x": 0, "y": 0, "z": 0}}
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


def _default_relative_offset(part_h: float) -> dict:
    """Fallback used on any parse failure AND as this module's plain
    "place it somewhere sane" default -- directly below the part, edge-
    to-edge, matching SYSTEM_PROMPT_WIRING's own already-established
    eyeballed convention ("the MCU's mount sits right under or beside
    the MCU's own placement") rather than inventing a new one."""
    return {"x": 0.0, "y": round(part_h, 2), "z": 0.0}


def _parse_relative_offset(raw_text: str, part_w: float, part_h: float, part_d: float) -> dict:
    """Parses/clamps the LLM's response into a usable {x,y,z} offset,
    falling back to _default_relative_offset() on any parse failure or a
    response that isn't even shaped like one -- never raises, same
    fail-safe convention agents/mech_primitive_pool.py's own
    _parse_primitives() already uses for its own LLM path."""
    try:
        parsed = json.loads(_strip_fences(raw_text))
        raw_offset = parsed.get("mount_offset")
        if not isinstance(raw_offset, dict):
            raise ValueError("no mount_offset in response")
    except (json.JSONDecodeError, AttributeError, ValueError):
        return _default_relative_offset(part_h)

    max_extent = max(part_w, part_h, part_d, 1.0) * _MAX_OFFSET_MULTIPLIER
    offset = {}
    for axis in _VALID_AXES:
        value = _as_float(raw_offset.get(axis))
        offset[axis] = round(min(max(value, -max_extent), max_extent), 2)
    return offset


def _needs_llm_relative_placement(subsection: dict, parts_by_id: dict) -> bool:
    """G3e-2's own scope, per the module docstring: a subsection needs
    an LLM call only when (a) it actually has a mount to place relative
    to something (a singleton has no relative-placement decision at all)
    and (b) that placement isn't already grounded in real mount_spec
    data. Imported lazily -- see module docstring on the circular-import
    fix."""
    from agents.hardware_speccer import _parse_mount_spec

    member_ids = (subsection or {}).get("member_ids") or []
    if len(member_ids) < 2:
        return False  # singleton -- nothing to place relative to anything

    anchor_part = parts_by_id.get(subsection.get("subsection_id"))
    if not isinstance(anchor_part, dict):
        return True  # no BOM entry to confirm this is already grounded -- don't skip it

    parsed = _parse_mount_spec(anchor_part.get("mount_spec"))
    # rect/cc patterns already got a real, deterministic reposition from
    # _resize_mount_parts_from_mount_spec(); "thread" (no bracket span to
    # size) did not, so it stays in scope here.
    return not (parsed and parsed.get("pattern") != "thread")


def _generate_relative_placement(part_placement: dict, mount_placement: dict,
                                  subsection_id: str, mount_id: str, key_env: str,
                                  agent_name: str, session_id: str = None, path: str = None,
                                  domain: str = None, violation_issue: str = None) -> dict:
    """The actual generate_text() call, factored out of
    _generate_relative_placement_for_subsection() so both G3e-2's pool
    path (first proposal, no feedback) and G3e-4's repair path
    (regenerate_subsection() below, violation fed back as extra context)
    share one implementation instead of two copies that could drift.

    `violation_issue`: when given (a Level 1->2 collision report's own
    `violation["issue"]` string, per eo/mech_validator.py's
    _check_subsection()), appended to the user turn as feedback -- same
    "feed the violation back as context" contract every other level's
    regenerate_node_fn follows (eo/mech_repair.py's own module
    docstring). None on a first-proposal call (G3e-2's pool path always
    passes None here).
    """
    part_w = part_placement.get("w") or 1
    part_h = part_placement.get("h") or 1
    part_d = part_placement.get("d") or 1

    payload = {
        "part": {"part_id": subsection_id, "w": part_w, "h": part_h, "d": part_d},
        "mount": {
            "part_id": mount_id,
            "w": mount_placement.get("w") or 1,
            "h": mount_placement.get("h") or 1,
            "d": mount_placement.get("d") or 1,
        },
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
        return _parse_relative_offset(raw, part_w, part_h, part_d)
    except RuntimeError:
        # Every provider in the chain failed -- fall back to the same
        # safe "directly below" default _parse_relative_offset() itself
        # falls back to on a bad response, so a quota/outage blip
        # degrades this subsection to a sane default, never a crash of
        # the whole pool.
        return _default_relative_offset(part_h)


def _generate_relative_placement_for_subsection(subsection: dict, placements_by_id: dict,
                                                  key_env: str, worker_id: int,
                                                  session_id: str = None, path: str = None,
                                                  domain: str = None) -> tuple:
    """Runs on one worker thread with one fixed account. Returns
    (subsection_id, mount_id, offset). Mirrors agents/
    mech_primitive_pool.py's own _generate_primitives_for_part() almost
    exactly -- same generate_text()/agent_start/agent_done shape."""
    subsection_id = subsection.get("subsection_id")
    member_ids = subsection.get("member_ids") or []
    mount_id = next((mid for mid in member_ids if mid != subsection_id), None)

    agent_name = f"mech_subsection_{worker_id}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Mech Subsection {worker_id} — {subsection_id}"})
    started = time.monotonic()

    part_placement = placements_by_id.get(subsection_id) or {}
    mount_placement = placements_by_id.get(mount_id) or {}

    offset = _generate_relative_placement(
        part_placement, mount_placement, subsection_id, mount_id, key_env, agent_name,
        session_id=session_id, path=path, domain=domain,
    )

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": f"placed {mount_id} relative to {subsection_id}",
                        "duration_ms": duration_ms})
    return subsection_id, mount_id, offset


def regenerate_subsection(mech: dict, node_id: str, violation: dict, attempt: int,
                           key_override=None, session_id: str = None, path: str = None,
                           domain: str = None) -> None:
    """G3e-4's `regenerate_node_fn` for Level 1->2 -- the exact
    `(mech, node_id, violation, attempt)` -> None shape eo/mech_repair.py's
    run_repair_loop() requires (see that module's docstring), wired to
    THIS pool's own single-subsection generation call
    (_generate_relative_placement() above) instead of duplicating it.

    `node_id` is a `subsection_id` (== the anchor part's own `part_id`,
    per eo/mech_subsections.py's group_into_subsections()) -- resolves
    the subsection's mount sibling straight off `mech["placements"]`
    itself rather than needing the caller to pass the subsection's
    member_ids back in, since `mech` (mutated in place by prior
    generate/repair rounds) is always the current source of truth for
    what this subsection's members actually are.

    Mutates the mount member's x/y/z on `mech["placements"]` in place,
    same "recompute mount position from part position + LLM-proposed
    relative offset" step run()'s own worker-pool loop already performs
    -- this is that same step, just for one node, called from the repair
    loop instead of the initial fan-out.

    A node_id with no "mount_"-prefixed sibling in `mech["placements"]`
    (shouldn't happen -- eo/mech_validator.py's LEVEL_1_2 check only
    ever reports a collision violation for a subsection that HAS two
    members) raises ValueError rather than silently no-op'ing, so a
    caller never mistakes "nothing to regenerate" for "regenerated but
    the fix didn't help" -- run_repair_loop() already treats a raise
    from regenerate_node_fn as a burned, failed attempt (see its own
    docstring's failure-modes section), which is exactly the right
    outcome for this genuinely-shouldn't-happen case too.
    """
    placements = (mech or {}).get("placements") or []
    placements_by_id = {
        p.get("part_id"): p for p in placements
        if isinstance(p, dict) and p.get("part_id")
    }
    part_placement = placements_by_id.get(node_id)
    if not isinstance(part_placement, dict):
        raise ValueError(f"regenerate_subsection: no placement found for node_id={node_id!r}")

    mount_id = f"mount_{node_id}"
    mount_placement = placements_by_id.get(mount_id)
    if not isinstance(mount_placement, dict):
        raise ValueError(
            f"regenerate_subsection: node_id={node_id!r} has no mount sibling "
            f"({mount_id!r}) to reposition"
        )

    agent_name = f"mech_subsection_repair_{node_id}"
    key_env = key_override
    if key_env is None:
        selected = _select_workers_for_role(ROLE_TAG, 1, None, session_id=session_id, agent_name=agent_name)
        key_env = selected[0] if selected else None

    offset = _generate_relative_placement(
        part_placement, mount_placement, node_id, mount_id, key_env, agent_name,
        session_id=session_id, path=path, domain=domain,
        violation_issue=(violation or {}).get("issue"),
    )

    mount_placement["x"] = round((part_placement.get("x") or 0) + offset["x"], 2)
    mount_placement["y"] = round((part_placement.get("y") or 0) + offset["y"], 2)
    mount_placement["z"] = round((part_placement.get("z") or 0) + offset["z"], 2)


def run(spec: dict, parts: list, session_id: str = None, path: str = None,
        domain: str = None, key_override=None, expanded: bool = False) -> dict:
    """G3e-2's entry point -- meant to be called synchronously from
    agents.hardware_speccer.run_hardware_speccer() once G3e-4 wires
    Level 1->2 into the pipeline, the same in-process shape agents/
    mech_primitive_pool.py's own run() already uses for Level 0->1.
    Mutates spec["mech"]["placements"] in place (each in-scope mount
    placement's x/y/z gets recomputed from its part's absolute position
    plus the LLM-proposed relative offset) and returns `spec`.

    key_override: same three-shape contract as agents/
    mech_primitive_pool.py's/code_writers.py's run() -- None (pick own
    workers via _select_workers), a single key_env string (use only that
    account for every subsection), or a list (use exactly those accounts
    as the parallel worker pool).

    No-op (returns `spec` unchanged, no worker-pool call at all) when no
    subsection actually needs this path -- most runs will have most
    mounts already grounded by mount_spec, and there's no reason to
    rank/select workers for an empty job, same short-circuit agents/
    mech_primitive_pool.py's own run() already uses.
    """
    mech = spec.get("mech") or {}
    placements = mech.get("placements")
    if not isinstance(placements, list):
        return spec

    placements_by_id = {
        p.get("part_id"): p for p in placements
        if isinstance(p, dict) and p.get("part_id")
    }
    parts_by_id = {p.get("id"): p for p in parts if isinstance(p, dict)}

    subsections = group_into_subsections(mech)
    targets = [s for s in subsections if _needs_llm_relative_placement(s, parts_by_id)]
    if not targets:
        return spec

    # Fixed pool size, same as agents/mech_primitive_pool.py's/
    # code_writers.py's run() -- NOT scaled to len(targets). Keys are
    # reused round-robin below when there are more subsections than
    # workers.
    worker_count = 8 if expanded else 5
    key_envs = _select_workers_for_role(ROLE_TAG, worker_count, key_override,
                                        session_id=session_id, agent_name=ROLE_TAG)

    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _generate_relative_placement_for_subsection, subsection, placements_by_id,
                key_envs[i % len(key_envs)], (i % len(key_envs)) + 1,
                session_id=session_id, path=path, domain=domain,
            ): subsection
            for i, subsection in enumerate(targets)
        }
        for future in as_completed(futures):
            subsection_id, mount_id, offset = future.result()
            if not mount_id:
                continue
            part_placement = placements_by_id.get(subsection_id)
            mount_placement = placements_by_id.get(mount_id)
            if not isinstance(part_placement, dict) or not isinstance(mount_placement, dict):
                continue
            print(f"    [Mech Subsection Pool] placed {mount_id} relative to {subsection_id}: {offset}")
            mount_placement["x"] = round((part_placement.get("x") or 0) + offset["x"], 2)
            mount_placement["y"] = round((part_placement.get("y") or 0) + offset["y"], 2)
            mount_placement["z"] = round((part_placement.get("z") or 0) + offset["z"], 2)

    return spec


if __name__ == "__main__":
    _demo_spec = {
        "mech": {"placements": [
            {"part_id": "sensor_1", "x": 10, "y": 10, "z": 0, "w": 20, "h": 15, "d": 5},
            {"part_id": "mount_sensor_1", "x": 0, "y": 0, "z": 0, "w": 20, "h": 5, "d": 5},
        ]},
    }
    _demo_parts = [
        {"id": "sensor_1", "name": "Generic Sensor", "category": "sensor"},
        {"id": "mount_sensor_1", "name": "Sensor Bracket", "category": "3D_PRINT/MISC"},
    ]
    result = run(_demo_spec, _demo_parts)
    print(json.dumps(result["mech"]["placements"], indent=2))
