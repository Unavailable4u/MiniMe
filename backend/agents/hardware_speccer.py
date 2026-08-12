"""
agents/hardware_speccer.py — MiniMe Blueprint. Proposes a hardware bill-of-
materials, wiring graph, rough physical layout, and phased assembly
instructions for a finished (or in-progress) hardware PRD/feasibility note,
same split and same reasoning as agents/schema_diagrammer.py's own
docstring -- read that module's docstring (and architecture_diagrammer.py's,
which it points to) first if you haven't; this one only documents what's
actually different.

What's different from schema/architecture_diagrammer.py:
  - No Mermaid rendering step. schema/architecture diagrammers exist because
    the model shouldn't write Mermaid syntax directly; here the model's JSON
    output IS the final artifact the four Blueprint sub-views (Parts /
    Wiring / Mech / Instructions) read directly, slice by slice. There's
    still a "model proposes structured data" discipline (ONLY valid JSON,
    fail-safe on parse errors) -- it's just that nothing downstream
    transforms it into another syntax the way _build_schema_mermaid() does.
  - One extra post-processing step schema/architecture diagrammers don't
    have: after the model proposes parts, each part's price is looked up
    via agents/part_price_finder.py's find_price() and merged in, so the
    spec returns with prices already populated on first generation rather
    than requiring a "Refresh prices" click. find_price() returns multiple
    vendor listings per part (BD_VENDOR_DOMAINS is six sites) -- takes
    listings[0], matching api/server.py's existing
    POST /api/workspaces/{ws_id}/parts/refresh-prices endpoint exactly,
    so a part priced here and a part re-priced later pick the same one.
  - Persistence is NOT a standalone bus key. eo/panel_content.py is for
    opaque pasted text (Mind Map, PRD, Schema, etc. -- one `content`
    string, no structure), which doesn't fit four sub-views with their
    own shapes and (for Instructions) per-step mutation. Instead this
    follows the precedent api/server.py's refresh-prices endpoint already
    set: four keys under eo/workspace_facts.py's per-workspace `custom`
    dict -- custom["parts"], custom["wiring"], custom["mech"],
    custom["instructions"] -- written via the same read-modify-write
    shape that endpoint uses (get_facts -> merge custom -> set_facts),
    so a full spec write here and a parts-only write from that endpoint
    never clobber each other's keys.

Place this file at: agents/hardware_speccer.py
"""

import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read_stage_output_text
from utils.llm_client import generate_text
from relay.emitter import emit_event, EventType
from eo.errors import MissingDependencyError
from eo import workspace_facts
from agents.structure_architect import (
    _strip_fences, _mermaid_id, _sanitize_mermaid_label,
)  # reuse, don't reimplement

load_dotenv()

# FALLBACK_CHAIN: last-resort static chain for the spec-generation call
# below, used ONLY if eo/dynamic_chain.py's build_fallback_chain() comes
# back empty (every registered account excluded/cooling down at once --
# should be very rare). This used to be the ONLY chain this module ever
# tried (one entry, GROQ_API_KEY, shared with part_price_finder's own
# calls and unmonitored/untagged in the registry) -- see
# run_hardware_speccer() below, which now builds a live, quota-ranked,
# multi-provider chain instead.
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY", "timeout": 30},
]


