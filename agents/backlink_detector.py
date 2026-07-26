"""
agents/backlink_detector.py — Part 4 §4.3. Deterministic, no-LLM-call
backlink detection: for every node in a workspace, checks whether the
node's content mentions another node's title, and creates a
"references" edge (eo/graph_edges.py, Part 0 §0.2) when it does.
"Backlinks shown automatically" needs no code beyond this — it's just
edges_for_node() filtered to this direction, the graph's natural shape.

Plain substring matching, case-insensitive — the notes doc is explicit
this doesn't need an LLM. Titles under MIN_TITLE_LENGTH characters are
skipped as a match target: a short, generic title (e.g. "Notes", "Q3")
would false-positive against nearly every other node's content, turning
"detect real cross-references" into "link everything to everything."

Data Layer architecture §3b adds a second, unrelated Backlink Detector
job alongside the original one above: run_after_source_manager() (§3a's
trigger stub, filled in here) reconciles a freshly-ingested source's new
Secondary Data topics (eo/secondary_data.py) against the REST of the
workspace's existing topic tree -- cross-source reparenting plus a
prerequisite-of/elaborates-on/contradicts/restates connection graph --
and emits the result as JSON Patch ops via apply_patch(). This one DOES
use an LLM (one generic_worker call, role "backlink_detector"): unlike
the substring pass above, "does this new topic actually belong under
that existing one, and how do the two relate" needs real judgment, not
string matching. detect_backlinks() below is untouched by this addition
-- two independent features that happen to share a filename because the
architecture doc's step numbering (§4 in the original build, §3 here)
put the "backlink" and "topic reconciliation" ideas in the same place.

Place this file at: agents/backlink_detector.py
"""

import os
import re
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.knowledge_graph import list_nodes
from eo.graph_edges import create_edge, edges_between
from eo.registry import get_role_prompt, add_role_prompt
from eo.secondary_data import get_secondary_data, apply_patch

RELATION = "references"
MIN_TITLE_LENGTH = 4

_JSON_BLOCK_RE = re.compile(r"```json\s*(.*?)\s*```", re.DOTALL)

# §3b: caps how many EXISTING workspace topics get sent to the LLM in one
# reconciliation call. Naive, not a real scoping strategy -- a workspace
# whose tree has grown past this just silently only reconciles against
# the first BACKLINK_MAX_EXISTING_TOPICS topics (dict insertion order,
# i.e. roughly oldest-first) rather than the ones most likely to
# actually relate to this new source. A smarter pre-filter (embedding
# similarity against the new topics, most-recently-touched-first, etc.)
# is real future work this patch doesn't attempt -- flagging the gap
# rather than quietly working around it, same posture
# agents/source_manager.py's own docstring takes with its own deferred
# gaps.
BACKLINK_MAX_EXISTING_TOPICS = 300

BACKLINK_DETECTOR_BRIEF = (
    "You are reconciling a freshly-ingested source's new topics against "
    "an already-existing workspace topic tree. You're given two labeled "
    "lists: NEW TOPICS (just extracted from the new source -- each "
    "already has a provisional parent from ITS OWN source only, chosen "
    "before the rest of the workspace was visible) and EXISTING TOPICS "
    "(everything already in this workspace before this source arrived). "
    "Each topic in both lists is labeled with a bracketed id like "
    "[a1b2c3d4], its name, and its summary.\n\n"
    "Two jobs:\n"
    "1. REPARENT: for any NEW topic that's actually a subtopic of an "
    "EXISTING topic (not just another new topic from the same source), "
    "say so. Only reparent when there's a real, specific match -- most "
    "new topics keep their current parent (or stay top-level).\n"
    "2. CONNECT: identify real cross-references between a NEW topic and "
    "an EXISTING topic (never between two NEW topics -- Source Manager "
    "already covered that within this source, and never between two "
    "EXISTING topics -- those were already reconciled when THEY were "
    "new). For each real connection, name a relation: \"prerequisite-of\", "
    "\"elaborates-on\", \"contradicts\", or \"restates\".\n\n"
    "Output a single fenced ```json code block containing an object with "
    "exactly two keys:\n"
    "- \"reparents\": a JSON array of {\"topic_id\": <NEW topic's "
    "bracketed id, no brackets>, \"new_parent_id\": <EXISTING topic's "
    "bracketed id, no brackets>} -- omit or leave empty if nothing needs "
    "reparenting\n"
    "- \"connections\": a JSON array of {\"new_topic_id\": <...>, "
    "\"existing_topic_id\": <...>, \"relation\": <one of the four above>} "
    "-- omit or leave empty if nothing connects\n"
    "Nothing else outside that code block. Judge only real matches -- "
    "under-connecting is fine, over-connecting turns this into noise "
    "nobody trusts."
)


