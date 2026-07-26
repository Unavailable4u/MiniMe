"""
eo/source_index.py — Data Layer architecture §5a: get_packet(), Mode C's
deterministic serving path.

No LLM call anywhere in this module — that's the whole point of Mode C
versus Mode B (agents/source_planner_lean.py, §5b): Mode C is "hand the
generation agent the topic tree as-is," Mode B is "have a lean role
decide which topics need their raw Primary Source excerpts pulled in
too." This file only ever does the former.

ASSUMPTION FLAGGED: the notebook step this implements says get_packet()
does a "covers edge walk." There is no separate stored "covers" edge
anywhere in this codebase (eo/graph_edges.py's `relation` strings, grep'd
clean) — the only existing topic -> source relationship is
eo/secondary_data.py's own `source_section_ids` field on each topic
(the same field cleanup_for_removed_source() and
get_secondary_data_scoped(scope="chat") already key off of). Read
"covers edge walk" as: walk each in-scope topic's source_section_ids and
surface it in the packet as `covers`, so a Mode C-only caller (§6a: Mind
Mapper, Concept Linker) knows WHICH source sections back each topic
without this module ever dereferencing those ids into actual excerpt
content -- pulling real excerpt text is Mode B's job, not Mode C's. If
that's not what "covers edge walk" meant, this function's shape is the
one thing to revise; get_secondary_data_scoped() and the packet's outer
shape (scope resolution, topic skeleton) should still hold either way.

Place this file at: eo/source_index.py
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.secondary_data import get_secondary_data_scoped, SCOPES

# Mode C's topic skeleton: every field a generation agent needs to
# understand the SHAPE of the tree (what a topic is, where it sits,
# what kind of content it is, what it covers) and nothing that requires
# a further fetch to use -- no raw excerpt text, that's Mode B's job.
_SKELETON_FIELDS = ("name", "summary", "parent", "content_hint")


def _topic_skeleton(topics: dict) -> dict:
    """§5a's topic-skeleton trim + covers-edge walk, one pass: each
    in-scope topic reduced to `_SKELETON_FIELDS` plus a `covers` list
    (see module docstring's ASSUMPTION FLAGGED note) -- walked straight
    off that topic's own `source_section_ids`, never resolved further
    into node content here.

    A topic missing a skeleton field (shouldn't happen post-§2c/§3b,
    nothing enforces it) gets None for that field rather than a
    KeyError -- a caller building context text from a packet shouldn't
    have to handle this module raising over one incomplete entry.
    """
    skeleton = {}
    for tid, topic in topics.items():
        entry = {field: topic.get(field) for field in _SKELETON_FIELDS}
        entry["covers"] = list(topic.get("source_section_ids") or [])
        skeleton[tid] = entry
    return skeleton


def get_packet(workspace_id: str, scope: str = "project", session_id: str = None) -> dict:
    """The one Mode C entry point: everything a Mode-C-only generation
    agent (§6a: Mind Mapper, Concept Linker) needs, and nothing it
    doesn't -- no LLM call, no Primary Source fetch, just Secondary
    Data reshaped for direct consumption.

    scope/session_id pass straight through to
    eo/secondary_data.py:get_secondary_data_scoped() -- same two-value
    contract (SCOPES = {"project", "chat"}), same requirement that
    scope="chat" calls come with a session_id or raise. Defaults to
    scope="project" since that's this store's own documented default
    posture ("what every later Mode C serving pass reaches for unless a
    caller specifically asked to narrow to one chat").

    Returns:
        {
          "workspace_id": str,
          "scope": "project" | "chat",
          "topics": {"<topic_id>": {"name", "summary", "parent",
                                     "content_hint", "covers": [node_id, ...]}, ...},
          "connections": [{"from_topic", "to_topic", "relation"}, ...],
        }

    `connections` passes through unchanged from get_secondary_data_scoped()
    -- it's already exactly the shape a consumer needs (both endpoints
    already filtered to the same in-scope topic set that produced
    `topics` above), nothing left to trim.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required")
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope {scope!r}; expected one of {sorted(SCOPES)}")

    doc = get_secondary_data_scoped(workspace_id, scope, session_id=session_id)
    return {
        "workspace_id": workspace_id,
        "scope": scope,
        "topics": _topic_skeleton(doc["topics"]),
        "connections": doc["connections"],
    }
