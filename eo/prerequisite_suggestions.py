"""
eo/prerequisite_suggestions.py — Data Layer architecture §9d: chat
proactive suggestions.

Given the set of Primary Source node ids api/task_runner.py's own
notebook-grounding pass (_grounded_task_text()) already resolved as
relevant to THIS chat turn, finds any Backlink Detector
(agents/backlink_detector.py) "prerequisite-of" connections whose
DEPENDENT topic overlaps what's being discussed but whose PREREQUISITE
topic doesn't — i.e. topics the user hasn't touched yet that the
material they're actually asking about depends on. That's the literal
"the chat agent... can tell the user what prerequisite topics exist for
something they're asking about" from §8's Chat integration bullet.

Mode C only (eo/source_index.py:get_packet()) — no LLM call here, same
"no re-guessing what Backlink Detector already decided" posture
agents/mind_mapper.py:generate_suggested_route() already takes for this
exact same connection data (its own §7c suggested-route flowchart).

This module only ever SUGGESTS — it never triggers generation itself.
The explicit-agreement gate §8 asks for ("offer — never silently
start... Generation only starts after explicit agreement") lives
entirely on the other side of the boundary: api/task_runner.py attaches
whatever find_prerequisite_suggestions() returns onto the chat
response's `result.prerequisite_suggestions`, and it's
MessageBubble.jsx's new suggestion card (§9d, frontend half) that
actually asks the person before anything gets dispatched to
POST /api/workspaces/{ws_id}/notebooks/generate. Nothing in this file
calls that endpoint, or anything like it.

Place this file at: eo/prerequisite_suggestions.py
"""
from eo.source_index import get_packet

_PREREQ_RELATION = "prerequisite-of"

# A chat turn discussing one topic could plausibly surface many upstream
# prerequisites once the graph gets deep — capped the same "don't crowd
# a UI element built for a quick glance" reasoning as everywhere else in
# this codebase that trims a full-graph read down for one small surface
# (e.g. eo/source_index.py:_topic_skeleton()'s own field trim).
MAX_SUGGESTIONS = 3


def find_prerequisite_suggestions(
    workspace_id: str,
    grounded_node_ids: list,
    session_id: str = None,
    scope: str = "project",
) -> list:
    """Returns up to MAX_SUGGESTIONS entries, each:

        {"topic_id", "name", "summary", "for_topic_id", "for_topic_name",
         "source_node_ids"}

    — "topic_id"/"name"/"summary"/"source_node_ids" describe the
    PREREQUISITE topic being suggested (source_node_ids is that topic's
    own `covers` list — get_packet()'s covers-edge walk, §5a — handed
    straight through so a caller can scope a Generate call to just this
    topic, same `{"source_node_ids": [...]}` shape
    NotebooksGeneratePicker.jsx's own scope-by-sources mode already
    uses); "for_topic_id"/"for_topic_name" name the topic actually
    being discussed that this one is a prerequisite OF, so the offer
    can read as "X is a prerequisite of Y, which you're asking about"
    rather than a bare, contextless topic name.

    Deterministic ordering: first-appearance in packet["connections"],
    de-duplicated by prerequisite topic id (the same prerequisite can
    legitimately feed more than one discussed topic; it's only offered
    once).

    Fail-open, same posture api/task_runner.py:_grounded_task_text()
    already takes for its own Secondary Data / Primary Source read: a
    missing workspace_id, no grounded node ids, no real prerequisite-of
    edges into what's being discussed, or the packet read itself
    raising (workspace mid-write, unknown workspace_id, whatever) all
    return an empty list rather than propagating — a broken suggestion
    pass must never take an otherwise-fine chat answer down with it.
    """
    if not workspace_id or not grounded_node_ids:
        return []
    try:
        packet = get_packet(workspace_id, scope=scope, session_id=session_id)
    except Exception:
        return []

    topics = packet["topics"]
    grounded = set(grounded_node_ids)

    discussed_topic_ids = {
        tid for tid, t in topics.items()
        if grounded.intersection(t.get("covers") or [])
    }
    if not discussed_topic_ids:
        return []

    suggestions = []
    seen_from = set()
    for c in packet["connections"]:
        if (c.get("relation") or "").strip() != _PREREQ_RELATION:
            continue
        from_id, to_id = c.get("from_topic"), c.get("to_topic")
        if to_id not in discussed_topic_ids or from_id in discussed_topic_ids:
            continue   # not a prerequisite OF what's being discussed, or
                       # it's already part of what's being discussed
                       # itself — nothing new to surface either way
        if from_id in seen_from or from_id not in topics:
            continue
        seen_from.add(from_id)
        prereq = topics[from_id]
        suggestions.append({
            "topic_id": from_id,
            "name": prereq.get("name") or "Untitled topic",
            "summary": prereq.get("summary"),
            "for_topic_id": to_id,
            "for_topic_name": (topics.get(to_id) or {}).get("name") or "Untitled topic",
            "source_node_ids": list(prereq.get("covers") or []),
        })
        if len(suggestions) >= MAX_SUGGESTIONS:
            break

    return suggestions
