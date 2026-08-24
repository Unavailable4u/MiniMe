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

import json
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.graph_edges import create_edge, edges_between
from eo.knowledge_graph import list_nodes
from eo.registry import add_role_prompt, get_role_prompt
from eo.secondary_data import apply_patch, get_secondary_data

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


# NEW — chat audit fix: run_after_source_manager()'s "if not
# existing_topics: return []" guard used to be the end of the story for
# a workspace's first source (or any source landing where every OTHER
# topic in the workspace also came from that same one source) -- there
# was simply no code path that ever let a source's topics connect to
# EACH OTHER, only to topics from a DIFFERENT, already-existing source.
# That's structurally fine for a multi-source research workspace, but
# for the much more common single-PDF-notebook case it meant
# packet["connections"] (what Mind Map's Study Path and the Library
# graph both read) stayed permanently empty no matter how many times
# Backlinks got re-run -- there was never anything to reconcile
# against. SELF_CONNECT_BRIEF/_run_self_connect_pass()/
# _parse_self_connect_result() below are the same-source counterpart to
# BACKLINK_DETECTOR_BRIEF/_run_incremental_pass()/_parse_backlink_result()
# above, minus the reparent half (a source's topics already have their
# own local parent from Mode A's own extraction pass -- see
# agents/source_manager.py's own docstring on that division of labor)
# -- just CONNECT, among one single list instead of two.
SELF_CONNECT_BRIEF = (
    "You are looking for real conceptual connections between topics that "
    "were all just extracted from the SAME single source (e.g. one "
    "lecture, one PDF, one document) -- there's nothing else in this "
    "workspace yet to compare against, so every topic here comes from "
    "the same place. You're given one labeled list, TOPICS, each "
    "tagged with a bracketed id like [a1b2c3d4], its name, and its "
    "summary.\n\n"
    "For every pair of DIFFERENT topics in this list that has a real "
    "relationship, name it with one of: \"prerequisite-of\" (you need to "
    "understand the first before the second makes sense), "
    "\"elaborates-on\" (the second goes deeper into something the first "
    "already introduced), \"contradicts\", or \"restates\".\n\n"
    "Output a single fenced ```json code block containing an object with "
    "exactly one key, \"connections\": a JSON array of "
    "{\"topic_a_id\": <bracketed id, no brackets>, \"topic_b_id\": "
    "<bracketed id, no brackets>, \"relation\": <one of the four above>}. "
    "topic_a_id and topic_b_id must be two DIFFERENT ids from the list "
    "above. Nothing else outside that code block. Judge only real "
    "matches -- under-connecting is fine, over-connecting turns this "
    "into noise nobody trusts. Leave the array empty if nothing in this "
    "list actually relates to anything else in it."
)


def _ensure_self_connect_role_registered() -> None:
    if not get_role_prompt("backlink_self_connect"):
        add_role_prompt("backlink_self_connect", SELF_CONNECT_BRIEF,
                         source="backlink_self_connect_seed")


def _run_self_connect_pass(topics: dict, session_id: str = None) -> str:
    """Same-source counterpart to _run_incremental_pass() below, for the
    case run_after_source_manager() used to just give up on entirely --
    see this section's own top-of-block note.
    """
    _ensure_self_connect_role_registered()
    from agents.generic_worker import run as run_role  # deferred, same
                                                          # reasoning as
                                                          # _run_incremental_pass()

    context = _topic_context_block("TOPICS", topics)
    task_text = (
        "Find real connections between these topics, per your "
        "instructions.\n\n" + context
    )
    result = run_role(
        role="backlink_self_connect", task_text=task_text, input_keys=[],
        session_id=session_id, include_conversation_context=False,
        domain="notes",
    )
    return result.get("text") or ""


def _parse_self_connect_result(raw: str, topic_ids: set) -> list[dict]:
    """Same validate-against-the-real-id-set posture
    _parse_backlink_result() above takes, for the single-list shape
    _run_self_connect_pass() actually asks for -- connections only, no
    reparents (see this section's own top-of-block note on why).
    """
    match = _JSON_BLOCK_RE.search(raw or "")
    if not match:
        return []
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []

    connections = []
    for item in parsed.get("connections") or []:
        if not isinstance(item, dict):
            continue
        a = item.get("topic_a_id")
        b = item.get("topic_b_id")
        relation = (item.get("relation") or "").strip()
        if a not in topic_ids or b not in topic_ids or a == b or not relation:
            continue
        connections.append({"from_topic": a, "to_topic": b, "relation": relation})
    return connections