def _ensure_role_registered() -> None:
    # Same defensive bootstrap agents/source_manager.py's own
    # _ensure_role_registered() does for "source_manager" -- an
    # already-running deployment that predates this patch still needs
    # this role's brief written the first time it's actually hired.
    if not get_role_prompt("backlink_detector"):
        add_role_prompt("backlink_detector", BACKLINK_DETECTOR_BRIEF,
                         source="backlink_detector_seed")


def _topic_context_block(label: str, topics: dict) -> str:
    """One labeled section ("NEW TOPICS" / "EXISTING TOPICS") of the
    brief's context: one bracketed-id-tagged line per topic. Empty
    `topics` still returns the labeled header -- the brief is explicit
    about there being two lists, so an empty EXISTING TOPICS section
    (which callers here never actually send, see
    run_after_source_manager()'s own early-return) would otherwise be
    ambiguous rather than clearly "none."
    """
    lines = [f"--- {label} ---"]
    for topic_id, topic in topics.items():
        name = topic.get("name", "")
        summary = topic.get("summary", "")
        lines.append(f"[{topic_id}] {name}: {summary}")
    return "\n".join(lines)


def _parse_backlink_result(raw: str, new_ids: set, existing_ids: set) -> tuple[list[dict], list[dict]]:
    """Parses the fenced ```json block the "backlink_detector" role's
    output should contain into validated (reparents, connections) lists.
    Same "drop what's malformed, never raise" posture
    agents/source_manager.py's own _parse_mode_a_topics() takes -- a bad
    LLM response degrades to (fewer results, or none), never an
    exception the caller has to handle specially.

    Every id the LLM names is checked against the real id sets this call
    was actually given: a reparent whose topic_id isn't one of THIS
    call's new topics, or whose new_parent_id isn't one of THIS call's
    existing topics (or is the topic's own id -- a direct self-parent
    cycle), is dropped. Same for connections: new_topic_id must be a new
    topic, existing_topic_id must be an existing one -- the brief asks
    for exactly that shape, this enforces it rather than trusting it.
    """
    match = _JSON_BLOCK_RE.search(raw or "")
    if not match:
        return [], []
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return [], []
    if not isinstance(parsed, dict):
        return [], []

    reparents = []
    for item in parsed.get("reparents") or []:
        if not isinstance(item, dict):
            continue
        topic_id = item.get("topic_id")
        new_parent_id = item.get("new_parent_id")
        if topic_id not in new_ids or new_parent_id not in existing_ids:
            continue
        if topic_id == new_parent_id:
            continue  # can't be its own parent
        reparents.append({"topic_id": topic_id, "new_parent_id": new_parent_id})

    connections = []
    for item in parsed.get("connections") or []:
        if not isinstance(item, dict):
            continue
        new_topic_id = item.get("new_topic_id")
        existing_topic_id = item.get("existing_topic_id")
        relation = (item.get("relation") or "").strip()
        if new_topic_id not in new_ids or existing_topic_id not in existing_ids:
            continue
        if not relation:
            continue  # no usable relation -- same "no usable identity" call
                      # source_manager.py's own parser makes for a nameless topic
        connections.append({
            "from_topic": new_topic_id, "to_topic": existing_topic_id,
            "relation": relation,
        })
    return reparents, connections


