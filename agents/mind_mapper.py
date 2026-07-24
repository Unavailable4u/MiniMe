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

CHANGED — bug #6 fix: used to return bare Mermaid source with a "return
raw text as a best-effort mermaid string" fallback when the model
answered without fencing it. That fallback is exactly what broke Mind
Map: MermaidDiagram.jsx would try to mermaid.render() a paragraph of
prose, fail, and fall back to dumping that prose in a <pre><code> block
-- the "shows raw code/text instead of a diagram" behavior the guide
calls out. Per the guide's steer ("Mind Map is a pure visualization
surface now... it should never show raw source/code"), the fallback
belongs to the caller's rendering decision, not to a silently-degraded
string this module hands back pretending everything's fine.

Now returns a typed {"kind": "mermaid" | "markdown", "text": str}
result instead. "mermaid" means the fenced ```mermaid block matched
(text is the bare source, wrapper stripped, same shape a user's manual
paste used to provide). "markdown" means the model answered without a
valid fence -- text is the raw answer, kept only so a caller could log
it for debugging, NOT for display (api/server.py doesn't save it to
panel_content on this path; see _generate_mindmap there). Retries once
in that case (LLMs are inconsistent about fencing; a second attempt
usually succeeds) before giving up and returning kind="markdown" for
real. Saving/interpreting this result is api/server.py's job (needs the
requesting user's id, which this module has no business knowing about),
same separation agents/study_generator.py already keeps from its own
caller.

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


def _attempt(task_text: str) -> dict:
    """One role call, classified into the typed {kind, text} shape."""
    from agents.generic_worker import run as run_role   # deferred, same
                                                          # circular-import
                                                          # reason as
                                                          # agents/note_taker.py,
                                                          # agents/fact_detector.py,
                                                          # agents/study_generator.py

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
    if match:
        return {"kind": "mermaid", "text": match.group(1).strip()}
    return {"kind": "markdown", "text": raw}


def generate_mindmap(workspace_id: str, source_node_ids: list[str] | None = None) -> dict:
    """Returns {"kind": "mermaid" | "markdown", "text": str} built from
    the given sources (or every source in the workspace when
    `source_node_ids` is falsy -- "blank scope = whole notebook," same
    convention as agents/fact_detector.py and agents/study_generator.py).

    Retries the role call once, silently, if the first attempt doesn't
    come back fenced -- see the file-header note above for why. Only
    kind="markdown" after both attempts should be treated by the caller
    as "couldn't produce a diagram," not a partial success to display.

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

    task_text = "Source material:\n\n" + context

    result = _attempt(task_text)
    if result["kind"] == "mermaid":
        return result
    return _attempt(task_text)   # NEW — bug #6 fix, part 2: one silent retry before giving up


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("usage: python -m agents.mind_mapper <workspace_id>")
    else:
        print(generate_mindmap(_sys.argv[1]))
