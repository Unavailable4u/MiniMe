"""
agents/mech_primitive_pool.py — G3b (Master Guide, "G3/G4. Hierarchical
parallel build + validate", Level 0->1 primitive composition, LLM path):
fans one LLM call out per part via eo/worker_pool.py, same worker-pool
pattern agents/code_writers.py's Code Writer Pool and agents/
content_adapter_pool.py already use -- but only for the parts G3a's
deterministic path (agents/hardware_speccer.py's
_apply_primitive_composition()) left completely uncovered: an electrical
part (mcu/sensor/actuator/power/module -- the same _ELECTRICAL_CATEGORIES
set hardware_speccer.py already uses for its "must have a wiring node"
rule) with no `dimensions_mm` at all, i.e. neither G1a's curated table
nor G1b's DigiKey/Mouser lookup matched it. A mechanical/enclosure part
(housing, lid, mount, fastener) is never in scope here -- those are
already box-shaped in reality and G3a's own default already covers them
correctly with no LLM call needed; spending a worker-pool call composing
"primitives" for a housing would be guessing at nothing.

Unlike content_adapter_pool.py, this module isn't a standalone top-level
agent dispatched by eo/executor.py and reading its input off a
memory-bus key -- it's one more mutator step inside
agents/hardware_speccer.py's own run_hardware_speccer() pipeline, called
synchronously right after G3a's _apply_primitive_composition(), the same
in-process shape every other G-series step in that module already uses
(_ensure_electrical_placements(), _apply_placement_shapes(), etc.). So
run() takes `spec`/`parts` directly as arguments and mutates
spec["mech"]["placements"] in place, exactly like its deterministic
siblings, rather than reading/writing its own KEYS[...] entry.

Worker selection reuses eo/worker_pool.py's shared, role_tag-parameterized
helper with role_tag="mech_primitive" -- registered onto the same
Cerebras Code-Writer-Pool accounts "content_writer" already shares
(eo/registry.py), not a new pool of keys. The quota-aware fairness
ranking already spreads load across whatever's least-used regardless of
which tag(s) an account carries, so a run doing content adaptation and
mech primitive generation at the same time still gets fair rotation
across the same 8 accounts.

Deferred imports of agents.hardware_speccer inside the functions below
(never at module level): hardware_speccer.py itself calls this module's
run() from inside run_hardware_speccer(), so a module-level
`import agents.hardware_speccer` here would be circular. Same fix, same
justification eo/dynamic_chain.py's own module docstring already
documents for the reverse-direction case.
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

ROLE_TAG = "mech_primitive"

# Master Guide's own cap for this LLM path ("1-4 primitives"), looser
# than G3a's deterministic templates (which top out at 2, the cone's
# shaft+dome) since a part composed from scratch with no matched
# dimensions gets a little more room to read as recognizable.
_MAX_PRIMITIVES = 4
_MIN_SIZE_MM = 0.5

_VALID_SHAPES = {"box", "cylinder", "cone"}
_VALID_COLOR_ROLES = {"primary", "accent"}

SYSTEM_PROMPT = """You are a mechanical CAD primitive composer. You are \
given ONE electrical part (name, category, and its already-decided \
bounding box w/h/d in millimeters -- you do not choose the bounding \
box, only what goes inside it) that has no real measured dimensions on \
file, only an estimated bounding box. Compose it out of 1 to 4 simple \
geometric primitives -- box, cylinder, or cone -- so it reads as a \
recognizable version of that part rather than a plain box, whenever a \
real shape is a reasonable guess.

Rules:
- Use a cylinder-based composition for round parts (motors, batteries, \
a buzzer's body, capacitors).
- Use a box-based composition for flat/rectangular parts (boards, \
modules, most sensors).
- Use a cone (stacked on a short box "shaft") for parts with a domed or \
tapered tip (buttons, speaker cones, antenna tips).
- If none of the above clearly fits, use a single box -- box is always \
a safe default; never force a cylinder or cone onto a part that isn't \
actually round or tipped.
- Every primitive's "offset" + "size" MUST fit entirely inside the \
given w/h/d bounding box -- "offset" is the primitive's own corner \
position (0,0,0 is the bounding box's own corner), "size" is its own \
w/h/d, and offset+size on every axis must be <= the bounding box's own \
w/h/d on that axis. Never propose a primitive that pokes outside the \
given bounding box.
- "color_role" is "primary" for the part's main body, "accent" for a \
small secondary/detail primitive (e.g. a shaft, a lens, a lead) -- most \
parts are entirely "primary".
- "rotation" is {"x": 0, "y": 0, "z": 0} unless the part's real-world \
orientation clearly calls for a primitive rotated on one axis.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{"primitives": [
  {"offset": {"x": 0, "y": 0, "z": 0}, "size": {"w": 10, "h": 10, "d": 10}, \
"rotation": {"x": 0, "y": 0, "z": 0}, "shape": "cylinder", "color_role": "primary"}
]}
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


