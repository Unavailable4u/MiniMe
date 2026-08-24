"""
eo/note_candidates.py — Notes domain: Part 4 §4.6's propose/accept/reject
store for agent-suggested notes.

Exact same shape eo/workspace_facts.py's own docstring promises for the
Notes domain's silent note-taking agent: propose_note() below holds a
candidate note SEPARATELY from the real graph (never touches
eo/knowledge_graph.py directly) until accept_candidate() is called — the
same "an agent-suggested addition never silently overwrites/appears
without review" discipline workspace_facts.py's propose_fact()/
accept_candidate() already established for tier-3 facts. The destination
on accept is different, though: a note isn't a workspace fact, it's a
real graph node, so accept_candidate() here calls
eo/knowledge_graph.py's write_node(node_type="note", ...) instead of
workspace_facts.update_custom_fact().

Storage: same memory-bus JSON-list-per-workspace pattern as
workspace_facts.py's `workspace_facts_candidates:{workspace_id}` —
"candidate_notes:{workspace_id}" here.

FIX — bug audit §9 (candidates accept/reject write path): this used to
address a candidate by its position in the list, same as
workspace_facts.py's store. That was fine single-player, but Part 8.4
added multi-user notification fan-out to this exact store (see
propose_note()'s emit_user_event call below) — two people can now be
looking at the same pending list at once. If user A accepts index 0
while user B is mid-review and clicks reject on what they saw at index
1, that index is still "valid" but now points at a different candidate
than the one B looked at: a silent misfire, not an error. Every
candidate now carries a real `candidate_id` (same
`f"{prefix}_{uuid.uuid4().hex[:10]}"` shape agents/note_clusterer.py's
cluster candidates already use) and accept/reject address by that id
instead — an id can't be shifted out from under a concurrent caller the
way a list index can.

Place this file at: eo/note_candidates.py
"""
import os
import sys
import uuid
from datetime import UTC, datetime

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write


def _key(workspace_id: str) -> str:
    return f"candidate_notes:{workspace_id}"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def propose_note(workspace_id: str, title: str, content: str,
                  tags: list, proposed_by: str) -> dict:
    """Agent-proposed note, held until the user accepts/rejects it —
    called by agents/note_taker.py, never write_node() directly."""
    if not workspace_id or not title or not content:
        raise ValueError("workspace_id, title, and content are required")
    candidates = read(_key(workspace_id), default=[])
    # NEW — bug audit §8 ("unread/new content" dots): this store had no
    # timestamp at all, so the frontend had nothing to diff against a
    # "last viewed" mark for the Suggested Notes sub-tab. Every other
    # candidate/edge store in this repo already stamps a timestamp for
    # exactly this reason (eo/graph_edges.py's create_edge → created_at,
    # agents/note_clusterer.py's propose_clusters → created_at) — this
    # was just the one that got missed. Purely additive field; existing
    # readers that don't know about it (accept_candidate/reject_candidate
    # below still pop by index) are unaffected.
    # FIX — bug audit §9: stable id instead of relying on list position,
    # see module docstring above for why.
    candidate = {"candidate_id": f"note_{uuid.uuid4().hex[:10]}",
                 "title": title, "content": content, "tags": tags or [],
                 "proposed_by": proposed_by, "proposed_at": _now()}
    candidates.append(candidate)
    write(_key(workspace_id), candidates)

    # NEW — Part 8.4: fan out to everyone who can see this workspace.
    # This is the real event driving §8.9's notification bell — the
    # person who needs to review a proposed note is almost never the
    # person currently looking at whatever chat the note-taker was
    # silently watching. Deferred imports, same reasoning write_node's
    # own deferred import already follows in accept_candidate() below:
    # keeps this module importable without the DB/Pusher stack wired up.
    try:
        from eo.chat_workspace import list_notify_targets
        from relay.emitter import emit_user_event
        for target_user_id in list_notify_targets(workspace_id):
            emit_user_event(
                "notification", target_user_id,
                payload={
                    "kind": "note_proposed",
                    "workspace_id": workspace_id,
                    "title": title,
                    "proposed_by": proposed_by,
                },
            )
    except Exception as exc:
        # Fire-and-forget, same discipline as relay/emitter.py itself —
        # a failed notification must never block the candidate save.
        print(f"  [note_candidates] notification emit failed: {exc}")

    return candidate


def list_candidates(workspace_id: str) -> list:
    return read(_key(workspace_id), default=[])


