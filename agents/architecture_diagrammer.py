"""
agents/architecture_diagrammer.py — Part 5 §5.3. Proposes a system-level
architecture diagram (REST API layer, database, queue, external service —
whatever the PRD implies) for a finished (or in-progress) PRD produced by
the "plan" domain.

Copies agents/structure_architect.py's proven split exactly, and is
deliberately a NEW sibling module rather than a third mode bolted onto
that file — see Part 5 §5.3's own reasoning for why structure_architect.py
stays untouched. What's copied on purpose:
  - The JSON-proposes / code-renders split: the LLM never emits Mermaid
    text directly (that's where hallucinated/broken diagram syntax comes
    from) — it proposes a structured plan, and a pure-Python function
    deterministically renders that into valid Mermaid syntax.
  - _mermaid_id() is imported directly from structure_architect.py, not
    reimplemented — one sanitizer, one place it can go wrong.
  - The JSON-parse-fails-safe fallback pattern.
  - The MissingDependencyError self-heal contract: if prd_writer hasn't
    run yet, this raises MissingDependencyError("prd_writer") instead of
    guessing from task_text alone, so eo/executor.py's adaptive-path
    self-heal branch can splice prd_writer in first and retry (exactly
    the mechanism extraction_table_builder.py already relies on for
    academic_search — see eo/executor.py's own comment on that case).

Two diagram shapes, model's choice (see SYSTEM_PROMPT):
  - "component" (the default/common case): a graph TD/flowchart of
    components and the edges between them.
  - "sequence": a sequenceDiagram, for content that reads better as a
    request/response flow than a static component graph.

Place this file at: agents/architecture_diagrammer.py
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
from agents.structure_architect import _mermaid_id, _strip_fences  # reuse, don't reimplement

load_dotenv()

# No isolated account has been allocated for this role yet (unlike
# structure_architect.py's GROQ_API_KEY_9) -- one ordinary generic-shaped
# call, same cost category noted in Part 5 §5.8, so it draws from the
# shared GROQ_API_KEY quota rather than inventing an unused env var.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY", "timeout": 30},
]

ARCHITECTURE_DIAGRAM_KEY = "architecture_diagram"

SYSTEM_PROMPT = """You are a software architecture diagrammer for a PRD-to-\
diagram pipeline. You do not write code or make product decisions -- you \
read a finished (or in-progress) PRD and propose a structured plan for a \
system architecture diagram that a separate, deterministic renderer will \
turn into Mermaid syntax. You never write Mermaid syntax yourself.

You will be given the PRD body (and, if available, the original intake \
restatement) as context. Decide what architecture the PRD actually implies \
-- a REST API layer, a database, a queue, an external service, a frontend \
client, whatever genuinely fits -- never invent a component the PRD gives \
you no reason to include, and never omit one it clearly requires (e.g. a \
PRD that describes user accounts implies some kind of auth/user store).

Choose ONE diagram_type:
- "component": the common case -- a static graph of components and the \
labeled edges between them (e.g. "Client" -->|HTTP request| "API Server").
- "sequence": use this INSTEAD of "component" only when the PRD's content \
is fundamentally about a request/response flow over time (e.g. a specific \
multi-step user action moving through several services) -- a sequence \
diagram communicates that better than a static graph.

Respond with ONLY valid JSON, no markdown fences, no explanation.

For diagram_type "component", use exactly this shape:
{
  "diagram_type": "component",
  "components": [
    {"id": "api_server", "label": "API Server", "kind": "service"},
    {"id": "db", "label": "PostgreSQL Database", "kind": "database"}
  ],
  "edges": [
    {"from": "api_server", "to": "db", "label": "reads/writes"}
  ]
}
"kind" is one of: "service", "database", "queue", "external", "client", \
"other" -- used only to pick a node shape, keep it accurate.

For diagram_type "sequence", use exactly this shape:
{
  "diagram_type": "sequence",
  "participants": [
    {"id": "client", "label": "Client"},
    {"id": "api", "label": "API Server"}
  ],
  "messages": [
    {"from": "client", "to": "api", "label": "submits request"}
  ]
}