def _run_self_connect_and_apply(workspace_id: str, new_topics: dict, doc: dict,
                                 session_id: str = None) -> list[dict]:
    """The same-source counterpart to run_after_source_manager()'s normal
    phase 2 (below): used only when this source's topics have no OTHER
    workspace topics yet to reconcile against. Same never-raises
    posture, same emit_event/notify boundary as the rest of this module
    -- kept as its own function so run_after_source_manager()'s early-
    return stays a one-line change rather than duplicating all of phase
    2's plumbing inline.
    """
    # run_after_source_manager()'s
    # own import below
    from eo.notify import notify  # deferred, same reasoning
    from relay.emitter import emit_event  # deferred, same reasoning as

    if len(new_topics) < 2:
        return []  # nothing to connect a single topic to

    agent_name = "backlink_detector"
    emit_event("agent_start", session_id=session_id, agent=agent_name,
               payload={"label": "Backlink Detector — connecting this source's own topics"})
    applied: list[dict] = []
    try:
        raw = _run_self_connect_pass(new_topics, session_id=session_id)
        connections = _parse_self_connect_result(raw, set(new_topics))
        ops = _build_ops([], connections, doc)
        if ops:
            apply_patch(workspace_id, ops)
            applied.extend(ops)
    except Exception as exc:
        print(f"  [Backlink Detector] self-connect skipped for {workspace_id}: {exc}")
    else:
        if applied:
            try:
                for op in applied:
                    notify(session_id, "connection_added", {
                        "workspace_id": workspace_id, **op["value"],
                    })
            except Exception as exc:
                print(f"  [Backlink Detector] connection_added notify skipped: {exc}")

    emit_event("agent_done", session_id=session_id, agent=agent_name,
               payload={"summary": f"{len(applied)} connection(s)"})
    notify(session_id, "backlinks_updated", {
        "workspace_id": workspace_id, "topic_ids": list(new_topics),
        "ops_applied": len(applied),
    })
    return applied


