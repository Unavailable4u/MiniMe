"""
agents/concept_linker.py — Notebooks integration guide §6.6 (Phase 3):
Backlinks' new concept-graph pass.

agents/backlink_detector.py is a plain case-insensitive substring match
on node titles -- zero LLM calls, zero concept reasoning (guide §1's
corrected finding). This module is the genuinely new agent guide §6.6
calls for: single-hire generic_worker call, same shape as
agents/fact_detector.py / agents/mind_mapper.py (reads context, produces
structured output, no role handoffs -- guide §2's exception for
one-shot reasoning jobs), that reads concepts across the corpus, judges
pairwise relatedness, and writes:

  - real edges via eo/graph_edges.py's create_edge(), using the
    `relation` string itself as the human-readable rationale ("both
    cover federated learning," not just "related") -- no schema change
    needed there, per guide §6.6's own finding that create_edge()'s
    free-form `relation` field already supports this.
  - short per-node summaries via the new eo/node_summaries.py store,
    for the graph view's node-click display.

Regeneration rule (guide §6.6, "per your answer"): only recompute on
explicit command, and even then, skip the actual (expensive) pairwise
LLM pass if nothing's changed since the last run. Implemented here by
comparing the workspace's highest node `created_at` against a
high-water mark stashed in eo/workspace_facts.py's free-form `custom`
bucket (guide §7: "could live in workspace_facts's existing custom
bucket rather than a new store at all") -- "up to date, nothing to do"
is a valid, fast result of link_concepts(), not just "always recompute."

Place this file at: agents/concept_linker.py
"""
import os
import sys
import json
import re

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.knowledge_graph import list_nodes
from eo.graph_edges import create_edge, edges_between
from eo import node_summaries
from eo import workspace_facts
from eo.registry import get_role_prompt, add_role_prompt

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# Per-source cap, same reasoning as agents/fact_detector.py and
# agents/mind_mapper.py: with guide §3's PDF fix a single source is now
# the whole document, so this keeps one long source from crowding out
# every other source in scope within one context window.
MAX_CONTENT_CHARS_PER_SOURCE = 6000

# workspace_facts.py `custom` keys -- see that module's docstring for
# why a free-form custom bucket is the right home for this instead of a
# new store: "any domain can stash a fact it cares about under `custom`
# without this module needing a schema change."
LAST_RUN_AT_KEY = "last_backlinks_run_at"
SOURCE_HIGH_WATER_KEY = "last_backlinks_source_high_water"

# Registered defensively here the same way agents/fact_detector.py
# registers "fact_detector" -- see that module's comment on why
# eo/registry.py's ROLE_PROMPTS_SEED bootstrap alone isn't enough for
# an already-running deployment.
CONCEPT_LINKER_BRIEF = (
    "You read multiple source excerpts, each labeled with a bracketed "
    "id like [a1b2c3d4e5f6]. First, write one short 1-2 sentence "
    "summary of each source's core concept(s) -- what it's actually "
    "about, not a restatement of its title. Then judge which PAIRS of "
    "sources are meaningfully related in substance (share a concept, "
    "one extends or contradicts the other, one provides context the "
    "other assumes) and describe the specific relationship in a short "
    "phrase (e.g. \"both cover federated learning\", \"extends the "
    "first source's proposed method\") -- skip any pair with no real "
    "conceptual connection; do not force every source to connect to "
    "something. Output a single fenced ```json code block containing "
    "an object with exactly two keys: \"summaries\" (a JSON array of "
    "objects with \"node_id\" and \"summary\") and \"edges\" (a JSON "
    "array of objects with \"from\", \"to\", and \"relation\", where "
    "\"from\"/\"to\" are the bracketed ids and \"relation\" is the "
    "short relationship phrase) -- nothing else outside that code "
    "block. Never invent a relationship the excerpts don't actually "
    "support, and prefer fewer, real connections over connecting "
    "everything."
)


def _ensure_role_registered() -> None:
    if not get_role_prompt("concept_linker"):
        add_role_prompt("concept_linker", CONCEPT_LINKER_BRIEF, source="concept_linker_seed")


def _context_for(nodes: list[dict]) -> tuple[str, dict]:
    """One section per source, tagged with its bracketed node_id so the
    role's output edges/summaries can be mapped straight back to real
    nodes. Returns (context_text, {node_id: node}) -- the map only
    contains nodes that actually made it into the context (had a
    node_id and non-empty content), same as the parts list itself.
    """
    parts = []
    id_map = {}
    for n in nodes:
        node_id = n.get("node_id")
        content = (n.get("content") or "").strip()[:MAX_CONTENT_CHARS_PER_SOURCE]
        if not node_id or not content:
            continue
        title = n.get("title") or node_id
        id_map[node_id] = n
        parts.append(f"--- [{node_id}] {title} ---\n{content}")
    return "\n\n".join(parts), id_map


