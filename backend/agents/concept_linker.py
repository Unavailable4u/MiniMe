"""
agents/concept_linker.py — Notebooks integration guide §6.6 (Phase 3):
Backlinks' concept-graph pass.

CHANGED — Data Layer architecture §6a: this module used to run its own
generic_worker LLM pass over every in-scope source's raw content,
judging pairwise relatedness itself. That's now Backlink Detector's job
(§3b's incremental patch generation already writes topic-to-topic
`connections` into Secondary Data, with a `relation` string per edge)
and Source Manager's job (§2c's Mode A extraction already writes a
`summary` per topic) -- concept_linker doing its own LLM relatedness
pass on top was duplicated reasoning over the same material. Per §6a
("Mode C only, no Mode B"), this module is now a deterministic,
LLM-free materialization pass: it reads eo/source_index.py:get_packet()
and projects what's already there onto the two stores this module has
always owned:

  - real edges via eo/graph_edges.py's create_edge(), one per Secondary
    Data connection, using that connection's own `relation` string
    verbatim (no re-derivation needed -- Backlink Detector already
    wrote the human-readable rationale).
  - short per-node summaries via eo/node_summaries.py, one per topic's
    own `summary` field (Source Manager already wrote it).

create_edge() and node_summaries.set_summaries() both key off real
Primary Source node ids (eo/knowledge_graph.py's `node:{workspace_id}:
{node_id}` convention), not topic ids -- a topic id has no meaning to
KnowledgeGraphView.jsx's existing node-click display or to the vector
node graph edges already live on. So each topic is resolved to an
"anchor" node: the first id in that topic's `covers` list (§5a's
covers-edge walk), same node the topic's own source_section_ids already
point at. A topic with an empty `covers` list can't anchor an edge or a
summary and is silently skipped, same "nothing enforces the invariant,
degrade instead of raise" posture eo/secondary_data.py's own dangling-
reference filter takes.

Regeneration rule (guide §6.6, unchanged in spirit): only recompute on
explicit command, and even then, skip the write pass if nothing's
changed since the last run. There is no LLM cost to guard anymore, but
re-walking and re-writing edges/summaries on every Generate click is
still wasted work when nothing moved -- so the skip check now compares
a signature of Secondary Data's current connections against a
high-water mark stashed in eo/workspace_facts.py's free-form `custom`
bucket, instead of the old highest-node-`created_at` comparison (topics
carry no timestamp of their own -- see eo/secondary_data.py's documented
shape).

Place this file at: agents/concept_linker.py
"""
import os
import sys
import json
import hashlib

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.source_index import get_packet
from eo.graph_edges import create_edge, edges_between
from eo import node_summaries
from eo import workspace_facts

# workspace_facts.py `custom` keys -- see that module's docstring for
# why a free-form custom bucket is the right home for this instead of a
# new store: "any domain can stash a fact it cares about under `custom`
# without this module needing a schema change."
LAST_RUN_AT_KEY = "last_backlinks_run_at"
CONNECTIONS_SIGNATURE_KEY = "last_backlinks_connections_signature"


def _anchor_node_id(workspace_id: str, topic: dict) -> str | None:
    """The one Primary Source node a topic's edges/summary get written
    against: the first id in its `covers` list (§5a), or None if the
    topic doesn't cover anything yet.
    """
    covers = topic.get("covers") or []
    return covers[0] if covers else None


def _anchor_vector_id(workspace_id: str, topic: dict) -> str | None:
    node_id = _anchor_node_id(workspace_id, topic)
    return f"node:{workspace_id}:{node_id}" if node_id else None