def _run_incremental_pass(new_topics: dict, existing_topics: dict, session_id: str = None) -> str:
    """§3b's one LLM call: role "backlink_detector", context built from
    both topic sets, same generic_worker.run() shape
    agents/source_manager.py's own §2c sequential pass uses --
    include_conversation_context=False since this role has no business
    seeing unrelated chat history, just these two topic lists.
    """
    _ensure_role_registered()
    from agents.generic_worker import run as run_role  # deferred -- same
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
                              created_by: str = "system",
                              overlap_tags: dict = None) -> list[dict]:
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

    §4: `overlap_tags` is Source Manager's other new return value from
    that same pass -- agents/overlapping_checker.py's per-topic
    {"tag": "new"|"duplicate"|"merge", "target_topic_id"} results,
    keyed by topic_id. "duplicate"-tagged topics never reach this
    function at all (Source Manager already folded them into the
    target topic's `instances` list and never wrote them as their own
    Secondary Data topic, so they're never in `topic_ids` to begin
    with). "merge"-tagged topics ARE still real topics here -- kept as
    their own node -- but skip this function's usual reparent/connect
    judgment call entirely: Overlapping Checker already decided they're
    the same underlying fact as an existing topic, so this short-
    circuits straight to a `same_fact_as` connection op instead of
    spending an LLM call re-deciding something already decided. Every
    other topic_id (tag "new", or no tag at all -- `overlap_tags`
    defaults to `{}` for any caller that doesn't pass it) goes through
    the unchanged reconciliation pass below.

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
    none) rather than losing an otherwise-valid batch. §4's merge-tag
    write and the LLM reconciliation pass are now two independent write
    phases for exactly this reason: a failure in the (LLM-backed, more
    failure-prone) reconciliation pass shouldn't roll back a same_fact_as
    connection that already landed.

    Returns the list of newly-applied ops (reparents as "replace",
    connections as "add", including any §4 same_fact_as ones) -- empty
    if nothing needed reconciling, or if the pass found nothing worth
    applying.
    """
    if not workspace_id or not topic_ids:
        return []
    overlap_tags = overlap_tags or {}

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
        # BUGFIX (chat audit): this used to just `return []` here --
        # "first source in this workspace, nothing else to reconcile
        # against" -- which is true, but left this source's OWN topics
        # never connected to EACH OTHER either, since that was never
        # this branch's job before. See _run_self_connect_and_apply()'s
        # own docstring above for why that's the wrong call for the
        # common single-source-notebook case.
        return _run_self_connect_and_apply(workspace_id, new_topics, doc, session_id=session_id)

    if len(existing_topics) > BACKLINK_MAX_EXISTING_TOPICS:
        # dict insertion order -- see BACKLINK_MAX_EXISTING_TOPICS's own
        # docstring note on why this cap is naive rather than a real
        # scoping strategy
        existing_topics = dict(list(existing_topics.items())[:BACKLINK_MAX_EXISTING_TOPICS])

    # §4: pull "merge"-tagged topics out of the normal reconciliation set.
    # A topic only short-circuits here if its tagged target is actually
    # one of THIS call's existing_topics (same "don't trust the id,
    # check it against the real set" posture _parse_backlink_result()
    # already takes with the LLM's own output below) -- anything else
    # (no tag, tag "new", or a "merge" tag whose target didn't survive
    # into existing_topics) falls through to the unchanged LLM pass.
    merge_ops = []
    reconcile_ids = []
    for tid in new_topics:
        tag_info = overlap_tags.get(tid)
        target_id = tag_info.get("target_topic_id") if tag_info else None
        if tag_info and tag_info.get("tag") == "merge" and target_id in existing_topics:
            merge_ops.append({
                "op": "add", "path": "/connections/-",
                "value": {"from_topic": tid, "to_topic": target_id,
                          "relation": "same_fact_as"},
            })
        else:
            reconcile_ids.append(tid)

    # Same "already linked, don't duplicate" de-dup _build_ops() applies
    # to the LLM pass's own connections below, applied here too so a
    # retried upload can't write a second same_fact_as edge for a pair
    # that already has one (in either direction, any relation).
    existing_pairs = {
        frozenset((c.get("from_topic"), c.get("to_topic"))) for c in doc["connections"]
    }
    merge_ops = [
        op for op in merge_ops
        if frozenset((op["value"]["from_topic"], op["value"]["to_topic"])) not in existing_pairs
    ]

    agent_name = "backlink_detector"
    emit_event("agent_start", session_id=session_id, agent=agent_name,
               payload={"label": "Backlink Detector — reconciling new topics"})
    applied: list[dict] = []

    # Phase 1 (§4): write the merge short-circuits on their own, before
    # the LLM pass even runs -- see this function's docstring on why
    # this is a separate try from phase 2 below.
    if merge_ops:
        try:
            apply_patch(workspace_id, merge_ops)
            applied.extend(merge_ops)
        except Exception as exc:
            print(f"  [Backlink Detector] same_fact_as write failed for {workspace_id}: {exc}")
        else:
            # §6b: fire once the write actually succeeded -- every
            # merge_ops entry is itself a connection ("op": "add", path
            # "/connections/-"), so all of them get an event here, not
            # a filtered subset the way phase 2 below needs one. Own
            # try/except so a notify hiccup is never mistaken for the
            # write itself having failed.
            try:
                from eo.notify import notify  # deferred, same reasoning
                                                # as this function's own
                                                # emit_event import above
                for op in merge_ops:
                    notify(session_id, "connection_added", {
                        "workspace_id": workspace_id, **op["value"],
                    })
            except Exception as exc:
                print(f"  [Backlink Detector] connection_added notify skipped: {exc}")

    # Phase 2: the original LLM reconciliation pass, now scoped to
    # reconcile_ids -- topics §4 already handled are excluded so the
    # LLM never re-decides something Overlapping Checker already
    # settled with a real embedding comparison.
    reconcile_topics = {tid: new_topics[tid] for tid in reconcile_ids}
    if reconcile_topics:
        try:
            raw = _run_incremental_pass(reconcile_topics, existing_topics, session_id=session_id)
            reparents, connections = _parse_backlink_result(
                raw, set(reconcile_topics), set(existing_topics),
            )
            ops = _build_ops(reparents, connections, doc)
            if ops:
                apply_patch(workspace_id, ops)
                applied.extend(ops)
        except Exception as exc:
            print(f"  [Backlink Detector] skipped for {workspace_id}: {exc}")
        else:
            # §6b: _build_ops() mixes reparents ("replace" on
            # /topics/<id>) and connections ("add" on /connections/-)
            # in one list -- only the latter is a connection_added
            # event. Own try/except, same reasoning as phase 1's.
            connection_ops = [op for op in ops if op["op"] == "add"] if ops else []
            if connection_ops:
                try:
                    from eo.notify import notify  # deferred, same
                                                    # reasoning as this
                                                    # function's own
                                                    # emit_event import
                                                    # above
                    for op in connection_ops:
                        notify(session_id, "connection_added", {
                            "workspace_id": workspace_id, **op["value"],
                        })
                except Exception as exc:
                    print(f"  [Backlink Detector] connection_added notify skipped: {exc}")

    # §4: no longer a single try/finally -- phase 1 and phase 2 above
    # each catch their own exception, so this now always runs after
    # both, the same "processing finished" guarantee the old single
    # finally block gave when there was only ever one write phase.
    emit_event("agent_done", session_id=session_id, agent=agent_name,
               payload={"summary": f"{len(applied)} connection(s)"})
    # NEW — Data Layer architecture §9a: notify() boundary, fires whether
    # either phase succeeded, degraded to zero ops, or hit a caught
    # exception -- "processing finished" is true in all cases, even when
    # there was nothing worth applying. §9d's chat proactive suggestions
    # (prerequisite topics from Backlink data) reads this event's payload
    # later; §9c's Generate-button loading state watches this same event
    # too rather than needing its own.
    from eo.notify import notify  # deferred, same reasoning as this
                                    # function's own emit_event import
                                    # just above
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
    import json
    import sys
    for ws in sys.argv[1:]:
        edges = detect_backlinks(ws)
        print(f"--- {ws}: {len(edges)} new backlink(s) ---")
        print(json.dumps(edges, indent=2)[:1000])