def _build_ops(reparents: list[dict], connections: list[dict], doc: dict) -> list[dict]:
    """Turns validated reparents/connections into apply_patch()-ready
    RFC 6902 ops against `doc` (the Secondary Data document these
    results were computed against).

    Reparent -> a "replace" on /topics/<topic_id>: the store's own
    contract (eo/secondary_data.py's _apply_one()) treats a topic entry
    as one opaque value, not a deep-patchable object, so this replaces
    the WHOLE topic dict with only its "parent" field changed, same as
    reading-then-rewriting any other dict value.

    Connection -> an "add" on /connections/- (RFC 6902's own append
    convention), skipped if an equivalent connection (same two topics,
    either direction, regardless of relation label) already exists --
    same "already linked, don't duplicate" de-dup detect_backlinks()
    above already applies to its own edges, kept consistent here.
    """
    ops = []
    for r in reparents:
        topic = dict(doc["topics"][r["topic_id"]])
        topic["parent"] = r["new_parent_id"]
        ops.append({"op": "replace", "path": f"/topics/{r['topic_id']}", "value": topic})

    existing_pairs = {
        frozenset((c.get("from_topic"), c.get("to_topic")))
        for c in doc["connections"]
    }
    seen_pairs = set()
    for c in connections:
        pair = frozenset((c["from_topic"], c["to_topic"]))
        if pair in existing_pairs or pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        ops.append({"op": "add", "path": "/connections/-", "value": c})
    return ops


def _run_incremental_pass(new_topics: dict, existing_topics: dict, session_id: str = None) -> str:
    """§3b's one LLM call: role "backlink_detector", context built from
    both topic sets, same generic_worker.run() shape
    agents/source_manager.py's own §2c sequential pass uses --
    include_conversation_context=False since this role has no business
    seeing unrelated chat history, just these two topic lists.
    """
    _ensure_role_registered()
    from agents.generic_worker import run as run_role   # deferred -- same
                                                          # circular-import
                                                          # reason as
                                                          # agents/source_manager.py

    context = "\n\n".join([
        _topic_context_block("NEW TOPICS", new_topics),
        _topic_context_block("EXISTING TOPICS", existing_topics),
    ])
    task_text = (
        "Reconcile these new topics against the existing workspace tree, "
        "per your instructions.\n\n" + context
    )
    result = run_role(
        role="backlink_detector", task_text=task_text, input_keys=[],
        session_id=session_id, include_conversation_context=False,
        domain="notes",
    )
    return result.get("text") or ""