def _connections_signature(connections: list[dict]) -> str:
    """A stable fingerprint of "what Secondary Data's connections look
    like right now" -- cheap to compute, cheap to compare, and changes
    whenever a connection is added, removed, or has its relation edited,
    without this module having to know or care WHY it changed.
    """
    key = "|".join(sorted(
        f"{c.get('from_topic')}->{c.get('to_topic')}:{c.get('relation')}"
        for c in connections
    ))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _needs_regeneration(workspace_id: str, connections: list[dict]) -> bool:
    """True when the current connection signature doesn't match the
    last run's high-water mark -- or when there's never been a run at
    all. Always compares against the full in-scope connection set
    (whatever the caller resolved), same "did anything change since
    last time" question the old node-`created_at` check asked, just
    against Secondary Data instead of Primary Source now.
    """
    stored = (workspace_facts.get_facts(workspace_id)
              .get("custom", {}).get(CONNECTIONS_SIGNATURE_KEY))
    if not stored:
        return True
    return _connections_signature(connections) != stored


def link_concepts(workspace_id: str, source_node_ids: list[str] | None = None,
                   force: bool = False) -> dict:
    """Runs the concept-linking materialization pass for `workspace_id`,
    scoped to `source_node_ids` (or the whole notebook when falsy --
    same "blank scope = whole notebook" convention every other
    Notebooks target uses). Unless `force`, first checks whether
    Secondary Data's connections have changed since the last run and
    returns a `status: "up_to_date"` result without re-walking the
    graph when nothing has.

    CHANGED — Data Layer architecture §6a: no LLM call anywhere in this
    function anymore -- see the module docstring for why. This is now a
    pure read (get_packet()) + pure write (create_edge()/
    set_summaries()) pass.

    `source_node_ids` scoping used to mean "only these Primary Source
    nodes"; read the same way §6a's mind_mapper.py retrofit reads it --
    "only topics whose `covers` list touches one of these node ids."

    Returns {"status", "edges_created", "summaries"} -- status is one
    of "up_to_date", "empty" (no topics in scope), or "done".
    """
    packet = get_packet(workspace_id, scope="project")
    topics = packet["topics"]
    if source_node_ids:
        wanted = set(source_node_ids)
        topics = {tid: t for tid, t in topics.items()
                  if wanted & set(t.get("covers") or [])}

    if not topics:
        return {"status": "empty", "edges_created": [], "summaries": {}}

    connections = [
        c for c in packet["connections"]
        if c.get("from_topic") in topics and c.get("to_topic") in topics
    ]

    if not force and not _needs_regeneration(workspace_id, connections):
        return {"status": "up_to_date", "edges_created": [], "summaries": {}}

    summaries_to_write = {}
    for tid, topic in topics.items():
        anchor = _anchor_node_id(workspace_id, topic)
        summary = (topic.get("summary") or "").strip()
        if anchor and summary:
            summaries_to_write[anchor] = summary
    written_summaries = node_summaries.set_summaries(workspace_id, summaries_to_write)

    created = []
    for c in connections:
        from_topic = topics.get(c.get("from_topic"))
        to_topic = topics.get(c.get("to_topic"))
        relation = (c.get("relation") or "").strip()
        if not from_topic or not to_topic or not relation:
            continue
        from_vec = _anchor_vector_id(workspace_id, from_topic)
        to_vec = _anchor_vector_id(workspace_id, to_topic)
        if not from_vec or not to_vec or from_vec == to_vec:
            continue   # no anchor node, or both topics anchor to the same source
        if edges_between(from_vec, to_vec):
            continue   # already linked (either direction, any relation) -- don't duplicate
        try:
            edge = create_edge(
                from_node_id=from_vec, to_node_id=to_vec,
                relation=relation, created_by="concept_linker",
            )
        except ValueError:
            continue   # e.g. both topics' anchors collapsed to the same node after all
        created.append(edge)

    workspace_facts.update_custom_fact(
        workspace_id, CONNECTIONS_SIGNATURE_KEY, _connections_signature(connections)
    )
    from datetime import datetime, timezone
    workspace_facts.update_custom_fact(
        workspace_id, LAST_RUN_AT_KEY, datetime.now(timezone.utc).isoformat()
    )

    return {"status": "done", "edges_created": created, "summaries": written_summaries}


if __name__ == "__main__":
    import sys as _sys
    for ws in _sys.argv[1:]:
        out = link_concepts(ws, force=True)
        print(f"--- {ws}: {out['status']}, {len(out['edges_created'])} new edge(s) ---")
        print(json.dumps(out, indent=2)[:1000])
