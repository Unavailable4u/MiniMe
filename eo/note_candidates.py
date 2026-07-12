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
"candidate_notes:{workspace_id}" here. No stored index field on each
candidate (same reasoning workspace_facts.py's candidates already
follow): a candidate's position in the list IS its address for accept/
reject, and caching an index inside the record would go stale the moment
an earlier candidate is accepted or rejected out from under it.

Place this file at: eo/note_candidates.py
"""
import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write


def _key(workspace_id: str) -> str:
    return f"candidate_notes:{workspace_id}"


def propose_note(workspace_id: str, title: str, content: str,
                  tags: list, proposed_by: str) -> dict:
    """Agent-proposed note, held until the user accepts/rejects it —
    called by agents/note_taker.py, never write_node() directly."""
    if not workspace_id or not title or not content:
        raise ValueError("workspace_id, title, and content are required")
    candidates = read(_key(workspace_id), default=[])
    candidate = {"title": title, "content": content, "tags": tags or [],
                 "proposed_by": proposed_by}
    candidates.append(candidate)
    write(_key(workspace_id), candidates)
    return candidate


def list_candidates(workspace_id: str) -> list:
    return read(_key(workspace_id), default=[])


def accept_candidate(workspace_id: str, index: int, section: str = "notes",
                      created_by: str = "user") -> str | None:
    """User accepts a proposed note into the real knowledge graph — the
    only place this module ever calls write_node(). Removed from the
    pending list either way, same "don't let a decided candidate linger"
    rule workspace_facts.py's accept_candidate()/reject_candidate() both
    follow. Returns the new node_id, or None if the embed/upsert itself
    failed (see write_node()'s own docstring) — the candidate is still
    removed from the pending list in that case, matching write_node()'s
    "degrade, don't hard-fail" posture rather than leaving a permanently
    -stuck candidate the user can never clear."""
    candidates = read(_key(workspace_id), default=[])
    if index < 0 or index >= len(candidates):
        raise IndexError(f"no candidate at index {index}")
    accepted = candidates.pop(index)
    write(_key(workspace_id), candidates)

    from eo.knowledge_graph import write_node   # deferred — same reasoning
    # graph/adapters.py's write_imported_node() already gives for
    # late-importing this: keeps this module importable/testable without
    # the Vector stack wired up.
    return write_node(
        workspace_id=workspace_id,
        section=section,
        node_type="note",
        title=accepted["title"],
        content=accepted["content"],
        created_by=accepted.get("proposed_by") or created_by,
        tags=accepted.get("tags", []),
    )


def reject_candidate(workspace_id: str, index: int) -> None:
    candidates = read(_key(workspace_id), default=[])
    if index < 0 or index >= len(candidates):
        raise IndexError(f"no candidate at index {index}")
    candidates.pop(index)
    write(_key(workspace_id), candidates)