Every id referenced in "edges"/"messages" MUST appear in "components"/\
"participants". Use short lowercase_with_underscores ids; labels are the \
human-readable text shown on the diagram.
"""


def _read_prd_context(session_id: str) -> str:
    """Reads prd_writer's generic_worker output straight off the bus at
    its stage_output:{session_id}:{role} key (see eo/executor.py's own
    comment on this convention) -- NOT a KEYS[...] entry, since prd_writer
    is a generic_worker role with no dedicated memory-bus key of its own.
    Falls back to intake_interviewer's restatement if prd_writer somehow
    hasn't produced anything yet, and raises MissingDependencyError if
    NEITHER is available, letting the adaptive-path self-heal branch
    splice prd_writer into the plan and retry rather than this module
    silently diagramming from nothing."""
    prd_output = read(f"stage_output:{session_id}:prd_writer", default=None)
    if isinstance(prd_output, dict) and prd_output.get("text"):
        return prd_output["text"]

    intake_output = read(f"stage_output:{session_id}:intake_interviewer", default=None)
    if isinstance(intake_output, dict) and intake_output.get("text"):
        return intake_output["text"]

    raise MissingDependencyError("prd_writer")


def _build_architecture_mermaid(plan: dict) -> str:
    """Deterministic renderer -- structure_architect.py's _build_mermaid()
    sibling, same "the model proposes data, code renders syntax" contract.
    _mermaid_id() is imported from structure_architect.py directly."""
    diagram_type = plan.get("diagram_type", "component")

    if diagram_type == "sequence":
        lines = ["sequenceDiagram"]
        for p in plan.get("participants", []):
            pid = _mermaid_id(f"p_{p.get('id', '?')}")
            label = p.get("label", p.get("id", "?"))
            lines.append(f'participant {pid} as {label}')
        for m in plan.get("messages", []):
            fid = _mermaid_id(f"p_{m.get('from', '?')}")
            tid = _mermaid_id(f"p_{m.get('to', '?')}")
            label = m.get("label", "")
            lines.append(f'{fid}->>{tid}: {label}')
        return "\n".join(lines)

    # "component" (default)
    SHAPE_BY_KIND = {
        "database": ("[(", ")]"),
        "queue": ("([", "])"),
        "external": ("{{", "}}"),
        "client": ("([", "])"),
        "service": ("[", "]"),
        "other": ("[", "]"),
    }
    lines = ["graph TD"]
    for c in plan.get("components", []):
        cid = _mermaid_id(f"c_{c.get('id', '?')}")
        label = c.get("label", c.get("id", "?"))
        open_b, close_b = SHAPE_BY_KIND.get(c.get("kind", "other"), ("[", "]"))
        lines.append(f'{cid}{open_b}"{label}"{close_b}')
    for e in plan.get("edges", []):
        fid = _mermaid_id(f"c_{e.get('from', '?')}")
        tid = _mermaid_id(f"c_{e.get('to', '?')}")
        label = e.get("label", "")
        if label:
            lines.append(f'{fid} -->|{label}| {tid}')
        else:
            lines.append(f'{fid} --> {tid}')
    return "\n".join(lines)


def run_architecture_diagrammer(session_id: str = None, tier: int = None,
                                 task_text: str = None, domain: str = None) -> dict:
    """Entry point, dispatched by eo/executor.py exactly like
    structure_architect.py's run_structure_architect(). Raises
    MissingDependencyError("prd_writer") via _read_prd_context() if
    there's nothing to diagram yet -- see that function's docstring."""
    prd_text = _read_prd_context(session_id)

    user_prompt = f"PRD:\n{prd_text}"
    if task_text:
        user_prompt += f"\n\nOriginal task: {task_text}"

    raw = generate_text(SYSTEM_PROMPT, user_prompt, CHAIN, agent_name="Architecture Diagrammer",
                         session_id=session_id, tier=tier, domain=domain)
    cleaned = _strip_fences(raw)

    try:
        plan = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fail safe, same spirit as structure_architect.py's fallbacks:
        # a single node naming the failure, rather than nothing at all.
        plan = {
            "diagram_type": "component",
            "components": [{"id": "unavailable", "label": "Diagram unavailable", "kind": "other"}],
            "edges": [],
        }

    plan["mermaid"] = _build_architecture_mermaid(plan)
    from memory.bus import write
    write(ARCHITECTURE_DIAGRAM_KEY, plan)
    emit_event("architecture_diagram", session_id, agent="architecture_diagrammer",
               payload={"mermaid": plan["mermaid"]})
    return {"text": plan["mermaid"], "mermaid": plan["mermaid"], "plan": plan}


if __name__ == "__main__":
    print(json.dumps(run_architecture_diagrammer(session_id="local-test"), indent=2))