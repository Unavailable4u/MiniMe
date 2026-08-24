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
from eo.secondary_data import SCOPES, get_secondary_data_scoped

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
        # NEW — Data Layer architecture §8d: only set when true (same
        # "no padding on the common case" posture
        # agents/source_planner_lean.py:_attach_excerpts() already uses
        # for its own "excerpts" key) -- pass-through only, this module
        # never sets the flag itself, that's §8c's
        # eo/correction_candidates.py:accept_candidate()'s job.
        if topic.get("user_corrected"):
            entry["user_corrected"] = True
        skeleton[tid] = entry
    return skeleton


def _children_index(topics: dict) -> dict:
    """One pass building parent_id -> [child_id, ...] off each topic's
    own `parent` field -- the hierarchy Mind Map walks per
    eo/secondary_data.py's own docstring ("`parent` is how the
    hierarchy Mind Map later walks (§6) is represented here"). There is
    no separate stored `subtopic_of` graph edge to walk (same kind of
    gap §5a's module docstring already flagged for "covers"): a real
    `subtopic_of` edge only exists once a topic is promoted into a node
    (§10), which most topics in an active tree never are yet. Reading
    "walk down subtopic_of edges" as "walk down the `parent` field's
    implied tree" is the one thing to revise if that's not what was
    meant -- the depth-adaptive walk itself below should still hold.
    """
    children = {}
    for tid, topic in topics.items():
        parent = topic.get("parent")
        if parent is None:
            continue
        children.setdefault(parent, []).append(tid)
    return children


def get_packet_depth(
    workspace_id: str,
    starting_topic_id: str,
    requested_depth: int,
    scope: str = "project",
    session_id: str = None,
) -> dict:
    """§7a: depth-adaptive walk down the `parent`-tree from a single
    starting topic, still Mode C -- no LLM call, no Primary Source
    fetch, just a narrower slice of the same skeleton get_packet()
    already builds.

    "Depth-adaptive" means the walk expands one tree level at a time
    and stops the moment a level comes back empty, rather than forcing
    exactly `requested_depth` levels regardless of whether the tree
    actually has that many. `requested_depth=0` returns just the
    starting topic itself; each level above that adds its direct
    children. The walk never goes beyond `requested_depth` even if the
    tree keeps going deeper -- that's the caller's ask, not a floor.

    Returns:
        {
          "workspace_id": str,
          "scope": "project" | "chat",
          "starting_topic_id": str,
          "requested_depth": int,
          "reached_depth": int,   # deepest level actually populated;
                                   # < requested_depth iff the tree ran
                                   # dry before satisfying the request
          "exhausted": bool,      # reached_depth < requested_depth --
                                   # §7b's own signal for when a Mode C
                                   # walk alone can't satisfy the ask
          "topics": {...},        # starting topic + every descendant
                                   # collected, same skeleton shape as
                                   # get_packet()
          "connections": [...],   # get_packet()'s connections, filtered
                                   # to endpoints both inside `topics`
        }

    Raises ValueError for a bad workspace_id/scope (same checks
    get_packet() makes) or a negative requested_depth, and KeyError if
    starting_topic_id doesn't resolve in the resolved scope -- a
    caller asking to root a walk at a topic that isn't there is a
    caller bug, not a "just return empty" situation.
    """
    if requested_depth < 0:
        raise ValueError("requested_depth must be >= 0")

    packet = get_packet(workspace_id, scope=scope, session_id=session_id)
    all_topics = packet["topics"]
    if starting_topic_id not in all_topics:
        raise KeyError(f"starting_topic_id {starting_topic_id!r} not found in scope {scope!r}")

    children = _children_index(all_topics)

    collected = {starting_topic_id}
    frontier = [starting_topic_id]
    reached_depth = 0
    for level in range(1, requested_depth + 1):
        next_frontier = []
        for tid in frontier:
            next_frontier.extend(children.get(tid, []))
        if not next_frontier:
            break
        collected.update(next_frontier)
        frontier = next_frontier
        reached_depth = level

    topics = {tid: all_topics[tid] for tid in collected}
    connections = [
        c for c in packet["connections"]
        if c.get("from_topic") in topics and c.get("to_topic") in topics
    ]

    return {
        "workspace_id": workspace_id,
        "scope": scope,
        "starting_topic_id": starting_topic_id,
        "requested_depth": requested_depth,
        "reached_depth": reached_depth,
        "exhausted": reached_depth < requested_depth,
        "topics": topics,
        "connections": connections,
    }


def get_topic_covered_sources(
    workspace_id: str,
    topic_id: str,
    scope: str = "project",
    session_id: str = None,
) -> list:
    """Step 6.11.d ("Work through: <step title>" scoping): given a
    workspace + a single topic, return just that topic's `covers` list
    (the source_section_ids it's backed by -- see this module's
    docstring for why "covers" means that and not a separate stored
    edge) with zero LLM calls and no Primary Source fetch.

    A thin wrapper over get_packet_depth(requested_depth=0) -- depth 0
    means "just the starting topic itself, no descendants" (see that
    function's own docstring), which is exactly the "one topic's
    sources" shape 6.11.f's context-splicing needs. Deliberately not a
    new tree walk: reusing get_packet_depth() keeps this one code path
    for "resolve a topic in scope" instead of a second one that could
    drift from it (e.g. missing scope="chat"'s session_id requirement).

    Returns a plain list of source_section_ids (possibly empty --
    a topic with no covers is valid, not an error). Raises the same
    ValueError/KeyError as get_packet_depth() for a bad workspace_id/
    scope/topic_id -- a caller passing a topic_id that doesn't resolve
    is a caller bug, not a "just return []" situation, same reasoning
    get_packet_depth() already documents for its own starting_topic_id.
    """
    packet = get_packet_depth(
        workspace_id,
        starting_topic_id=topic_id,
        requested_depth=0,
        scope=scope,
        session_id=session_id,
    )
    return packet["topics"][topic_id]["covers"]


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
