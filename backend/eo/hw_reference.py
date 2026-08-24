"""
eo/hw_reference.py — Hardware reference-design RAG: writer (Phase 0,
Patch 0.1 of the Mech/Enclosure implementation guide).

Same shared Upstash Vector index eo/knowledge_graph.py's write_node()
already uses, a fifth id-prefix -- see that module's own docstring for
why node:/cyclemem:/semantic_cache's un-prefixed ids/routing_memory
each get their own top-level prefix instead of overloading "node":

    hw_ref:{workspace_id}:{ref_id}

Deliberately its own module rather than a new node_type inside
knowledge_graph.py: reference-design entries are queried on a
different axis (component generic_name/aliases, see the future
search_hw_references() in Patch 0.3) than knowledge_graph.py's nodes
are (workspace_id/section/node_type/tags), and keeping them in
separate prefixes means a hw_ref: similarity search can never
accidentally surface a knowledge-graph node or vice versa -- same
isolation reasoning knowledge_graph.py's own docstring gives for not
reusing "cyclemem" or semantic_cache's bare ids.

Same "degrade, don't hard-fail" posture write_node() already
establishes: a failed embed/upsert here means "this reference design
isn't retrievable yet," never a hard error that should interrupt the
research agent's run.
"""
import os
import sys
import uuid
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import vector_index
from utils.llm_client import log_usage, embed_text

ID_PREFIX = "hw_ref"


def _hw_ref_vector_id(workspace_id: str, ref_id: str) -> str:
    return f"{ID_PREFIX}:{workspace_id}:{ref_id}"


def _query_text(generic_name: str, aliases: list | None, mobility_type: str | None = None) -> str:
    text = generic_name
    if aliases:
        text = f"{text} ({', '.join(a for a in aliases if isinstance(a, str))})"
    # Patch A.5 (Mech View standalone implementation guide, Phase A):
    # fold `mobility_type` into the query text so a wheeled robot's
    # reference-design matches aren't drawn from handheld-gadget
    # precedent -- same generic_name/aliases-only vocabulary as before
    # when `mobility_type` is absent or the "static" default (Part 1's
    # own safe-default posture), so a `full`/`static` caller's query
    # text is byte-for-byte unchanged from before this patch.
    if mobility_type and mobility_type != "static":
        text = f"{text} -- {mobility_type} device"
    return text


