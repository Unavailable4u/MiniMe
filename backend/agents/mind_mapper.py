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
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.source_index import get_packet
from utils.mermaid_lint import looks_valid_mermaid

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
    from agents.generic_worker import run as run_role  # deferred, same
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
    # BUGFIX (rendering audit): this retry used to fire only when the fence
    # itself was missing (kind == "markdown"). A properly-fenced block that
    # was still syntactically broken Mermaid sailed through untouched and
    # only ever surfaced as MindMapView's "Couldn't render this as a
    # diagram — try Regenerate" fallback at render time, with no server-side
    # retry ever having been attempted. looks_valid_mermaid() is a cheap
    # heuristic (see utils/mermaid_lint.py's own header for exactly what it
    # can and can't catch), not a real parser, so this doesn't guarantee the
    # retried result renders either -- it just gives the common, cheaply
    # detectable breakage a second chance before giving up.
    if result["kind"] == "mermaid" and looks_valid_mermaid(result["text"]):
        return result
    return _attempt(task_text)   # one silent retry before giving up


_ROUTE_RELATION = "prerequisite-of"


def _sanitize_label(name: str) -> str:
    """Mermaid node labels here are wrapped in double quotes
    (`id["label"]`) -- strip any embedded double quotes rather than
    trying to escape them, since a topic name is short, human-written
    text where dropping a stray quote character costs nothing, and a
    bad escape sequence is exactly the kind of "syntax error in text"
    MermaidDiagram.jsx's own header comment already flags as a real,
    expected failure mode for LLM-authored Mermaid -- no reason to
    reintroduce that risk in a deterministic path that doesn't need it.
    """
    return (name or "Untitled topic").replace('"', "").strip() or "Untitled topic"


def generate_suggested_route(workspace_id: str, scope: str = "project",
                              session_id: str = None) -> dict:
    """Data Layer architecture §7c: a suggested-route flowchart built
    straight off Backlink Detector's own "prerequisite-of" connections
    (agents/backlink_detector.py's BACKLINK_DETECTOR_BRIEF, one of its
    four relation labels) -- no LLM call here at all, unlike
    generate_mindmap() above. Backlink Detector already did the one
    judgment call this needs (which topics are real prerequisites of
    which); this function's only job is reshaping that graph into
    Mermaid syntax, the same deterministic "no re-guessing what's
    already been decided" posture eo/source_index.py:get_packet() takes
    for the topic skeleton.

    Returns the same typed {"kind": "mermaid", "text": str} shape
    generate_mindmap() returns, so a caller (MindMapView's own
    <MermaidDiagram>, §4.7's click-to-sub-chat handling) needs no
    branching to tell the two apart -- "kind" is always "mermaid" here
    since there's no model output to come back unfenced; a caller
    checking `result["kind"] == "mermaid"` before rendering still holds
    for both.

    Raises LookupError if scope has zero real prerequisite-of edges
    between resolvable topics (same "don't silently save an empty
    diagram" contract generate_mindmap() already keeps for its own
    empty-context case) -- a caller should turn this into a clear
    per-branch error/empty-state, not a blank flowchart.
    """
    packet = get_packet(workspace_id, scope=scope, session_id=session_id)
    topics = packet["topics"]

    edges = []
    seen_pairs = set()
    for c in packet["connections"]:
        if (c.get("relation") or "").strip() != _ROUTE_RELATION:
            continue
        from_id, to_id = c.get("from_topic"), c.get("to_topic")
        if from_id not in topics or to_id not in topics:
            continue
        pair = (from_id, to_id)
        if pair in seen_pairs:
            continue  # same de-dup posture backlink_detector.py's own
                      # _build_ops() already applies before storing
        seen_pairs.add(pair)
        edges.append(pair)

    if not edges:
        raise LookupError("no prerequisite-of connections in scope")

    # Stable node ids in first-appearance order -- Mermaid node ids
    # can't be a raw topic id safely (format isn't guaranteed
    # identifier-safe), so map each referenced topic to a short t<N>
    # id, same "sanitize, don't trust the source string" approach
    # _sanitize_label() takes for the visible label right below.
    node_ids = {}
    for from_id, to_id in edges:
        for tid in (from_id, to_id):
            if tid not in node_ids:
                node_ids[tid] = f"t{len(node_ids)}"

    lines = ["flowchart TD"]
    for tid, sid in node_ids.items():
        lines.append(f'    {sid}["{_sanitize_label(topics[tid].get("name"))}"]')
    for from_id, to_id in edges:
        lines.append(f"    {node_ids[from_id]} --> {node_ids[to_id]}")

    return {"kind": "mermaid", "text": "\n".join(lines)}


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) < 2:
        print("usage: python -m agents.mind_mapper <workspace_id>")
    else:
        print(generate_mindmap(_sys.argv[1]))