def _clamp_primitive(raw, w: float, h: float, d: float):
    """Validates and clamps one LLM-proposed primitive against its
    part's own w/h/d bounding box -- same "clamped to its own
    footprint" safety net agents/hardware_speccer.py's
    _mount_hole_primitives() already applies to mount-hole primitives,
    reused here so an LLM overshoot (a size or offset that pokes past
    the bounding box) gets pulled back inside rather than either
    crashing this step or shipping a primitive FreeCAD's later
    validation pass (G3c) would just flag anyway. Returns None if
    `raw` isn't even shaped like a primitive (missing offset/size) --
    the caller drops it rather than guessing a replacement for
    genuinely malformed output."""
    if not isinstance(raw, dict):
        return None
    offset = raw.get("offset")
    size = raw.get("size")
    if not isinstance(offset, dict) or not isinstance(size, dict):
        return None

    bounds = {"x": w, "y": h, "z": d}
    size_keys = {"x": "w", "y": "h", "z": "d"}
    clamped_offset, clamped_size = {}, {}
    for axis, bound in bounds.items():
        bound = max(bound, _MIN_SIZE_MM)
        ox = min(max(_as_float(offset.get(axis)), 0), bound - _MIN_SIZE_MM)
        clamped_offset[axis] = round(ox, 2)
        size_key = size_keys[axis]
        remaining = max(bound - ox, _MIN_SIZE_MM)
        sz = min(max(_as_float(size.get(size_key)), _MIN_SIZE_MM), remaining)
        clamped_size[size_key] = round(sz, 2)

    shape = raw.get("shape")
    if shape not in _VALID_SHAPES:
        shape = "box"
    color_role = raw.get("color_role")
    if color_role not in _VALID_COLOR_ROLES:
        color_role = "primary"
    rotation = raw.get("rotation")
    if not isinstance(rotation, dict):
        rotation = {"x": 0, "y": 0, "z": 0}
    else:
        rotation = {axis: _as_float(rotation.get(axis)) for axis in ("x", "y", "z")}

    return {
        "offset": clamped_offset,
        "size": clamped_size,
        "rotation": rotation,
        "shape": shape,
        "color_role": color_role,
    }


def _parse_primitives(raw_text: str, w: float, h: float, d: float) -> list:
    """Parses/clamps the LLM's response into a valid primitives list,
    falling back to a single full-bounding-box box primitive (the same
    shape agents.hardware_speccer._box_primitive_template() produces --
    imported lazily, see module docstring on the circular-import fix)
    on any parse failure, empty list, or a response that clamps down to
    nothing usable. Never raises and never returns an empty list --
    every part this pool runs on ends up with at least one primitive,
    same fail-safe convention the rest of this pipeline uses (empty-
    but-valid or a safe default, never a hard crash)."""
    from agents.hardware_speccer import _box_primitive_template

    try:
        parsed = json.loads(_strip_fences(raw_text))
        raw_primitives = parsed.get("primitives")
        if not isinstance(raw_primitives, list) or not raw_primitives:
            raise ValueError("no primitives in response")
    except (json.JSONDecodeError, AttributeError, ValueError):
        return _box_primitive_template(w, h, d)

    clamped = []
    for raw in raw_primitives[:_MAX_PRIMITIVES]:
        primitive = _clamp_primitive(raw, w, h, d)
        if primitive:
            clamped.append(primitive)

    return clamped or _box_primitive_template(w, h, d)


def _generate_primitives_for_part(placement: dict, part: dict, key_env: str,
                                   worker_id: int, session_id: str = None,
                                   path: str = None, domain: str = None) -> tuple:
    """Runs on one worker thread with one fixed account. Returns
    (part_id, primitives). Mirrors content_adapter_pool.py's
    _write_one_variant()/code_writers.py's _write_one_module() almost
    exactly -- same generate_text()/agent_start/agent_done shape."""
    part_id = placement.get("part_id")
    agent_name = f"mech_primitive_{worker_id}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Mech Primitive {worker_id} — {part_id}"})
    started = time.monotonic()

    w = placement.get("w") or 1
    h = placement.get("h") or 1
    d = placement.get("d") or 1

    user_content = json.dumps({
        "part_id": part_id,
        "name": part.get("name"),
        "generic_name": part.get("generic_name"),
        "category": part.get("category"),
        "description": part.get("description"),
        "bounding_box_mm": {"w": w, "h": h, "d": d},
    })

    chain = [{"provider": "cerebras", "model": "gpt-oss-120b", "key_env": key_env}]

    try:
        raw = generate_text(
            SYSTEM_PROMPT, user_content, chain,
            agent_name=agent_name, session_id=session_id, path=path, domain=domain,
        )
        primitives = _parse_primitives(raw, w, h, d)
    except RuntimeError:
        # Every provider in the chain failed -- fall back to the same
        # safe single-box shape _parse_primitives() itself falls back
        # to on a bad response, so a quota/outage blip degrades this
        # part to "plain box," never a crash of the whole pool.
        from agents.hardware_speccer import _box_primitive_template
        primitives = _box_primitive_template(w, h, d)

    duration_ms = int((time.monotonic() - started) * 1000)
    summary = f"{len(primitives)} primitive(s) for {part_id}"
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": summary, "duration_ms": duration_ms})
    return part_id, primitives