def write_hw_reference(mech_ref: dict) -> str | None:
    """Embeds and upserts one hardware reference-design entry.

    Expected keys on `mech_ref`:
      workspace_id      (required) -- same per-workspace isolation
                         write_node() enforces.
      generic_name       (required) -- the CANONICAL component name this
                         reference is indexed under (already resolved by
                         the caller, e.g. via
                         component_dimension_table.lookup_curated_dimensions()
                         -- see agents/web_researcher.py's indexing
                         path), not the source's own ad-hoc wording.
      content            (required) -- text to embed (title/snippet is
                         plenty; same "pass a summary, not the full
                         text" guidance write_node() gives for long
                         sources).
      title              optional, defaults to generic_name.
      source_url         optional.
      dimension_ref_id   optional -- the curated-table row id, when the
                         caller already resolved one, so a later reader
                         can cross-reference the exact table row
                         without a second lookup.
      aliases            optional list, stored for reference/debugging
                         only (search_hw_references(), Patch 0.3, still
                         queries by generic_name/aliases at call time --
                         this isn't a substitute for that).
      created_by         optional, defaults to "web_researcher".
      ref_id             optional -- caller-supplied id; a fresh one is
                         generated when absent, same convention
                         write_node() uses for node_id.
      session_id/tier    optional, forwarded to log_usage() only, same
                         as write_node().

    Returns the new ref_id on success, None if embedding/upsert failed
    -- caller should treat a None return exactly like a failed
    write_node() call: the indexing run itself isn't affected, this one
    entry just isn't searchable yet.
    """
    workspace_id = mech_ref.get("workspace_id")
    generic_name = mech_ref.get("generic_name")
    content = mech_ref.get("content")
    if not workspace_id or not generic_name or not content:
        print("  [HW Reference] missing workspace_id/generic_name/content, not stored")
        return None

    ref_id = mech_ref.get("ref_id") or uuid.uuid4().hex[:12]
    vector_id = _hw_ref_vector_id(workspace_id, ref_id)

    try:
        vector = embed_text(content)
    except Exception as exc:
        print(f"  [HW Reference] embed failed, reference not stored: {exc}")
        return None

    # Same placement rationale as write_node(): log right after the
    # embed call succeeds, so a downstream Vector failure doesn't hide
    # the fact the billable HF call already happened.
    log_usage("huggingface", "HUGGINGFACE_API_KEY", None,
              session_id=mech_ref.get("session_id"), tier=mech_ref.get("tier"),
              agent_name="HW Reference")

    metadata = {
        "workspace_id": workspace_id,
        "generic_name": generic_name,
        "title": mech_ref.get("title") or generic_name,
        "content": content,
        "source_url": mech_ref.get("source_url"),
        "dimension_ref_id": mech_ref.get("dimension_ref_id"),
        "aliases": mech_ref.get("aliases") or [],
        "created_by": mech_ref.get("created_by") or "web_researcher",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        vector_index().upsert(vectors=[(vector_id, vector, metadata)])
    except Exception as exc:
        print(f"  [HW Reference] vector upsert failed: {exc}")
        return None

    return ref_id


def search_hw_references(generic_name: str, aliases: list = None, top_k: int = 5,
                          mobility_type: str = None) -> list:
    """Patch 0.3: similarity search restricted to hw_ref: entries only.

    Patch A.5 (Mech View standalone implementation guide, Phase A):
    `mobility_type` (the device's own `mech["archetype"]["mobility_type"]`,
    e.g. "wheeled"/"handheld"/"wearable") is optional and folded into
    the retrieval query text via `_query_text()` above alongside
    `generic_name`/`aliases`, so a wheeled robot's own precedent search
    doesn't surface handheld-gadget reference builds for the same
    generic part. Omitted (or the "static" default) leaves the query
    text -- and therefore this function's own results -- unchanged
    from before this patch.

    Deliberately NOT workspace-scoped (unlike knowledge_graph.py's
    search_nodes()) -- a reference build/app-note for a given component
    is useful precedent for ANY project that later uses that same
    component, not just the workspace that happened to index it first.
    That's the whole point of this being a RAG layer instead of another
    per-workspace node type: the value compounds across every workspace
    that ever researches the same part.

    Queries by `generic_name`/`aliases` -- the same canonical
    vocabulary _ensure_generic_names() guarantees on every part by the
    time G2 (hardware_speccer.py) calls this, never an ad-hoc name from
    an earlier LLM call. This is the coordination point the Phase 0
    design calls out: write_hw_reference() (Patch 0.2's indexing path)
    and this function both key off the exact same field, so "generic
    9V battery" and "28BYJ-48 Stepper" match consistently instead of
    fragmenting into near-duplicate labels on either side.

    Prefix isolation (this function's one real job): every hw_ref:
    entry always carries a non-empty "generic_name" metadata field
    (write_hw_reference() requires it), which no other record type in
    the shared index sets -- same "filter on a field only this
    prefix's writer sets" trick eo/routing_memory.py's own
    retrieve_similar_outcomes() already uses (`filter="outcome != ''"`)
    to keep its eo_outcome: records out of unrelated queries. Belt-and-
    suspenders: results are ALSO filtered by actual id prefix below, so
    a future record type that happens to reuse "generic_name" as a
    field name still can't leak into these results.

    Returns [] -- never raises -- on embed/query failure or a clean
    no-hit query alike, so a caller (Patch 0.4) can treat "no
    precedent" and "retrieval unavailable" identically: degrade to
    today's behavior, same posture write_hw_reference() already takes
    toward failed embeds.
    """
    if not (generic_name or "").strip():
        return []

    try:
        vector = embed_text(_query_text(generic_name, aliases, mobility_type))
    except Exception as exc:
        print(f"  [HW Reference] search embed failed: {exc}")
        return []

    try:
        results = vector_index().query(
            vector=vector, top_k=top_k, include_metadata=True,
            filter="generic_name != ''",
        )
    except Exception as exc:
        print(f"  [HW Reference] search query failed: {exc}")
        return []

    matches = []
    for m in results:
        vector_id = getattr(m, "id", None)
        if not vector_id or not vector_id.startswith(f"{ID_PREFIX}:"):
            continue
        meta = getattr(m, "metadata", None)
        if not meta:
            continue
        ref_id = vector_id.split(":", 2)[-1]
        matches.append({
            "ref_id": ref_id, "vector_id": vector_id,
            "score": getattr(m, "score", None), **meta,
        })
    return matches