SYSTEM_PROMPT = """You are a hardware bill-of-materials and assembly \
planner. You read a finished (or in-progress) hardware PRD/feasibility \
note and propose the parts list, a wiring graph (which part connects to \
which, over which specific pins/terminals, and whether that connection \
carries data, power, or ground), a rough physical layout inside an \
enclosure, and a step-by-step assembly sequence grouped into phases \
(e.g. Fabricate, Wire, Bring-up).

Never invent a part the PRD gives you no reason to include. Every wiring \
edge must reference two part ids that exist in your own parts list. Every \
instruction step's tool_ids/part_ids must reference real entries.

Every electrical part in "parts" (i.e. every part whose category is \
"mcu", "sensor", "actuator", or "power") MUST have a matching entry in \
"wiring.nodes" and MUST appear in at least one "wiring.edges" entry \
(as either "from" or "to") -- a part that's in the bill of materials but \
never wired to anything is a bug, not a valid answer, even if it's a \
support/passthrough part like a charge controller or voltage regulator. \
The only exception is a purely mechanical, non-electrical part (e.g. \
screws, standoffs, an enclosure shell) -- omit those from "wiring.nodes" \
entirely rather than adding an orphaned node for them.

Every wiring edge must name the actual pin or terminal on each side \
whenever the PRD or the parts involved make that determinable -- e.g. \
"GPIO27" on an ESP32, "SDA"/"SCL" for I2C, "+"/"-" for a battery or \
power rail, "VCC"/"GND" for a generic supply/ground pin, "5V"/"3V3" for \
a regulator's output. Use null for "from_pin"/"to_pin" only when the \
connection genuinely has no single named pin to point to (e.g. an \
abstract "data" link you can't resolve to a specific terminal) -- do not \
leave them null just because it's easier; a wiring diagram whose edges \
don't say which pin connects to which pin is not detailed enough to \
build from.

For the physical layout, you are worse at spatial reasoning than at \
listing parts or wiring edges -- do not attempt precise millimeter \
placement. Propose a rough grid layout only: order parts front-to-back by \
category, with power/MCU parts placed near the enclosure's center and \
sensors placed near the hull edges they would realistically mount at. \
Treat this as "which part roughly goes where," not engineering-grade CAD.

Leave "estimated_price_bdt", "vendor_name", "vendor_url", and \
"price_checked_at" as null for every part -- pricing is looked up \
separately after you respond, not something you should guess at.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{
  "parts": [
    {"id": "mcu_1", "name": "ESP32 DevKit", "category": "mcu",
     "description": "Main microcontroller", "qty": 1,
     "estimated_price_bdt": null, "vendor_name": null, "vendor_url": null,
     "price_checked_at": null}
  ],
  "wiring": {
    "nodes": [{"id": "mcu_1", "label": "ESP32 DevKit", "type": "mcu"}],
    "edges": [{"from": "mcu_1", "to": "sensor_1", "kind": "data",
               "from_pin": "GPIO34", "to_pin": "AOUT"}]
  },
  "mech": {
    "enclosure": {"w": 100, "h": 60, "d": 40},
    "placements": [
      {"part_id": "mcu_1", "x": 0, "y": 0, "z": 0, "w": 25, "h": 25, "d": 5}
    ]
  },
  "instructions": {
    "phases": [
      {"id": "fabricate", "name": "Fabricate",
       "steps": [
         {"id": "step_1", "title": "3D print the enclosure halves",
          "tool_ids": ["3d_printer"], "part_ids": [], "done": false}
       ]}
    ]
  }
}
"category" is one of: "mcu", "sensor", "actuator", "power", "module". \
"type" (wiring nodes) uses the same set. "kind" (wiring edges) is one of: \
"data", "power", "ground". "from_pin"/"to_pin" are short strings naming \
the actual pin/terminal on each side (see above), or null only when \
genuinely not resolvable. Use short lowercase_with_underscores ids; every \
id referenced elsewhere (wiring edges, mech placements, instruction \
tool_ids/part_ids) MUST match an id defined in "parts"/"wiring.nodes".
"""



def _read_prd_context(session_id: str) -> str:
    """Identical convention to schema_diagrammer.py's own
    _read_prd_context() -- see that module's docstring for why this reads
    stage_output:{session_id}:{role} rather than a KEYS[...] entry, and
    why it raises MissingDependencyError("prd_writer") rather than
    guessing from nothing. Hardware PRDs go through the same prd_writer
    role as software PRDs -- there's no separate hardware-specific writer,
    so this is unchanged from the schema/architecture diagrammers.

    Bug fix (2026-08-12): now goes through memory.bus.read_stage_output_text()
    instead of re-reading the raw key and checking isinstance(..., dict)
    inline. That inline check only ever matched an approval-edited stage
    output -- an ordinary completed run writes a plain string (see
    agents/generic_worker.py's own bus_write() call), which this used to
    treat as "nothing here yet" and raise MissingDependencyError even
    when prd_writer had just finished successfully. See
    read_stage_output_text()'s own docstring for the full shape mismatch
    this closes."""
    prd_text = read_stage_output_text(session_id, "prd_writer")
    if prd_text:
        return prd_text

    intake_text = read_stage_output_text(session_id, "intake_interviewer")
    if intake_text:
        return intake_text

    raise MissingDependencyError("prd_writer")