def run_after_source_manager(workspace_id: str, topic_ids: list[str],
                              session_id: str = None,
                              created_by: str = "system") -> list[dict]:
    """Data Layer architecture §3a/§3b. Source Manager's
    process_upload() (agents/source_manager.py, §2c/§2d) calls this the
    moment its own Mode A topic-extraction pass finishes, so a fresh
    source's new topics get reconciled against the rest of the
    workspace's tree without any separate step or button. Mode A only:
    Mode B/C (§5) don't produce new Secondary Data topics the same way,
    so they never call this -- there's nothing here for them to trigger.

    `topic_ids` is Source Manager's own return value from that pass --
    the ids of the topics IT just wrote to Secondary Data
    (eo/secondary_data.py) for this source. Two cases short-circuit
    before any LLM call: an empty `topic_ids` (Mode A found nothing
    extractable) and a workspace whose Secondary Data has no OTHER
    topics yet (this is the first source in the workspace -- nothing
    exists yet to reconcile against). Both return [] immediately.

    §3b's actual reconciliation (this function's body once past those
    guards): read the workspace's current Secondary Data
    (get_secondary_data()), split it into this call's new topics vs.
    everything else, run one "backlink_detector" LLM pass over both
    lists (_run_incremental_pass()), turn its validated answer into RFC
    6902 ops (_build_ops()), and apply them in one apply_patch() call --
    same all-or-nothing write Source Manager's own Mode A pass uses.

    Never raises: same posture as everything else Source Manager calls
    on this path (see source_manager.py's own "never raises" notes) --
    a problem in Backlink Detector shouldn't take down the upload that
    triggered it. A failure anywhere past the guards (a bad LLM
    response, every account exhausted, a malformed patch) is caught and
    logged, degrading to the ops found before the failure (possibly
    none) rather than losing an otherwise-valid batch.

    Returns the list of newly-applied ops (reparents as "replace",
    connections as "add") -- empty if nothing needed reconciling, or if
    the pass found nothing worth applying.
    """
    if not workspace_id or not topic_ids:
        return []

    from relay.emitter import emit_event  # deferred: mirrors
                                            # source_manager.py's own
                                            # import, avoids a hard
                                            # dependency for callers
                                            # that never trigger this

    doc = get_secondary_data(workspace_id)
    new_topics = {tid: doc["topics"][tid] for tid in topic_ids if tid in doc["topics"]}
    if not new_topics:
        return []  # topic_ids didn't resolve to anything currently in the
                    # doc (e.g. a race with a since-applied removal) --
                    # nothing real to reconcile
    existing_topics = {tid: t for tid, t in doc["topics"].items() if tid not in new_topics}
    if not existing_topics:
        return []  # first source in this workspace -- no wider tree yet

    if len(existing_topics) > BACKLINK_MAX_EXISTING_TOPICS:
        # dict insertion order -- see BACKLINK_MAX_EXISTING_TOPICS's own
        # docstring note on why this cap is naive rather than a real
        # scoping strategy
        existing_topics = dict(list(existing_topics.items())[:BACKLINK_MAX_EXISTING_TOPICS])

    agent_name = "backlink_detector"
    emit_event("agent_start", session_id=session_id, agent=agent_name,
               payload={"label": "Backlink Detector — reconciling new topics"})
    applied: list[dict] = []
    try:
        raw = _run_incremental_pass(new_topics, existing_topics, session_id=session_id)
        reparents, connections = _parse_backlink_result(
            raw, set(new_topics), set(existing_topics),
        )
        ops = _build_ops(reparents, connections, doc)
        if ops:
            apply_patch(workspace_id, ops)
            applied = ops
    except Exception as exc:
        print(f"  [Backlink Detector] skipped for {workspace_id}: {exc}")
        applied = []
    finally:
        emit_event("agent_done", session_id=session_id, agent=agent_name,
                   payload={"summary": f"{len(applied)} connection(s)"})
        # NEW — Data Layer architecture §9a: notify() boundary, in the
        # same finally as agent_done above so it fires whether the
        # pass succeeded, degraded to zero ops, or hit the caught
        # exception -- "processing finished" is true in all three
        # cases, even when there was nothing worth applying. §9d's
        # chat proactive suggestions (prerequisite topics from
        # Backlink data) reads this event's payload later; §9c's
        # Generate-button loading state watches this same event too
        # rather than needing its own.
        from eo.notify import notify  # deferred, same reasoning as
                                        # this function's own emit_event
                                        # import just above
        notify(session_id, "backlinks_updated", {
            "workspace_id": workspace_id, "topic_ids": topic_ids,
            "ops_applied": len(applied),
        })
    return applied


