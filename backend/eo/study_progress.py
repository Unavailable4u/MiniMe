"""
eo/study_progress.py — Mass progress-tracking system (per-workspace,
per-topic study status).

Same JSON-store-with-lock shape as eo/quiz_progress.py and
eo/graph_edges.py: a single file, a lock around read/modify/write.
Unlike those two (which store an array of records — attempts, edges),
this store is a nested mapping keyed by workspace_id then topic_id,
since a topic's progress is a single evolving record rather than an
append-only log (see PROGRESS-6.2 for the schema once it lands).

Skeleton only for now (step 6.1 of the mass progress-tracking phase):
- module bootstrap / path constants
- the lock
- _read()/_write() primitives

Schema (workspace_id -> topic_id -> {status, notes, timestamps}) lands
in step 6.2, and get_progress()/set_progress() helpers land in step 6.3.

Place this file at: eo/study_progress.py
"""
import json
import os
import threading
from datetime import UTC, datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROGRESS_PATH = os.path.join(BASE_DIR, "data", "study", "_study_progress.json")
_lock = threading.Lock()


def _now():
    return datetime.now(UTC).isoformat()


def _read():
    if not os.path.exists(PROGRESS_PATH):
        return {}
    with open(PROGRESS_PATH) as f:
        return json.load(f)


def _write(data):
    os.makedirs(os.path.dirname(PROGRESS_PATH), exist_ok=True)
    with open(PROGRESS_PATH, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Schema (step 6.2)
#
# Store shape:
#   {
#     "<workspace_id>": {
#       "<topic_id>": {
#         "status": "not_started" | "ongoing" | "done",
#         "notes": str,            # free-form, user- or agent-authored
#         "timestamps": {
#           "created_at": iso8601 str,       # first time this topic
#                                             # got a record at all
#           "updated_at": iso8601 str,       # last write of any kind
#           "status_changed_at": iso8601 str # last time `status` itself
#                                             # changed (distinct from
#                                             # e.g. a notes-only edit)
#         },
#       },
#       ...
#     },
#     ...
#   }
#
# `status` is a closed set (unlike e.g. graph_edges.py's free-form
# `relation`) because it drives a fixed three-column board view
# (Not Started/Ongoing/Done, step 6.9) rather than being displayed
# as-is, so an unrecognized value would just be a rendering bug
# waiting to happen.
#
# A topic with no entry at all is implicitly "not_started" — the store
# only holds rows for topics that have actually been touched, mirroring
# quiz_progress.py's append-on-activity posture rather than
# pre-populating every topic_id up front (get_progress(), step 6.3,
# is where that default gets materialized for callers).
# ---------------------------------------------------------------------------

STATUS_NOT_STARTED = "not_started"
STATUS_ONGOING = "ongoing"
STATUS_DONE = "done"
VALID_STATUSES = (STATUS_NOT_STARTED, STATUS_ONGOING, STATUS_DONE)


def _default_record() -> dict:
    """The implicit shape for a topic that has never been written before —
    used by get_progress() (step 6.3) as the fallback for an unseen
    topic_id, and as the base that set_progress() (step 6.3) merges
    onto for a topic's first write."""
    now = _now()
    return {
        "status": STATUS_NOT_STARTED,
        "notes": "",
        "timestamps": {
            "created_at": now,
            "updated_at": now,
            "status_changed_at": now,
        },
    }


# ---------------------------------------------------------------------------
# get_progress()/set_progress() helpers (step 6.3)
# ---------------------------------------------------------------------------

def get_progress(workspace_id: str, topic_id: str = None):
    """Read-only lookup — no lock needed, same as quiz_progress.py's
    list_attempts()/get_attempt() (a single _read() is already atomic
    at the OS level for a file this small; the lock only guards
    read-modify-write in set_progress()).

    With `topic_id`: returns that one topic's record, defaulting to
    _default_record()'s "not_started" shape if the topic has never
    been written (see the sparse-storage note in the schema comment
    above) — callers never have to special-case a KeyError for an
    untouched topic.

    Without `topic_id`: returns the whole {topic_id: record} map for
    the workspace (empty dict if the workspace itself has no rows yet)
    — what the Not Started/Ongoing/Done board view (step 6.9) needs to
    render every topic at once.
    """
    ws = _read().get(workspace_id, {})
    if topic_id is not None:
        return ws.get(topic_id, _default_record())
    return ws


def set_progress(workspace_id: str, topic_id: str, status: str = None,
                  notes: str = None) -> dict:
    """Merge-update a topic's record under the lock — read/modify/write,
    same discipline as quiz_progress.py's record_attempt().

    Both `status` and `notes` are optional and independent: pass only
    the one(s) you're changing (e.g. build_topic_workflow()'s
    first-generation hook in step 6.6 only ever touches `status`, never
    `notes`). Omitted args leave the existing value untouched; a
    never-seen topic starts from _default_record().

    `timestamps.updated_at` is stamped on every call. `status_changed_at`
    only moves when `status` is passed AND differs from the record's
    current status — a notes-only call (or a status re-set to the same
    value) doesn't count as a transition.

    Raises ValueError for a `status` outside VALID_STATUSES rather than
    silently storing junk a board-view renderer would then choke on.
    """
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status: {status!r} (must be one of {VALID_STATUSES})"
        )

    with _lock:
        data = _read()
        ws = data.setdefault(workspace_id, {})
        record = ws.get(topic_id) or _default_record()

        now = _now()
        if status is not None and status != record["status"]:
            record["status"] = status
            record["timestamps"]["status_changed_at"] = now
        if notes is not None:
            record["notes"] = notes
        record["timestamps"]["updated_at"] = now

        ws[topic_id] = record
        _write(data)

    return record
