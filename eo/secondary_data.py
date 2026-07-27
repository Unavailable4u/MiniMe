"""
eo/secondary_data.py — Data Layer architecture §2/§10: the "Secondary
Data" half of Primary Source vs. Secondary Data.

Primary Source (agents/source_ingestor.py's write_ingested_source())
stays verbatim and untouched. Everything Source Manager and Backlink
Detector *derive* from it — the topic tree and the topic-to-topic
connection graph, plus later any accepted Corrections — lives here
instead, as one JSON document per workspace.

Same small-store shape eo/node_summaries.py and
agents/note_clusterer.py's candidate store already use: a single JSON
file, workspace_id as the top-level key, a threading.Lock around
read/modify/write. The difference from those two is *why* the doc gets
modified: per architecture §10, this document is never rewritten
wholesale — every mutation goes through apply_patch()'s JSON-Patch
(RFC 6902) subset (add/remove/replace only — the three ops Source
Manager's Mode A pass, Backlink Detector, and an accepted Correction
actually need; move/copy/test are out of scope here). Source Manager's
Mode A pass (agents/source_manager.py, §3) is the first real caller —
one batch of "add" ops per freshly-ingested source, scoped to that
source's own new topics only. Fitting those topics into the rest of an
existing workspace's tree (cross-source parents, prerequisite/
elaborates-on/contradicts/restates connections) is Backlink Detector's
incremental-patch job, a later step, not this one.

Per-workspace document shape:

    {
      "topics": {
        "<topic_id>": {
          "name": str,
          "summary": str,
          "parent": "<topic_id>" | None,
          "source_section_ids": [str, ...],
          "content_hint": "procedural" | "conceptual" | "data-heavy" | "narrative",
          "instances": [                       # optional, defaults to []
            {"source_section_ids": [...], "verbatim": str, "confidence": float}
          ]
        },
        ...
      },
      "connections": [
        {"from_topic": "<topic_id>", "to_topic": "<topic_id>", "relation": str},
        ...
      ]
    }

`topics` mirrors Source Manager's per-topic output (§3) plus a stable
id to key it by; `parent` is how the hierarchy Mind Map later walks
(§6) is represented here — a separate `subtopic_of` graph edge only
shows up once a topic is promoted into a real node (§10), which is a
later step, not this one. `connections` is Backlink Detector's
prerequisite-of/elaborates-on/contradicts/restates graph (§4) — a
free-form `relation` string, same "don't over-specify" choice
eo/graph_edges.py already made for its own edges.

`instances` (added for agents/overlapping_checker.py's "duplicate"
fold-in) is additive and optional — an old topic dict written before
this field existed has no key for it at all, so every read site that
consumes it must use `.get("instances", [])` rather than assume the
key is present; nothing here backfills it onto existing topics. It's
only ever appended to (via the `/topics/<id>/instances/-` patch path
below), never replaced or read-modified-written wholesale, so a
topic's own provenance history only grows.

Place this file at: eo/secondary_data.py
"""
import os
import json
import re
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SECONDARY_DATA_PATH = os.path.join(BASE_DIR, "data", "graph", "_secondary_data.json")
_lock = threading.Lock()

# Kept here (not just in the docstring) so later steps -- Source
# Manager's topic extraction, Corrections' validation -- import one
# shared source of truth instead of re-typing the literal strings.
CONTENT_HINTS = {"procedural", "conceptual", "data-heavy", "narrative"}

_EMPTY_WORKSPACE_DOC = {"topics": {}, "connections": []}


def _read() -> dict:
    if not os.path.exists(SECONDARY_DATA_PATH):
        return {}
    with open(SECONDARY_DATA_PATH) as f:
        return json.load(f)


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(SECONDARY_DATA_PATH), exist_ok=True)
    with open(SECONDARY_DATA_PATH, "w") as f:
        json.dump(data, f, indent=2)


