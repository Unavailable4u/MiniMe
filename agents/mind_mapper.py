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

CHANGED — Data Layer architecture §6a: was reading every in-scope
node's raw Primary Source content straight off eo/knowledge_graph.py's
list_nodes(). Now reads eo/source_index.py:get_packet() instead --
Mode C only, no Mode B (§5's own distinction): the mapper role gets the
already-extracted topic skeleton (name/summary/content_hint) plus the
connection graph Backlink Detector already built, never a raw-excerpt
fetch. This is strictly less content per topic than the old full-text
pass, but it's the same material Source Manager's Mode A extraction
(§2c) and Backlink Detector (§3b) already did the work of distilling --
asking the mapper role to re-read entire source documents a second
time was duplicated effort the topic tree exists specifically to avoid.

`source_node_ids` scoping used to mean "only these Primary Source
nodes"; there's no raw node id in a Mode C packet to filter by
directly, so it's now read as "only topics whose `covers` list touches
one of these node ids" -- the closest equivalent once content lives at
topic granularity instead of node granularity.

Place this file at: agents/mind_mapper.py
"""
import os
import sys
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.source_index import get_packet

_MERMAID_BLOCK_RE = re.compile(r"```mermaid\s*(.*?)\s*```", re.DOTALL)

# Topic summaries/hints are already short (Source Manager's own
# extraction pass keeps them that way) -- this is just a defensive cap,
# not the load-bearing truncation MAX_CONTENT_CHARS_PER_SOURCE used to
# be against full source text.
MAX_CONTENT_CHARS_PER_TOPIC = 2000


def _context_for(topics: dict, connections: list[dict]) -> str:
    """Builds the mapper role's context from a Mode C packet's topic
    skeleton instead of raw source text: one section per topic (name +
    summary, falling back to content_hint if a topic somehow has no
    summary yet), followed by a plain-language dump of the known
    topic-to-topic relationships so the model can route edges through
    real connections Backlink Detector already found instead of
    re-guessing them from prose alone.
    """
    parts = []
    for topic in topics.values():
        name = topic.get("name") or "Untitled topic"
        body = (topic.get("summary") or topic.get("content_hint") or "").strip()
        body = body[:MAX_CONTENT_CHARS_PER_TOPIC]
        if not body:
            continue
        parts.append(f"--- {name} ---\n{body}")

    rel_lines = []
    for c in connections:
        from_topic = topics.get(c.get("from_topic"))
        to_topic = topics.get(c.get("to_topic"))
        if not from_topic or not to_topic:
            continue
        rel_lines.append(
            f"{from_topic.get('name')} -> {to_topic.get('name')}: "
            f"{c.get('relation') or 'related'}"
        )
    if rel_lines:
        parts.append("--- Known relationships ---\n" + "\n".join(rel_lines))

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

    Raises LookupError if the resolved scope has zero readable topic
    content, so the caller can turn that into a clear per-branch error
    instead of silently saving an empty diagram.
    """
    packet = get_packet(workspace_id, scope="project")
    topics = packet["topics"]
    if source_node_ids:
        wanted = set(source_node_ids)
        topics = {tid: t for tid, t in topics.items()
                  if wanted & set(t.get("covers") or [])}
    connections = [
        c for c in packet["connections"]
        if c.get("from_topic") in topics and c.get("to_topic") in topics
    ]

    context = _context_for(topics, connections)
    if not context:
        raise LookupError("no readable topic content in scope")

    task_text = "Topic material:\n\n" + context

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