def cleanup_for_removed_source(workspace_id: str, node_ids: list[str]) -> list[dict]:
    """Data Layer architecture §3c — deletion cleanup, no LLM. Call this
    from wherever a source's own node(s) get deleted (api/server.py's
    delete_workspace_node(), the same endpoint that already cascades to
    graph_edges and cluster candidates for the deleted `node_ids` batch)
    so Secondary Data doesn't keep carrying topics -- and connections
    referencing them -- for a source that no longer exists.

    `node_ids` is that same deletion's own `batch_ids`: the root node
    plus every same_source sibling that endpoint already resolved and
    deleted. A topic belongs to "this source" if ANY of its
    source_section_ids is in that set -- Source Manager's Mode A pass
    (§3, §2c/§2d) only ever writes a topic's source_section_ids from
    ONE source's own sections in a single call, so in practice a
    topic's ids are either all-in or all-out of this set; checking
    "any" rather than "all" is just the more defensive read of that
    invariant, not a looser one.

    A topic that's a CHILD of a removed topic (parent == a removed
    topic's id) but doesn't itself belong to the deleted source is
    reparented to top-level (parent: None) rather than deleted --
    losing its own real content just because its parent's source got
    removed would be its own, separate bug. A topic that's both a
    child of a removed topic AND itself belongs to the deleted source
    needs no such op: it's already in the removal set.

    Every connection touching a removed topic (either endpoint) is
    dropped, computed from this call's own get_secondary_data() read
    and applied by matching index. Honest caveat: apply_patch()'s
    /connections/<index> path addresses the RAW stored list, while
    get_secondary_data() returns a copy with already-dangling
    connections filtered out (§1d) -- the two only line up if the raw
    store has no OTHER dangling connections at the moment this runs.
    Since apply_patch() is this document's only write path and this is
    precisely the cleanup that would otherwise let a dangling
    connection persist, that invariant holds in the normal flow; it's
    not re-verified against a second raw read here, same "documented
    narrow window, not a second locking mechanism" tradeoff the rest of
    this store's callers already accept.

    No-op (returns []) for an empty `node_ids` or a deletion that
    doesn't touch any topic's source_section_ids (a workspace with no
    Secondary Data yet, or content that was never run through Source
    Manager's Mode A pass at all).

    Never raises: same posture as run_after_source_manager() above -- a
    problem here shouldn't retroactively fail a node/edge/candidate
    deletion that already happened by the time this is called. Returns
    the list of applied ops (may be empty).
    """
    if not workspace_id or not node_ids:
        return []

    doc = get_secondary_data(workspace_id)
    deleted = set(node_ids)

    removed_topic_ids = {
        tid for tid, t in doc["topics"].items()
        if any(sid in deleted for sid in (t.get("source_section_ids") or []))
    }
    if not removed_topic_ids:
        return []

    ops = [{"op": "remove", "path": f"/topics/{tid}"} for tid in removed_topic_ids]

    for tid, t in doc["topics"].items():
        if tid in removed_topic_ids:
            continue  # already covered by its own remove op above
        if t.get("parent") in removed_topic_ids:
            orphaned = dict(t)
            orphaned["parent"] = None
            ops.append({"op": "replace", "path": f"/topics/{tid}", "value": orphaned})

    # Descending index order: within THIS batch, removing a higher
    # index first never shifts the position a lower, still-pending
    # index is about to target -- /connections/<index> is a live list
    # position, not a stable id (eo/secondary_data.py's own path-shape
    # comment).
    stale_indices = sorted(
        (i for i, c in enumerate(doc["connections"])
         if c.get("from_topic") in removed_topic_ids or c.get("to_topic") in removed_topic_ids),
        reverse=True,
    )
    ops.extend({"op": "remove", "path": f"/connections/{i}"} for i in stale_indices)

    try:
        apply_patch(workspace_id, ops)
    except ValueError as exc:
        print(f"  [Backlink Detector] deletion cleanup failed for {workspace_id}: {exc}")
        return []
    return ops


def detect_backlinks(workspace_id: str, created_by: str = "system") -> list[dict]:
    """Scans every node in `workspace_id` for title mentions, creating a
    "references" edge for every match not already linked in either
    direction. Returns the list of newly created edges (empty if
    nothing new was found).
    """
    nodes = list_nodes(workspace_id)
    targets = [n for n in nodes if len(n.get("title", "").strip()) >= MIN_TITLE_LENGTH]

    created = []
    for source in nodes:
        content = (source.get("content") or "").lower()
        if not content:
            continue
        for target in targets:
            if target["node_id"] == source["node_id"]:
                continue
            title = target["title"].strip()
            if title.lower() not in content:
                continue
            if edges_between(source["vector_id"], target["vector_id"]):
                continue  # already linked (either direction) -- don't duplicate
            edge = create_edge(
                from_node_id=source["vector_id"],
                to_node_id=target["vector_id"],
                relation=RELATION,
                created_by=created_by,
            )
            created.append(edge)
    return created


if __name__ == "__main__":
    import sys
    import json
    for ws in sys.argv[1:]:
        edges = detect_backlinks(ws)
        print(f"--- {ws}: {len(edges)} new backlink(s) ---")
        print(json.dumps(edges, indent=2)[:1000])