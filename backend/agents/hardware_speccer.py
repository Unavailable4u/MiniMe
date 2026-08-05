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
from memory.bus import read
from utils.llm_client import generate_text
from relay.emitter import emit_event
from eo.errors import MissingDependencyError
from eo import workspace_facts
from agents.structure_architect import _strip_fences  # reuse, don't reimplement

load_dotenv()

# Same reasoning as schema_diagrammer.py's CHAIN: no isolated account
# allocated for this role yet, shared GROQ_API_KEY quota.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY", "timeout": 30},
]


SYSTEM_PROMPT = """You are a hardware bill-of-materials and assembly \
planner. You read a finished (or in-progress) hardware PRD/feasibility \
note and propose the parts list, a wiring graph (which part connects to \
which, and whether that connection carries data, power, or ground), a \
rough physical layout inside an enclosure, and a step-by-step assembly \
sequence grouped into phases (e.g. Fabricate, Wire, Bring-up).

Never invent a part the PRD gives you no reason to include. Every wiring \
edge must reference two part ids that exist in your own parts list. Every \
instruction step's tool_ids/part_ids must reference real entries.

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
    "edges": [{"from": "mcu_1", "to": "sensor_1", "kind": "data"}]
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
"data", "power", "ground". Use short lowercase_with_underscores ids; every \
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
    so this is unchanged from the schema/architecture diagrammers."""
    prd_output = read(f"stage_output:{session_id}:prd_writer", default=None)
    if isinstance(prd_output, dict) and prd_output.get("text"):
        return prd_output["text"]

    intake_output = read(f"stage_output:{session_id}:intake_interviewer", default=None)
    if isinstance(intake_output, dict) and intake_output.get("text"):
        return intake_output["text"]

    raise MissingDependencyError("prd_writer")


def _populate_prices(parts: list) -> list:
    """Looks up and merges pricing for every part via
    agents/part_price_finder.py's find_price(), so the spec returns with
    prices already populated on first generation instead of requiring a
    separate "Refresh prices" click. Takes listings[0] -- NOT a "cheapest"
    selection -- matching api/server.py's existing
    POST /api/workspaces/{ws_id}/parts/refresh-prices endpoint exactly,
    so initial pricing and a later refresh never disagree about which
    vendor a part shows. find_price() itself is cached (eo/price_cache.py,
    5-day TTL), so this is cheap on any *re*-generation of the same parts.
    """
    from agents.part_price_finder import find_price

    for part in parts:
        try:
            result = find_price(part.get("name", ""))
        except Exception:
            # A single vendor-search failure shouldn't fail the whole
            # spec -- same "degrade, don't blow up" spirit as
            # part_price_finder.py's own per-provider try/except.
            continue

        listing = result["listings"][0] if result.get("listings") else None
        if not listing:
            continue
        part["estimated_price_bdt"] = listing.get("price_bdt")
        part["vendor_name"] = listing.get("vendor")
        part["vendor_url"] = listing.get("url")
        part["price_checked_at"] = result.get("checked_at")

    return parts


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

    raw = generate_text(SYSTEM_PROMPT, user_prompt, CHAIN, agent_name="Hardware Speccer",
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

    spec["parts"] = _populate_prices(spec.get("parts", []))

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

    emit_event("device_spec", session_id, agent="hardware_speccer",
               payload={"part_count": len(spec.get("parts", []))})
    return {"text": json.dumps(spec), "spec": spec}


if __name__ == "__main__":
    print(json.dumps(run_hardware_speccer(session_id="local-test", workspace_id="local-test-ws"), indent=2))