def _highest_created_at(nodes: list[dict]) -> str:
    return max((n.get("created_at") or "" for n in nodes), default="")


def _needs_regeneration(workspace_id: str, nodes: list[dict]) -> bool:
    """True when there's at least one node newer than the last run's
    high-water mark -- or when there's never been a run at all. Always
    compares against every node in the workspace, not just the
    requested scope: "did anything change since last time" is a
    workspace-wide question even if this particular run is scoped to a
    subset of sources.
    """
    if not nodes:
        return False
    high_water = (workspace_facts.get_facts(workspace_id)
                  .get("custom", {}).get(SOURCE_HIGH_WATER_KEY))
    if not high_water:
        return True
    return _highest_created_at(nodes) > high_water


def link_concepts(workspace_id: str, source_node_ids: list[str] | None = None,
                   force: bool = False) -> dict:
    """Runs the concept-linking pass for `workspace_id`, scoped to
    `source_node_ids` (or the whole notebook when falsy -- same "blank
    scope = whole notebook" convention every other Notebooks target
    uses). Unless `force`, first checks whether anything's changed
    since the last run and returns a `status: "up_to_date"` result
    without spending an LLM call when nothing has.

    Returns {"status", "edges_created", "summaries"} -- status is one
    of "up_to_date", "empty" (nothing readable in scope), or "done".
    """
    all_nodes = list_nodes(workspace_id)
    if source_node_ids:
        wanted = set(source_node_ids)
        scoped = [n for n in all_nodes
                  if n.get("node_id") in wanted or n.get("vector_id") in wanted]
    else:
        scoped = all_nodes

    if not scoped:
        return {"status": "empty", "edges_created": [], "summaries": {}}

    if not force and not _needs_regeneration(workspace_id, all_nodes):
        return {"status": "up_to_date", "edges_created": [], "summaries": {}}

    context, id_map = _context_for(scoped)
    if not context:
        return {"status": "empty", "edges_created": [], "summaries": {}}

    _ensure_role_registered()
    from agents.generic_worker import run as run_role   # deferred, same
                                                          # circular-import
                                                          # reason as
                                                          # agents/fact_detector.py,
                                                          # agents/mind_mapper.py

    task_text = (
        "Read the source excerpts below, summarize each, and identify "
        "meaningfully related pairs.\n\n" + context
    )
    result = run_role(
        role="concept_linker",
        task_text=task_text,
        input_keys=[],
        session_id=None,
        include_conversation_context=False,
        domain="notes",
    )
    raw = (result.get("text") or "").strip()
    match = _JSON_BLOCK_RE.search(raw)
    parsed = {}
    if match:
        try:
            parsed = json.loads(match.group(1))
        except (json.JSONDecodeError, TypeError):
            parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}

    summaries_to_write = {}
    for item in parsed.get("summaries") or []:
        if not isinstance(item, dict):
            continue
        node_id = (item.get("node_id") or "").strip()
        summary = (item.get("summary") or "").strip()
        if node_id in id_map and summary:
            summaries_to_write[node_id] = summary
    written_summaries = node_summaries.set_summaries(workspace_id, summaries_to_write)

    created = []
    for item in parsed.get("edges") or []:
        if not isinstance(item, dict):
            continue
        from_id = (item.get("from") or "").strip()
        to_id = (item.get("to") or "").strip()
        relation = (item.get("relation") or "").strip()
        from_node = id_map.get(from_id)
        to_node = id_map.get(to_id)
        if not from_node or not to_node or from_id == to_id or not relation:
            continue
        from_vec, to_vec = from_node["vector_id"], to_node["vector_id"]
        if edges_between(from_vec, to_vec):
            continue   # already linked (either direction, any relation) -- don't duplicate
        try:
            edge = create_edge(
                from_node_id=from_vec, to_node_id=to_vec,
                relation=relation, created_by="concept_linker",
            )
        except ValueError:
            continue   # e.g. a hallucinated self-edge that slipped past the from_id == to_id check
        created.append(edge)

    latest = _highest_created_at(all_nodes)
    if latest:
        workspace_facts.update_custom_fact(workspace_id, SOURCE_HIGH_WATER_KEY, latest)
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