def _populate_prices(parts: list, session_id: str = None) -> list:
    """Looks up and merges pricing for every part via
    agents/part_price_finder.py's find_price(), so the spec returns with
    prices already populated on first generation instead of requiring a
    separate "Refresh prices" click. Takes listings[0] -- NOT a "cheapest"
    selection -- matching api/server.py's existing
    POST /api/workspaces/{ws_id}/parts/refresh-prices endpoint exactly,
    so initial pricing and a later refresh never disagree about which
    vendor a part shows. find_price() itself is cached (eo/price_cache.py,
    5-day TTL), so this is cheap on any *re*-generation of the same parts.

    Bug fix (2026-08-12): this used to call find_price() once per part in
    a plain sequential for-loop, every part fighting over the exact same
    1-2 hardcoded accounts (part_price_finder.py's old module-level
    CHAIN) -- the root cause of hardware_speccer sitting for 2-3 minutes
    on a large parts list before finally rate-limiting itself out. Now
    parallelized with a ThreadPoolExecutor, same pattern
    agents/code_writers.py already uses for module writes: pick up to
    `worker_count` distinct, quota-ranked accounts tagged
    "part_price_finder" (eo/worker_pool.py's shared role_tag-parameterized
    selector -- see eo/registry.py's AGENT_CAPABILITIES), and hand each
    worker thread its OWN find_price(chain_override=...) chain (built via
    eo/dynamic_chain.py's build_fallback_chain_excluding(), so a worker's
    fallback steps also skip whatever its sibling workers are already
    using) instead of every part racing for one shared key.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from agents.part_price_finder import find_price
    from eo.worker_pool import _select_workers
    from eo.dynamic_chain import build_fallback_chain_excluding

    if not parts:
        return parts

    ROLE_TAG = "part_price_finder"
    # Cap workers at the smaller of (parts to look up, accounts tagged for
    # this role) -- no point spinning up more threads than there is work
    # or more than there are distinct accounts to spread it across.
    worker_count = min(len(parts), 8)
    try:
        key_envs = _select_workers(ROLE_TAG, worker_count, session_id=session_id, agent_name=ROLE_TAG)
    except RuntimeError:
        # No account tagged "part_price_finder" at all (e.g. registry not
        # updated yet in this deployment) -- degrade to the single
        # sequential fallback chain rather than crashing outright.
        key_envs = []

    def _price_one(part: dict, key_env: str, worker_id: int) -> dict:
        name = part.get("name", "")
        chain = None
        if key_env:
            # This worker's own chain: start on its assigned account,
            # then fall back to OTHER accounts/providers this specific
            # worker's siblings aren't already using.
            from eo.dynamic_chain import chain_step_for
            chain = [chain_step_for(key_env)] + build_fallback_chain_excluding(
                ROLE_TAG, exclude_keys={key_env})
        try:
            result = find_price(name, chain_override=chain,
                                 agent_name=f"{ROLE_TAG}_{worker_id}")
        except Exception:
            # A single vendor-search failure shouldn't fail the whole
            # spec -- same "degrade, don't blow up" spirit as
            # part_price_finder.py's own per-provider try/except.
            return part
        listing = result["listings"][0] if result.get("listings") else None
        if not listing:
            return part
        part["estimated_price_bdt"] = listing.get("price_bdt")
        part["vendor_name"] = listing.get("vendor")
        part["vendor_url"] = listing.get("url")
        part["price_checked_at"] = result.get("checked_at")
        return part

    if not key_envs:
        # Fallback: no tagged accounts resolved -- still parallelize the
        # I/O (web_search fan-out inside find_price() already helps), but
        # every worker shares find_price()'s own static FALLBACK_CHAIN
        # (chain_override=None) since there's nothing to spread them
        # across account-wise.
        with ThreadPoolExecutor(max_workers=min(len(parts), 4)) as executor:
            futures = [executor.submit(_price_one, part, None, i + 1)
                       for i, part in enumerate(parts)]
            for future in as_completed(futures):
                future.result()
        return parts

    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = [
            executor.submit(_price_one, part, key_envs[i % len(key_envs)], (i % len(key_envs)) + 1)
            for i, part in enumerate(parts)
        ]
        for future in as_completed(futures):
            future.result()

    return parts


# Same color-per-kind convention WiringGraph.jsx's EDGE_COLORS already
# uses for the force-graph view, kept identical here (via Mermaid's
# linkStyle) so the two renderings of the same wiring data never disagree
# about what "power" vs "ground" vs "data" looks like.
_EDGE_COLOR_BY_KIND = {
    "data": "#22c55e",
    "power": "#f59e0b",
    "ground": "#6b7280",
}
_DEFAULT_EDGE_COLOR = "#6b7280"

# Same node-type vocabulary as WiringGraph.jsx's TYPE_COLORS / Blueprint's
# device-spec schema (hardware_speccer.py's own SYSTEM_PROMPT above) --
# one flowchart shape per type so the diagram is visually scannable by
# category (MCU vs sensor vs power, etc.) without reading every label,
# the same idea architecture_diagrammer.py's SHAPE_BY_KIND already uses
# for software components.
_SHAPE_BY_TYPE = {
    "mcu": ("[", "]"),          # rectangle
    "sensor": ("(", ")"),        # rounded
    "actuator": ("([", "])"),    # stadium
    "power": ("[(", ")]"),       # cylinder
    "module": ("[[", "]]"),      # subroutine
}
_TYPE_TITLES = {
    "mcu": "MCU", "sensor": "Sensors", "actuator": "Actuators",
    "power": "Power", "module": "Modules",
}


def _build_wiring_mermaid(spec: dict) -> str:
    """Deterministic renderer -- same "JSON proposes, Python renders"
    contract as architecture_diagrammer.py's _build_architecture_mermaid()
    and schema_diagrammer.py's _build_schema_mermaid(), and reuses their
    exact sanitizer (_sanitize_mermaid_label(), see structure_architect.py
    for why: Bug 5 of the rendering audit) so this inherits "always valid
    Mermaid syntax" for free instead of needing its own validate/retry
    pass. This is the detailed, pin-level wiring diagram Blueprint's
    force-graph view (WiringGraph.jsx) can't express -- see that
    component's own module docstring for why a force-graph is the right
    tool for "what connects to what" but the wrong tool for "which
    specific pin connects to which specific pin."

    Groups nodes into one subgraph per device-spec type (mcu/sensor/
    actuator/power/module) -- same idea the PRD's own (unvalidated,
    Bug 9/10/11) freeform Wiring Overview mermaid block already reached
    for on its own (ESP32/Sensor_Ports/Power subgraphs), just done here
    deterministically off the same structured wiring.nodes/edges data
    Blueprint already trusts, instead of the model writing Mermaid syntax
    directly.

    An edge whose from_pin/to_pin (hardware_speccer.py's SYSTEM_PROMPT,
    step 1 of the wiring-detail fix) are present gets a
    "kind: from_pin->to_pin" label; an edge missing one or both pins
    (an older spec, or a connection the model couldn't resolve to a
    named pin) falls back to just "kind", same as before that schema
    change -- never blocks rendering on missing pin data.

    An edge referencing a node id that isn't in wiring.nodes (shouldn't
    happen per the SYSTEM_PROMPT's own contract, but a model response is
    never fully guaranteed to honor it) is skipped rather than emitted,
    so this never produces Mermaid that references an undeclared node.

    Returns "" for an empty/missing wiring.nodes -- caller decides what
    an empty diagram means for its own UI, this function doesn't guess.
    """
    wiring = spec.get("wiring") or {}
    nodes = wiring.get("nodes") or []
    edges = wiring.get("edges") or []
    if not nodes:
        return ""

    node_ids = {}
    groups = {}
    group_order = []
    for n in nodes:
        node_type = n.get("type") or "module"
        groups.setdefault(node_type, []).append(n)
        if node_type not in group_order:
            group_order.append(node_type)

    lines = ["flowchart LR"]
    for node_type in group_order:
        title = _TYPE_TITLES.get(node_type, node_type.title())
        lines.append(f'    subgraph {_mermaid_id(f"grp_{node_type}")}["{title}"]')
        open_b, close_b = _SHAPE_BY_TYPE.get(node_type, ("[", "]"))
        for n in groups[node_type]:
            raw_id = n.get("id") or "?"
            nid = _mermaid_id(f"n_{raw_id}")
            node_ids[raw_id] = nid
            label = _sanitize_mermaid_label(n.get("label") or raw_id)
            lines.append(f'        {nid}{open_b}"{label}"{close_b}')
        lines.append("    end")

    style_lines = []
    edge_index = 0
    for e in edges:
        from_id = node_ids.get(e.get("from"))
        to_id = node_ids.get(e.get("to"))
        if not from_id or not to_id:
            continue  # references a node not in wiring.nodes -- skip, don't emit a dangling reference

        kind = e.get("kind") or "link"
        from_pin = (e.get("from_pin") or "").strip()
        to_pin = (e.get("to_pin") or "").strip()
        if from_pin and to_pin:
            raw_label = f"{kind}: {from_pin}->{to_pin}"
        elif from_pin or to_pin:
            raw_label = f"{kind}: {from_pin or to_pin}"
        else:
            raw_label = kind
        # Quoted, not bare, edge label: an unquoted Mermaid pipe-label
        # (|...|) can't safely contain parentheses -- exactly the
        # "ESP[ESP32 5V (Vin)]"-style parse error from the rendering
        # audit's Bug 5, but on an edge label instead of a node label.
        # Pin names routinely contain parens (e.g. "GPIO21 (SDA)"), so
        # quoting is required here even though the node labels above get
        # away without it.
        label = _sanitize_mermaid_label(raw_label, fallback=kind)

        lines.append(f'    {from_id} -->|"{label}"| {to_id}')
        color = _EDGE_COLOR_BY_KIND.get(kind, _DEFAULT_EDGE_COLOR)
        style_lines.append(f'    linkStyle {edge_index} stroke:{color},color:{color}')
        edge_index += 1

    lines.extend(style_lines)
    return "\n".join(lines)


def run_hardware_speccer(session_id: str = None, tier: int = None,
                          task_text: str = None, domain: str = None,
                          workspace_id: str = None) -> dict:
    """Entry point, dispatched by eo/executor.py alongside schema/
    architecture_diagrammer.py's run_*() functions -- same signature plus
    workspace_id, since the spec is written into that workspace's
    workspace_facts.custom (see module docstring), not a standalone bus
    key. Raises MissingDependencyError("prd_writer") via
    _read_prd_context() if there's nothing to spec yet -- see that
    function's docstring. Raises ValueError if workspace_id is missing --
    same requirement workspace_facts.set_facts() itself enforces, checked
    here first so the (slower, costs tokens) generation call never runs
    for a request that was always going to fail to save."""
    if not workspace_id:
        raise ValueError("workspace_id is required")

    prd_text = _read_prd_context(session_id)

    user_prompt = f"PRD:\n{prd_text}"
    if task_text:
        user_prompt += f"\n\nOriginal task: {task_text}"

    # Bug fix (2026-08-12): deferred import -- see eo/dynamic_chain.py's
    # module docstring for why this can't be a module-level import
    # (eo.registry imports this module at load time; eo.dynamic_chain
    # imports eo.registry at ITS module level). Quota-ranked,
    # cooldown-aware, spread across providers -- replaces the old
    # single-entry CHAIN that had nothing to fall back to.
    from eo.dynamic_chain import build_fallback_chain
    chain = build_fallback_chain("hardware_speccer") or FALLBACK_CHAIN

    raw = generate_text(SYSTEM_PROMPT, user_prompt, chain, agent_name="Hardware Speccer",
                         session_id=session_id, tier=tier, domain=domain)
    cleaned = _strip_fences(raw)

    try:
        spec = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fail safe, same spirit as schema/architecture_diagrammer.py's
        # fallbacks: a minimal valid shape naming the failure, rather than
        # nothing at all -- keeps all four Blueprint sub-views renderable.
        spec = {
            "parts": [{"id": "unavailable", "name": "Spec unavailable", "category": "module",
                       "description": "", "qty": 1, "estimated_price_bdt": None,
                       "vendor_name": None, "vendor_url": None, "price_checked_at": None}],
            "wiring": {"nodes": [], "edges": []},
            "mech": {"enclosure": {"w": 0, "h": 0, "d": 0}, "placements": []},
            "instructions": {"phases": []},
        }

    spec["parts"] = _populate_prices(spec.get("parts", []), session_id=session_id)

    # Step 3 of the wiring-detail fix: deterministically render the
    # pin-level flowchart off spec["wiring"] (nodes/edges, now carrying
    # from_pin/to_pin per step 1's schema change) and fold it back in as
    # wiring.mermaid, right alongside the nodes/edges WiringGraph.jsx's
    # force-graph view already reads -- one wiring object, two renderings
    # of it, never two independently-generated diagrams that could
    # disagree (that's exactly the problem with the PRD's own separate,
    # unvalidated Wiring Overview block -- Bug 9/10/11 of the rendering
    # audit). "" (empty wiring.nodes) is a valid, honest value here, not
    # an error -- a caller checks for it the same way it already checks
    # for an empty nodes/edges list.
    if "wiring" not in spec or not isinstance(spec["wiring"], dict):
        spec["wiring"] = {"nodes": [], "edges": []}
    spec["wiring"]["mermaid"] = _build_wiring_mermaid(spec)

    # Same read-modify-write shape api/server.py's refresh-prices endpoint
    # already uses for custom["parts"] alone -- read the whole facts
    # object, update only this spec's four custom keys, write it back, so
    # unrelated custom entries (e.g. deploy_target) are never touched.
    facts = workspace_facts.get_facts(workspace_id)
    custom = dict(facts.get("custom") or {})
    custom["parts"] = spec.get("parts", [])
    custom["wiring"] = spec.get("wiring", {})
    custom["mech"] = spec.get("mech", {})
    custom["instructions"] = spec.get("instructions", {})
    workspace_facts.set_facts(workspace_id, {"custom": custom})
    print(f"  [hardware_speccer] wrote device spec to workspace_id={workspace_id!r} "
          f"(session_id={session_id!r}, {len(custom['parts'])} parts)")

    workspace_facts.record_section_entries(
      workspace_id,
      "hardware",
      [
        {
          "key": part.get("id") or part.get("name") or f"part_{index}",
          "title": part.get("name") or part.get("id") or f"Part {index + 1}",
          "summary": f"{part.get('category') or 'module'} ×{part.get('qty') or 1}",
          "data": part,
        }
        for index, part in enumerate(spec.get("parts", []))
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="parts",
    )
    workspace_facts.record_section_entries(
      workspace_id,
      "components",
      [
        {
          "key": node.get("id") or f"node_{index}",
          "title": node.get("label") or node.get("id") or f"Node {index + 1}",
          "summary": node.get("type") or node.get("kind") or "component",
          "data": node,
        }
        for index, node in enumerate(spec.get("wiring", {}).get("nodes", []))
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="wiring_nodes",
    )
    workspace_facts.record_section_entries(
      workspace_id,
      "connections",
      [
        {
          "key": f"{edge.get('from') or 'from'}->{edge.get('to') or 'to'}:{edge.get('kind') or 'link'}",
          "title": f"{edge.get('from') or '?'} -> {edge.get('to') or '?'}",
          "summary": edge.get("kind") or "connection",
          "data": edge,
        }
        for edge in spec.get("wiring", {}).get("edges", [])
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="wiring_edges",
    )
    workspace_facts.record_section_entries(
      workspace_id,
      "instructions",
      [
        {
          "key": phase.get("id") or phase.get("name") or f"phase_{index}",
          "title": phase.get("name") or phase.get("id") or f"Phase {index + 1}",
          "summary": f"{len(phase.get('steps', []))} step(s)",
          "data": phase,
        }
        for index, phase in enumerate(spec.get("instructions", {}).get("phases", []))
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="instructions",
    )
    workspace_facts.record_section_entries(
      workspace_id,
      "instructions",
      [
        {
          "key": step.get("id") or f"step_{phase_index}_{step_index}",
          "title": step.get("title") or step.get("id") or f"Step {step_index + 1}",
          "summary": "done" if step.get("done") else "pending",
          "data": {"phase_id": phase.get("id"), **step},
        }
        for phase_index, phase in enumerate(spec.get("instructions", {}).get("phases", []))
        for step_index, step in enumerate(phase.get("steps", []))
      ],
      source="hardware_speccer",
      source_ref=session_id,
      event="instruction_steps",
    )

    emit_event(EventType.DEVICE_SPEC, session_id, agent="hardware_speccer",
               payload={"part_count": len(spec.get("parts", []))})
    return {"text": json.dumps(spec), "spec": spec}


if __name__ == "__main__":
    print(json.dumps(run_hardware_speccer(session_id="local-test", workspace_id="local-test-ws"), indent=2))