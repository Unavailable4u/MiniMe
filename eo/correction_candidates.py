"""
eo/correction_candidates.py — Data Layer architecture §8c: the pending
list a located correction (§8b's agents/correction_locator.py) sits in
until a person accepts or rejects it in the Patch Review tab. Same
propose/list/accept/reject shape eo/workspace_facts.py's own fact
candidates already use (see that module's docstring) — one more
sibling next to eo/note_candidates.py's note candidates and
agents/note_clusterer.py's cluster candidates, same reasoning each of
those gives for a stable candidate_id over a list index (Part 8.4's
notification fan-out means two reviewers can be looking at the same
pending list at once).

Storage: one memory-bus key per workspace —

    correction_candidates:{workspace_id}

Unlike workspace_facts' candidates (which hold a *proposed value*
folded into `custom` on accept), a correction candidate already holds
a ready-to-apply JSON Patch op — agents/correction_locator.py's own
op shape, {"op": "replace", "path": "/topics/<id>", "value": {...}} —
plus the topic's own pre-edit value as `before`, captured at
propose_candidate() time so the Patch Review tab can render a real
before/after even if the topic changes again before the person gets
to review it. A stale `before` just means the diff view is showing
what motivated the proposal, not a live re-read — same "the state
that triggered this, not whatever's there now" posture
agents/backlink_detector.py's own incremental patches already take
for §4's dangling-reference case.

accept_candidate() is the only path that ever calls
eo/secondary_data.py:apply_patch() for a correction — same
single-writer discipline that module's own docstring requires ("the
only write path onto a workspace's Secondary Data document"). The op
is applied BEFORE the candidate is popped off the pending list, so a
bad op (e.g. the topic vanished between propose and accept) leaves
the candidate right where it was for the person to retry or discard,
rather than silently disappearing on a failed apply.

Place this file at: eo/correction_candidates.py
"""
import uuid

from memory.bus import read, write
from eo.secondary_data import get_secondary_data, apply_patch


def _key(workspace_id: str) -> str:
    return f"correction_candidates:{workspace_id}"


def propose_candidate(workspace_id: str, correction_text: str, scope_label: str,
                       topic_id: str, op: dict) -> dict:
    """Called right after agents/correction_locator.py:locate_correction()
    returns a usable op — never for a result with op=None, there's
    nothing to review in that case (the Corrections tab shows the
    located reason inline instead, per §8a's capture-only surface, so
    a dead end never reaches this pending list at all).
    """
    if not workspace_id or not topic_id or not op:
        raise ValueError("workspace_id, topic_id, and op are required")

    # Captured now, not re-read at review time -- see module docstring.
    before = get_secondary_data(workspace_id).get("topics", {}).get(topic_id)

    candidates_key = _key(workspace_id)
    candidates = read(candidates_key, default=[])
    candidate = {
        "candidate_id": f"corr_{uuid.uuid4().hex[:10]}",
        "topic_id": topic_id,
        "op": op,
        "before": before,
        "correction_text": correction_text,
        "scope_label": scope_label,
    }
    candidates.append(candidate)
    write(candidates_key, candidates)
    return candidate


def list_candidates(workspace_id: str) -> list:
    return read(_key(workspace_id), default=[])


def accept_candidate(workspace_id: str, candidate_id: str) -> dict:
    """Applies the candidate's op via the one real write path
    (eo/secondary_data.py:apply_patch()), THEN removes it from the
    pending list -- apply first so a failed op (e.g. the topic's since
    been deleted) leaves the candidate in place instead of vanishing
    without ever having taken effect. Same "addressed by candidate_id,
    not list position" fix workspace_facts.py:accept_candidate() and
    every other sibling candidate store already applies.

    §8d: the applied value is tagged `user_corrected: true` right here,
    at the moment a person actually approves it -- never at
    propose_candidate() time (a pending, unreviewed candidate isn't a
    correction yet) and never inside
    agents/correction_locator.py:_build_op() (that module proposes an
    edit, it doesn't get to decide it was accepted). This is the one
    tag Mode B's own excerpt-pulling path
    (agents/source_planner_lean.py:_attach_excerpts(), §8d part 2)
    checks before it hands a topic's real Primary Source excerpt to a
    downstream generation agent, so a corrected name/summary/
    content_hint doesn't silently lose to whatever the raw source text
    says once Primary Source gets pulled back in.
    """
    candidates_key = _key(workspace_id)
    candidates = read(candidates_key, default=[])
    match_index = next((i for i, c in enumerate(candidates) if c.get("candidate_id") == candidate_id), None)
    if match_index is None:
        raise FileNotFoundError(candidate_id)

    candidate = candidates[match_index]
    op = dict(candidate["op"])
    if op.get("op") in ("add", "replace") and isinstance(op.get("value"), dict):
        op["value"] = {**op["value"], "user_corrected": True}
    apply_patch(workspace_id, [op])  # raises before the pending list is touched on failure

    candidates.pop(match_index)
    write(candidates_key, candidates)
    return {**candidate, "op": op}


def reject_candidate(workspace_id: str, candidate_id: str) -> None:
    candidates_key = _key(workspace_id)
    candidates = read(candidates_key, default=[])
    match_index = next((i for i, c in enumerate(candidates) if c.get("candidate_id") == candidate_id), None)
    if match_index is None:
        raise FileNotFoundError(candidate_id)
    candidates.pop(match_index)
    write(candidates_key, candidates)
