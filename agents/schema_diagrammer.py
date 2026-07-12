"""
agents/schema_diagrammer.py — Part 5 §5.3. Proposes a database/entity
schema diagram for a finished (or in-progress) PRD, same split and same
reasoning as agents/architecture_diagrammer.py's own docstring -- read
that module's docstring first if you haven't; this one only documents
what's actually different (the target Mermaid syntax and JSON shape).

Place this file at: agents/schema_diagrammer.py
"""

import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write
from utils.llm_client import generate_text
from relay.emitter import emit_event
from eo.errors import MissingDependencyError
from agents.structure_architect import _mermaid_id, _strip_fences  # reuse, don't reimplement

load_dotenv()

# Same reasoning as architecture_diagrammer.py's CHAIN: no isolated
# account allocated for this role yet, shared GROQ_API_KEY quota.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY", "timeout": 30},
]

SCHEMA_DIAGRAM_KEY = "schema_diagram"

# Mermaid erDiagram crow's-foot relationship tokens, keyed by the plain
# English relationship type the model is asked to choose from -- kept as
# a fixed lookup rather than asking the model to emit crow's-foot syntax
# directly, same "model proposes data, code renders syntax" reasoning as
# every other choice in this split.
RELATIONSHIP_TOKENS = {
    "one_to_one": "||--||",
    "one_to_many": "||--o{",
    "many_to_one": "}o--||",
    "many_to_many": "}o--o{",
}

SYSTEM_PROMPT = """You are a database schema diagrammer for a PRD-to-diagram \
pipeline. You do not write code or make product decisions -- you read a \
finished (or in-progress) PRD and propose a structured entity/relationship \
plan that a separate, deterministic renderer will turn into Mermaid \
erDiagram syntax. You never write Mermaid syntax yourself.

You will be given the PRD body (and, if available, the original intake \
restatement) as context. Identify the real entities the PRD implies (e.g. \
a PRD describing user accounts and saved items implies at least a "User" \
entity and an "Item" entity) and the fields each one plausibly needs -- \
never invent an entity or field the PRD gives you no reason to include, \
and don't pad a thin PRD with entities it doesn't actually call for.

Respond with ONLY valid JSON, no markdown fences, no explanation, in \
exactly this shape:
{
  "entities": [
    {"name": "user", "label": "User", "fields": [
      {"name": "id", "type": "uuid"},
      {"name": "email", "type": "string"}
    ]}
  ],
  "relationships": [
    {"from": "user", "to": "order", "type": "one_to_many", "label": "places"}
  ]
}
"type" must be exactly one of: "one_to_one", "one_to_many", "many_to_one", \
"many_to_many". Every "from"/"to" in "relationships" MUST match an entity \
"name". Use short lowercase_with_underscores for entity/field names; \
"label" is the human-readable name shown on the diagram.
"""


def _read_prd_context(session_id: str) -> str:
    """Identical convention to architecture_diagrammer.py's own
    _read_prd_context() -- see that module's docstring for why this reads
    stage_output:{session_id}:{role} rather than a KEYS[...] entry, and
    why it raises MissingDependencyError("prd_writer") rather than
    guessing from nothing."""
    prd_output = read(f"stage_output:{session_id}:prd_writer", default=None)
    if isinstance(prd_output, dict) and prd_output.get("text"):
        return prd_output["text"]

    intake_output = read(f"stage_output:{session_id}:intake_interviewer", default=None)
    if isinstance(intake_output, dict) and intake_output.get("text"):
        return intake_output["text"]

    raise MissingDependencyError("prd_writer")


def _build_schema_mermaid(plan: dict) -> str:
    """Deterministic renderer targeting erDiagram syntax. _mermaid_id()
    reused from structure_architect.py, same as architecture_diagrammer.py
    -- erDiagram entity names can't contain spaces/punctuation either, so
    the identical sanitizer applies unchanged."""
    lines = ["erDiagram"]
    for e in plan.get("entities", []):
        eid = _mermaid_id(f"e_{e.get('name', '?')}").upper()
        lines.append(f'{eid} {{')
        for f in e.get("fields", []):
            f_type = f.get("type", "string")
            f_name = f.get("name", "field")
            lines.append(f'    {f_type} {f_name}')
        lines.append('}')
    for r in plan.get("relationships", []):
        fid = _mermaid_id(f"e_{r.get('from', '?')}").upper()
        tid = _mermaid_id(f"e_{r.get('to', '?')}").upper()
        token = RELATIONSHIP_TOKENS.get(r.get("type"), "||--o{")
        label = r.get("label", "relates to")
        lines.append(f'{fid} {token} {tid} : "{label}"')
    return "\n".join(lines)


def run_schema_diagrammer(session_id: str = None, tier: int = None,
                           task_text: str = None, domain: str = None) -> dict:
    """Entry point, dispatched by eo/executor.py alongside
    architecture_diagrammer.py's run_architecture_diagrammer() -- same
    signature, same dispatch branch (see the wiring note handed back
    alongside this file)."""
    prd_text = _read_prd_context(session_id)

    user_prompt = f"PRD:\n{prd_text}"
    if task_text:
        user_prompt += f"\n\nOriginal task: {task_text}"

    raw = generate_text(SYSTEM_PROMPT, user_prompt, CHAIN, agent_name="Schema Diagrammer",
                         session_id=session_id, tier=tier, domain=domain)
    cleaned = _strip_fences(raw)

    try:
        plan = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fail safe, same spirit as structure_architect.py's fallbacks.
        plan = {
            "entities": [{"name": "unavailable", "label": "Schema unavailable", "fields": []}],
            "relationships": [],
        }

    plan["mermaid"] = _build_schema_mermaid(plan)
    write(SCHEMA_DIAGRAM_KEY, plan)
    emit_event("schema_diagram", session_id, agent="schema_diagrammer",
               payload={"mermaid": plan["mermaid"]})
    return {"text": plan["mermaid"], "mermaid": plan["mermaid"], "plan": plan}


if __name__ == "__main__":
    print(json.dumps(run_schema_diagrammer(session_id="local-test"), indent=2))