def accept_candidate(workspace_id: str, candidate_id: str, section: str = "notes",
                      created_by: str = "user") -> str | None:
    """User accepts a proposed note into the real knowledge graph — the
    only place this module ever calls write_node(). Removed from the
    pending list either way, same "don't let a decided candidate linger"
    rule workspace_facts.py's accept_candidate()/reject_candidate() both
    follow. Returns the new node_id, or None if the embed/upsert itself
    failed (see write_node()'s own docstring) — the candidate is still
    removed from the pending list in that case, matching write_node()'s
    "degrade, don't hard-fail" posture rather than leaving a permanently
    -stuck candidate the user can never clear.

    FIX — bug audit §9: addressed by `candidate_id`, not list position —
    see module docstring for why a plain index is unsafe once two people
    can be reviewing the same pending list at once."""
    candidates = read(_key(workspace_id), default=[])
    match_index = next((i for i, c in enumerate(candidates) if c.get("candidate_id") == candidate_id), None)
    if match_index is None:
        raise FileNotFoundError(candidate_id)
    accepted = candidates.pop(match_index)
    write(_key(workspace_id), candidates)

    from eo.knowledge_graph import write_node  # deferred — same reasoning
    # graph/adapters.py's write_imported_node() already gives for
    # late-importing this: keeps this module importable/testable without
    # the Vector stack wired up.
    node_id = write_node(
        workspace_id=workspace_id,
        section=section,
        node_type="note",
        title=accepted["title"],
        content=accepted["content"],
        created_by=accepted.get("proposed_by") or created_by,
        tags=accepted.get("tags", []),
    )

    # NEW — this note is now durably part of the workspace's knowledge.
    # Any cached answer semantically close to it may be stale or
    # contradicted by it, so purge proactively rather than waiting on
    # TTL/verification to catch it on the next read. Only bother if the
    # write actually succeeded — no point invalidating cache over a note
    # that never made it into the graph. Fire-and-forget: a failed purge
    # must never block the accept itself.
    if node_id:
        try:
            from eo.semantic_cache import invalidate_cache
            invalidate_cache(f"{accepted['title']}\n{accepted['content']}", workspace_id=workspace_id)
        except Exception as exc:
            print(f"  [note_candidates] cache invalidation failed, skipped: {exc}")

    return node_id


def get_topic_related_notes(
    workspace_id: str,
    topic_id: str,
    top_k: int = 10,
    min_score: float | None = None,
    scope: str = "project",
    session_id: str = None,
) -> list[dict]:
    """Step 6.11.e ("Work through: <step title>" scoping): accepted
    notes tied to a single topic.

    ASSUMPTION FLAGGED (same posture as eo/source_index.py's own
    module-docstring flag for "covers"): there is no stored topic ->
    note edge anywhere in this codebase. accept_candidate() above calls
    eo/knowledge_graph.py:write_node() with no topic_id/
    source_section_ids param at all -- a note becomes a graph node with
    only workspace_id/section/node_type/tags/created_by on it, nothing
    that names which topic it came from. The one existing mechanism
    that CAN relate a note to a topic is knowledge_graph.py's own
    search_nodes(), a semantic vector search already used for global
    search (§0.1) -- so this reuses that, querying with the topic's own
    name+summary (from eo/source_index.py's get_topic_covered_sources()
    sibling, get_packet_depth()) rather than inventing a second,
    unrelated matching mechanism. If a real topic_id/covers-style edge
    ever gets added to write_node() later, this should be swapped for
    an exact-match filter instead -- semantic similarity is a
    best-available proxy here, not the intended long-term shape.

    Returns search_nodes()'s own result shape (node_id, score, title,
    content, tags, created_by, created_at, ...) filtered to
    node_type="note", most-similar first. Empty list (not an error) if
    the topic has neither a name nor a summary to query with, or if
    search_nodes() itself comes back empty/degraded -- same
    "degrade, don't hard-fail" posture search_nodes() already documents
    for a Vector hiccup.

    min_score is optional and unset by default: search_nodes() already
    ranks by similarity, and Upstash Vector's score scale varies by
    index config, so imposing a hard cutoff here would be a guess.
    Pass one only if the caller has already tuned a threshold that
    fits their index.
    """
    from eo.source_index import get_packet_depth

    packet = get_packet_depth(
        workspace_id,
        starting_topic_id=topic_id,
        requested_depth=0,
        scope=scope,
        session_id=session_id,
    )
    topic = packet["topics"][topic_id]
    query_text = "\n".join(part for part in (topic.get("name"), topic.get("summary")) if part)
    if not query_text:
        return []

    from eo.knowledge_graph import search_nodes  # deferred — same
    # reasoning accept_candidate() above already gives for its own
    # write_node import: keeps this module importable/testable without
    # the Vector stack wired up.
    notes = search_nodes(
        workspace_id, query_text, top_k=top_k,
        node_type="note", session_id=session_id,
    )
    if min_score is not None:
        notes = [n for n in notes if (n.get("score") or 0) >= min_score]
    return notes


def reject_candidate(workspace_id: str, candidate_id: str) -> None:
    candidates = read(_key(workspace_id), default=[])
    match_index = next((i for i, c in enumerate(candidates) if c.get("candidate_id") == candidate_id), None)
    if match_index is None:
        raise FileNotFoundError(candidate_id)
    candidates.pop(match_index)
    write(_key(workspace_id), candidates)