def _drop_dangling_connections(doc: dict) -> dict:
    """Safety net, not a guarantee: patch history *should* always keep
    `connections` and `topics` in sync (every connection's endpoints
    added before or with the connection itself, every topic removal
    cleaning up its own edges), but nothing enforces that invariant
    across every possible caller. Rather than trust it blindly, every
    read drops (never errors on) any connection whose `from_topic` or
    `to_topic` no longer resolves to a real topic id -- cheap, and it's
    the difference between a dangling reference silently corrupting a
    Mind Map render (§6) versus the connection just quietly not being
    there. Filters the returned copy only; never rewrites the stored
    document -- that stays exactly what apply_patch() last wrote, so a
    stale write elsewhere (a bug, a bad manual edit) is *visible* as a
    dangling reference next time something inspects the raw file,
    instead of being silently swallowed on the first read after it.
    """
    topic_ids = doc["topics"].keys()
    doc["connections"] = [
        c for c in doc["connections"]
        if c.get("from_topic") in topic_ids and c.get("to_topic") in topic_ids
    ]
    return doc


def get_secondary_data(workspace_id: str) -> dict:
    """The current Secondary Data document for one workspace: current
    topic tree + connection graph. Returns a fresh empty skeleton
    (never written to disk) for a workspace that hasn't had anything
    written yet, same "don't persist on read" posture
    node_summaries.get_summaries() takes -- there's nothing worth
    storing until a real mutation happens.

    Read-only on purpose: every write path is apply_patch()'s JSON
    Patch subset, never a direct setter here, so there's exactly one
    way this document ever changes rather than a read accessor's
    sibling function inviting a second, wholesale-replace path.

    Every connection referencing a topic id that no longer resolves is
    dropped from the returned copy before it comes back to the caller
    -- see _drop_dangling_connections() above.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required")
    data = _read().get(workspace_id)
    if data is None:
        # Return a copy, not the module-level constant, so a caller
        # that mutates the returned dict in place (before this
        # workspace has ever been written) can't corrupt every other
        # empty-workspace read in the same process.
        return {"topics": dict(_EMPTY_WORKSPACE_DOC["topics"]),
                "connections": list(_EMPTY_WORKSPACE_DOC["connections"])}
    # Copy before filtering -- _drop_dangling_connections mutates the
    # dict it's handed, and the caller mutating its own copy afterward
    # still shouldn't be able to corrupt what a concurrent reader sees.
    data = {"topics": dict(data.get("topics", {})),
            "connections": list(data.get("connections", []))}
    return _drop_dangling_connections(data)


# Data Layer architecture §3d: valid values for get_secondary_data_scoped()'s
# `scope` argument. Kept as a module-level constant, same reasoning
# CONTENT_HINTS gets one, so later callers (§5a's Mode C serving, §6/§7's
# Mind Map walk, §9d's chat suggestions) import one shared source of
# truth instead of re-typing the two literal strings.
SCOPES = {"project", "chat"}


def get_secondary_data_scoped(workspace_id: str, scope: str, session_id: str = None) -> dict:
    """Data Layer architecture §3d: two read-time filters over
    get_secondary_data()'s already-dangling-filtered document, by which
    chat session originally ingested each topic's underlying source
    material. The second read-time filter this store gets, after §1d's
    dangling-connection one above -- this one is about WHO should see a
    topic, not whether it still resolves.

    scope="project" -- no filtering beyond what get_secondary_data()
    already does. "Everything in this workspace's tree" -- Notebooks'
    normal, default posture, and what every later Mode C serving pass
    (§5a) reaches for unless a caller specifically asked to narrow to
    one chat.

    scope="chat" -- keeps only topics traceable back to THIS session's
    own uploads: a topic survives if ANY of its source_section_ids
    resolves to a Primary Source node (eo/knowledge_graph.py) whose own
    session_id (§1a) matches `session_id`. Requires `session_id`
    (raises ValueError without one -- "this chat" is meaningless
    without knowing which chat). A topic with no source_section_ids at
    all (shouldn't normally happen, but nothing enforces it) is treated
    as NOT in scope for scope="chat" -- unattributable content doesn't
    default to visible just because there's nothing to disqualify it.

    Connections are filtered to only those whose BOTH endpoints survive
    the topic filter -- same "every endpoint must resolve" contract
    _drop_dangling_connections() already enforces against "does the id
    exist at all," applied here against this call's smaller in-scope
    topic set instead.

    Costs one list_nodes() scan per call (eo/knowledge_graph.py) to
    build the node_id -> session_id lookup this needs -- fine for
    Notebooks' actual call volume (once per Mind Map render or Mode C
    serve, not once per topic), same "correctness over micro-optimizing
    an uncommon path" choice agents/backlink_detector.py's own
    detect_backlinks() already makes calling list_nodes() itself.
    Degrades to an all-topics-out-of-scope result (not a raise) if that
    scan itself fails -- list_nodes() already degrades to a partial or
    empty list on its own failure, so this just passes that same
    degraded posture through.
    """
    doc = get_secondary_data(workspace_id)
    if scope == "project":
        return doc
    if scope not in SCOPES:
        raise ValueError(f"Unknown scope {scope!r}; expected one of {sorted(SCOPES)}")
    if not session_id:
        raise ValueError('session_id is required for scope="chat"')

    from eo.knowledge_graph import list_nodes  # deferred: keeps this
                                                 # module's own import
                                                 # surface small for the
                                                 # (more common) callers
                                                 # that only ever pass
                                                 # scope="project"
    node_sessions = {n["node_id"]: n.get("session_id") for n in list_nodes(workspace_id)}

    in_scope_ids = {
        tid for tid, t in doc["topics"].items()
        if any(node_sessions.get(sid) == session_id
               for sid in (t.get("source_section_ids") or []))
    }
    topics = {tid: t for tid, t in doc["topics"].items() if tid in in_scope_ids}
    connections = [
        c for c in doc["connections"]
        if c.get("from_topic") in in_scope_ids and c.get("to_topic") in in_scope_ids
    ]
    return {"topics": topics, "connections": connections}


# Three path shapes -- everything this document ever needs to
# address, per the schema in this module's docstring. Deliberately not
# a general JSON Pointer implementation (RFC 6901): the doc's shape
# never nests deeper than this, so a tiny hand-rolled matcher is
# clearer than pulling in a pointer-resolution dependency for three
# cases.
#
#   /topics/<topic_id>              -- one topic entry, keyed by id (a
#                                       dict, not a list, so add/replace/
#                                       remove are all the same "set/
#                                       delete this key" operation)
#   /topics/<topic_id>/instances/-  -- append one instance entry onto an
#                                       EXISTING topic's `instances` list
#                                       (add only -- an instance is never
#                                       removed or replaced in place;
#                                       raises if the topic id doesn't
#                                       already exist, same "endpoints
#                                       must resolve" posture
#                                       _drop_dangling_connections()
#                                       enforces for connections)
#   /connections/-                  -- RFC 6902's own "append" convention
#                                       (add only -- remove/replace need
#                                       a real index)
#   /connections/<index>            -- one connection entry by position

_INSTANCE_APPEND_RE = re.compile(r"^topics/([^/]+)/instances/-$")


def _split_path(path: str) -> tuple[str, str]:
    parts = path.strip("/").split("/", 1)
    if len(parts) != 2 or parts[0] not in ("topics", "connections"):
        raise ValueError(
            f"Unsupported path {path!r}; expected /topics/<topic_id>, "
            f"/topics/<topic_id>/instances/-, or /connections/<index|->"
        )
    return parts[0], parts[1]


def _apply_instance_append(doc: dict, action: str, topic_id: str, op: dict) -> None:
    if action != "add":
        raise ValueError(
            f"Unsupported op {action!r} on /topics/<id>/instances/-; "
            f"only 'add' (append) is supported -- an instance entry is "
            f"never removed or replaced in place"
        )
    if "value" not in op:
        raise ValueError(
            "Patch op 'add' on /topics/<id>/instances/- missing 'value'"
        )
    topic = doc["topics"].get(topic_id)
    if topic is None:
        raise ValueError(
            f"Cannot append instance: topic {topic_id!r} does not exist"
        )
    topic.setdefault("instances", []).append(op["value"])


def _apply_one(doc: dict, op: dict) -> None:
    action = op.get("op")
    if action not in ("add", "remove", "replace"):
        raise ValueError(
            f"Unsupported op {action!r}; this store only accepts "
            f"add/remove/replace (RFC 6902 move/copy/test not needed here)"
        )
    path = op.get("path")
    if not path:
        raise ValueError("Patch op missing 'path'")

    # Checked before the generic two-part split below, since this is a
    # three-part path (/topics/<id>/instances/-) that _split_path()'s
    # collection/key shape doesn't cover.
    instance_match = _INSTANCE_APPEND_RE.match(path.strip("/"))
    if instance_match:
        _apply_instance_append(doc, action, instance_match.group(1), op)
        return

    collection, key = _split_path(path)

    if collection == "topics":
        if action == "remove":
            doc["topics"].pop(key, None)  # missing key is a no-op, not an error
        else:  # add or replace -- both are just "set this key"
            if "value" not in op:
                raise ValueError(f"Patch op {action!r} on {path!r} missing 'value'")
            doc["topics"][key] = op["value"]
        return

    # collection == "connections"
    if action == "add":
        if key != "-":
            raise ValueError(
                f"Adding a connection only supports the '-' (append) "
                f"index, got {path!r}"
            )
        if "value" not in op:
            raise ValueError("Patch op 'add' on /connections/- missing 'value'")
        doc["connections"].append(op["value"])
        return

    # remove/replace on connections need a real, in-range index
    try:
        idx = int(key)
    except ValueError:
        raise ValueError(f"Connection path index must be an integer, got {path!r}")
    if not (0 <= idx < len(doc["connections"])):
        raise ValueError(f"Connection index {idx} out of range for path {path!r}")
    if action == "remove":
        doc["connections"].pop(idx)
    else:  # replace
        if "value" not in op:
            raise ValueError(f"Patch op 'replace' on {path!r} missing 'value'")
        doc["connections"][idx] = op["value"]


def apply_patch(workspace_id: str, ops: list[dict]) -> dict:
    """The only write path onto a workspace's Secondary Data document.
    `ops` is a list of RFC 6902-shaped {"op", "path", "value"?} dicts,
    restricted to add/remove/replace against the two path shapes above.

    All-or-nothing: every op is validated against a working copy before
    anything is written to disk, so a bad op in a batch (e.g. Backlink
    Detector's own incremental patch, §4) can't partially apply and
    leave the document in a state between two consistent versions.
    Locked around the whole read-validate-write sequence, same
    granularity note_clusterer.py's candidate store already uses, so
    two concurrent callers (§4's parallel Backlink Detector fan-out)
    can't race each other into a corrupted write.

    Returns the resulting document (post-patch) for a caller that wants
    to act on the new state immediately without a second read.
    """
    if not workspace_id:
        raise ValueError("workspace_id is required")
    if not ops:
        raise ValueError("ops must be a non-empty list")

    with _lock:
        all_data = _read()
        doc = all_data.get(workspace_id)
        if doc is None:
            doc = {"topics": dict(_EMPTY_WORKSPACE_DOC["topics"]),
                   "connections": list(_EMPTY_WORKSPACE_DOC["connections"])}
        else:
            # Work on a deep-enough copy so a validation failure partway
            # through the batch never touches the on-disk version.
            doc = {"topics": dict(doc.get("topics", {})),
                   "connections": list(doc.get("connections", []))}

        for op in ops:
            _apply_one(doc, op)

        all_data[workspace_id] = doc
        _write(all_data)
        return doc
