"""
eo/timeline_node_blurbs.py — CO4 patch 5: short plain-language blurbs
for the routing-trace timeline's node-click detail panel (role steps,
CO4 patch 3's cache_hit/worker_pool_selection decision events, and the
two endpoint nodes).

Reuses eo/node_summaries.py's pattern per the CO4 plan ("new blurb
store + endpoint" rather than inventing a new one) -- a single JSON
file, a lock around read/modify/write. Same shape as that file, with
one difference: node_summaries.py is per-workspace and written by an
agent (concept_linker.py) once per node INSTANCE, because a concept
node's summary depends on the actual source content behind it. A
timeline node has no equivalent per-instance content -- "cache_hit" or
"implementer" mean the same thing on every run -- so this store is
global and keyed by node KIND instead of workspace_id, and there's no
agent writing to it. DEFAULT_BLURBS ships with the file so a fresh
checkout has real content on first request rather than an empty store
that only fills in after something writes to it.

set_blurb() exists for the same forward-compatibility reason
node_summaries.set_summaries() does -- so a later patch can add an
edit affordance without a second store -- but nothing in CO4 calls it
yet; get_blurbs() is the only function api/routes/tasks.py's new
GET /api/timeline/node_blurbs uses.

Read by RoutingTraceGraph.jsx's node-click detail panel.

Place this file at: eo/timeline_node_blurbs.py
"""
import os
import json
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLURBS_PATH = os.path.join(BASE_DIR, "data", "timeline", "_node_blurbs.json")
_lock = threading.Lock()

# Keyed by the same `kind` RoutingTraceGraph.jsx's detail panel derives
# per node (see that file's blurbKindOf()): a role's step id for a
# regular step node, event.type for a CO4 patch 3 decision node, and
# the two endpoint ids. Anything not in this map (a role not covered
# below, e.g. a domain-specific one from agentRoleIcons.js's full
# table) falls back to a generic per-category line in the frontend
# rather than needing every role enumerated here by hand.
DEFAULT_BLURBS = {
    "__input__": "Your task as it arrived, before classification or routing decided anything.",
    "__output__": "The final answer handed back to you, after every role in the pipeline finished.",
    "cache_hit": "A previous answer was similar enough (by fingerprint match or an LLM-verified check) that the semantic cache returned it directly instead of re-running the pipeline.",
    "worker_pool_selection": "The quota-ranked fairness rotation picked which provider/worker pool would run this role, based on remaining quota across accounts.",
}


def _read() -> dict:
    if not os.path.exists(BLURBS_PATH):
        return dict(DEFAULT_BLURBS)
    with open(BLURBS_PATH) as f:
        data = json.load(f)
    # Stored overrides win over defaults, but a kind never explicitly
    # overridden still gets its default rather than disappearing.
    merged = dict(DEFAULT_BLURBS)
    merged.update(data)
    return merged


def _write(data: dict) -> None:
    os.makedirs(os.path.dirname(BLURBS_PATH), exist_ok=True)
    with open(BLURBS_PATH, "w") as f:
        json.dump(data, f, indent=2)


def get_blurbs() -> dict:
    """kind -> blurb, for every kind with a blurb right now (defaults
    plus any stored overrides/additions)."""
    return _read()


def get_blurb(kind: str) -> str | None:
    return get_blurbs().get(kind)


def set_blurb(kind: str, blurb: str) -> dict:
    """Not called anywhere yet (see module docstring). A falsy `blurb`
    removes a stored override for `kind`, reverting it to its
    DEFAULT_BLURBS entry (or to nothing, if it was never a default)."""
    if not kind:
        raise ValueError("kind is required")
    with _lock:
        data = _read()
        if blurb:
            data[kind] = blurb
        else:
            data.pop(kind, None)
        _write(data)
    return data