def _needs_llm_primitives(placement: dict, parts_by_id: dict) -> bool:
    """G3b's own scope, per the Master Guide: an electrical part
    (agents.hardware_speccer._ELECTRICAL_CATEGORIES -- the same set
    that module's own wiring-node rule uses) whose G3a pass left with
    no `primitives` at all, because it had no `dimensions_mm` from
    either G1a or G1b. A part that already has `primitives` (G3a
    covered it) or isn't electrical (a housing/lid/mount/fastener,
    already box-shaped in reality) is out of scope here. Imported
    lazily -- see module docstring on the circular-import fix."""
    from agents.hardware_speccer import _ELECTRICAL_CATEGORIES

    if not isinstance(placement, dict) or placement.get("primitives"):
        return False
    part = parts_by_id.get(placement.get("part_id"))
    if not isinstance(part, dict):
        return False
    if part.get("dimensions_mm"):
        return False
    return part.get("category") in _ELECTRICAL_CATEGORIES


def run(spec: dict, parts: list, session_id: str = None, path: str = None,
        domain: str = None, key_override=None, expanded: bool = False) -> dict:
    """G3b's entry point -- called synchronously from
    agents.hardware_speccer.run_hardware_speccer(), right after G3a's
    _apply_primitive_composition(spec, spec["parts"]). Mutates
    spec["mech"]["placements"] in place (each covered entry gains a
    `primitives` list) and returns `spec`, matching the mutate-and-
    return convention this call site already expects from its other
    G-series steps.

    key_override: same three-shape contract as code_writers.py's/
    content_adapter_pool.py's run() -- None (pick own workers via
    _select_workers), a single key_env string (use only that account
    for every part), or a list (use exactly those accounts as the
    parallel worker pool).

    No-op (returns `spec` unchanged, no worker-pool call at all) when
    nothing needs this path -- most runs will have zero uncovered
    parts once G0/G1's curated table and DigiKey/Mouser lookups have
    matched everything they can, and there's no reason to rank/select
    workers for an empty job.
    """
    placements = (spec.get("mech") or {}).get("placements")
    if not isinstance(placements, list):
        return spec

    parts_by_id = {p.get("id"): p for p in parts if isinstance(p, dict)}
    uncovered = [p for p in placements if _needs_llm_primitives(p, parts_by_id)]
    if not uncovered:
        return spec

    # Fixed pool size, same as code_writers.py's/content_adapter_pool.py's
    # run() -- NOT scaled to len(uncovered). Keys are reused round-robin
    # below when there are more uncovered parts than workers.
    worker_count = 8 if expanded else 5
    key_envs = _select_workers_for_role(ROLE_TAG, worker_count, key_override,
                                        session_id=session_id, agent_name=ROLE_TAG)

    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _generate_primitives_for_part, placement, parts_by_id[placement["part_id"]],
                key_envs[i % len(key_envs)], (i % len(key_envs)) + 1,
                session_id=session_id, path=path, domain=domain,
            ): placement
            for i, placement in enumerate(uncovered)
        }
        for future in as_completed(futures):
            part_id, primitives = future.result()
            print(f"    [Mech Primitive Pool] composed {part_id}: {len(primitives)} primitive(s)")
            for placement in placements:
                if placement.get("part_id") == part_id:
                    placement["primitives"] = primitives
                    break

    return spec


if __name__ == "__main__":
    _demo_spec = {
        "mech": {"placements": [{"part_id": "motor_1", "x": 0, "y": 0, "z": 0, "w": 28, "h": 19, "d": 19}]},
    }
    _demo_parts = [{"id": "motor_1", "name": "Generic DC Motor", "generic_name": "DC motor",
                     "category": "actuator", "description": "Small brushed DC motor"}]
    result = run(_demo_spec, _demo_parts)
    print(json.dumps(result["mech"]["placements"], indent=2))
