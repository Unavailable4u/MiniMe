"""
agents/mind_mapper.py — Notebooks integration guide §6.5 (Phase 2): Mind
Map's new content-reading pass.

eo/registry.py already has a working, hand-written "mapper" role brief
that outputs real Mermaid syntax in a fenced ```mermaid block -- it just
had no caller before this (Mind Map's subtab was a pure paste box, per
guide §1). Single-hire generic_worker call, same shape as
agents/note_taker.py / agents/fact_detector.py / agents/study_generator.py
-- guide §2's exception for Mind Map specifically calls this out as
closer in shape to a reasoning role (reads context, produces structured
output) than to the deterministic Clusters/Backlinks pattern, but still
not a staffed Panel run: one role, no handoffs.

Returns bare Mermaid source (the fenced ```mermaid wrapper stripped off),
not the role's raw fenced-block text -- MermaidDiagram.jsx (and
NotebooksTab.jsx's MindMapView, which passes panel_content's saved
"mindmap" content straight to it as `mermaidText`) expects the bare
source, the same shape a user's manual paste used to provide. Saving
that content is api/server.py's job (needs the requesting user's id,
which this module has no business knowing about), same separation
agents/study_generator.py already keeps from its own caller.

Place this file at: agents/mind_mapper.py
"""
import os
import sys
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.knowledge_graph import list_nodes

_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*(.*?)\s*```", re.DOTALL)

# Same per-source truncation reasoning as agents/fact_detector.py and
# agents/study_generator.py.
MAX_CONTENT_CHARS_PER_SOURCE = 8000


def _context_for(nodes: list[dict]) -> str:
    parts = []
    for n in nodes:
        title = n.get("title") or n.get("node_id")
        content = (n.get("content") or "").strip()[:MAX_CONTENT_CHARS_PER_SOURCE]
        if not content:
            continue
        parts.append(f"--- {title} ---\n{content}")
    return "\n\n".join(parts)


def generate_mindmap(workspace_id: str, source_node_ids: list[str] | None = None) -> str:
    """Returns bare Mermaid source built from the given sources (or
    every source in the workspace when `source_node_ids` is falsy --
    "blank scope = whole notebook," same convention as
    agents/fact_detector.py and agents/study_generator.py).

    Raises LookupError if the resolved scope has zero readable source
    content, so the caller can turn that into a clear per-branch error
    instead of silently saving an empty diagram.
    """
    nodes = list_nodes(workspace_id)
    if source_node_ids:
        wanted = set(source_node_ids)
        nodes = [n for n in nodes if n.get("node_id") in wanted or n.get("vector_id") in wanted]

    context = _context_for(nodes)
    if not context:
        raise LookupError("no readable source content in scope")

    from agents.generic_worker import run as run_role   # deferred, same
                                                          # circular-import
                                                          # reason as
                                                          # agents/note_taker.py,
                                                          # agents/fact_detector.py,
                                                          # agents/study_generator.py

    task_text = "Source material:\n\n" + context
    result = run_role(
        role="mapper",
        task_text=task_text,
        input_keys=[],
        session_id=None,
        include_conversation_context=False,
        domain="notes",
    )
    raw = (result.get("text") or "").strip()
    match = _MERMAID_BLOCK_RE.search(raw)
    # Best-effort fallback when the model answers without fencing it --
    # still hand back something MermaidDiagram.jsx can attempt to render
    # rather than silently saving an empty string over a real prior map.
    return match.group(1).strip() if match else raw


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("usage: python -m agents.mind_mapper <workspace_id>")
    else:
        print(generate_mindmap(_sys.argv[1])[:1000])
