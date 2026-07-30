"""
eo/notify.py — Data Layer architecture §9a/§9b: notify(session_id,
kind, payload) is the one boundary every call site below fires
through; §9b (this revision) is what makes it actually push, via a
real per-session WebSocket connection registry (api/server.py's new
/ws/{session_id} route + eo/ws_registry.py).

RESOLVED (Notebooks Chat-First refinement, Phase 4 steps 4.1/4.2 — was
the open question flagged below): _deliver() now pushes through BOTH
transports. eo/ws_registry.py's self-hosted socket stays exactly as it
was -- SessionContext.jsx's `/ws/{session_id}` connection is its real,
working consumer today and this phase doesn't touch it. Every event is
now ALSO mirrored onto relay/emitter.py's existing `session-{session_id}`
Pusher channel (via emit_event(), see that file's own step-4.2 comment
on its VALID_EVENT_TYPES) -- the channel WorkspaceDockContext.jsx
already binds for every dock, with zero new subscription plumbing
needed to receive these there. See decisions/step-4.1-notification-
transport.md for the full writeup of why Pusher over a second
self-hosted socket.

ORIGINAL ASSUMPTION FLAGGED (kept for history, now resolved above):
relay/emitter.py already has a real, working transport (Pusher, one
channel per session_id) that much of this codebase already emits
through (agent_start/agent_done/routing_decision/etc., Part 6;
emit_user_event()'s per-user channel, Part 8.4). §9's own text asks for
something separate, though -- "9a: notify() stub ... no transport yet"
followed by "9b: WebSocket endpoint in api/server.py + per-session
connection registry, notify() now actually pushes" -- which only reads
as "stand up a new, self-hosted WebSocket channel," not "wrap the
existing Pusher one." Taking that literally: this module pushes
through its own eo/ws_registry.py, not through relay/emitter.py:
emit_event(). If that's not what was meant and this was actually meant
to piggyback on the existing Pusher channel, this file (and
eo/ws_registry.py) are the two things to revise -- notify()'s call
sites below and its event-shape contract hold either way.

§9b's actual change: notify() itself is unchanged -- still validates
shape, still session_id=None no-ops, still raises on an unknown kind.
Only _deliver()'s body changed, from a log-only stub to a real push
(eo.ws_registry.push()), so no call site wired in below needed to
change for this to land.

Place this file at: eo/notify.py
"""
import logging
from datetime import datetime, timezone

from eo.ws_registry import push as _ws_push
from relay.emitter import emit_event as _emit_pusher_event   # NEW — Phase 4 step 4.2

logger = logging.getLogger(__name__)

# Every event kind a §9a call site fires today. Same closed-set
# validation posture relay/emitter.py:VALID_EVENT_TYPES already uses --
# a typo'd kind should fail loud in dev, not silently vanish into a
# channel nobody's listening on yet. Grows as later steps wire their
# own call sites (§9c's Generate-button loading state watches these
# same two rather than needing a new kind of its own -- see module
# docstring above; §9d's chat proactive suggestions reads
# "backlinks_updated" payloads for prerequisite topics).
VALID_KINDS = {
    "upload_processed",    # agents/source_manager.py:process_upload() finished
    "backlinks_updated",   # agents/backlink_detector.py:run_after_source_manager() finished
    "workspace_promoted",  # NEW — §10d: eo/chat_workspace.py:chat_triggered_partial_promote()
                            # fired an actual promote
    "topic_added",         # NEW — Overlap/Live-Viz guide §5: agents/source_manager.py's
                            # Mode A pass wrote a genuinely new topic node.
                            # payload: {topic_id, name, parent}
    "topic_merged",        # NEW — same guide, §5: a "duplicate"-tagged topic got folded
                            # into an existing one's `instances` instead of its own node.
                            # payload: {topic_id, target_topic_id, tag}
    "connection_added",    # NEW — same guide, §5: a connection op landed (Backlink
                            # Detector's normal reparent/connect pass, or its §4
                            # same_fact_as merge short-circuit). payload:
                            # {from_topic, to_topic, relation}
}


def notify(session_id: str, kind: str, payload: dict = None) -> dict | None:
    """The one boundary every call site below fires through.

    Returns the event dict that was pushed (§9b), or None on the
    no-op path (no session_id) -- same "return value is for
    tests/logging only, never control flow" contract
    relay/emitter.py:emit_event() already documents for its own
    callers. Raises on an unrecognized kind (a caller bug, not a
    runtime condition to degrade past) rather than staying silent the
    way the no-session_id path does.

    session_id=None is a no-op, not an error -- same reasoning
    relay/emitter.py gives for its own no-channel case: this keeps it
    safe to call notify() from a code path that sometimes runs without
    a session (CLI usage, background jobs) without every caller having
    to guard for that itself.
    """
    if session_id is None:
        return None
    if kind not in VALID_KINDS:
        raise ValueError(f"[notify] Unknown kind {kind!r}. Must be one of {sorted(VALID_KINDS)}.")

    event = {
        "kind": kind,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload or {},
    }
    _deliver(event)
    return event


def _deliver(event: dict) -> None:
    """§9b + Phase 4 step 4.2: pushes through BOTH transports, each
    independently guarded -- one failing (or one kind not yet
    registered on the other side, see relay/emitter.py's own
    VALID_EVENT_TYPES comment) must never block or take down the
    other. Still logs first -- same reason §9a's stub did: loud enough
    to confirm a call site fired correctly, without that log line
    being mistaken for a delivery guarantee on either transport, since
    both eo.ws_registry.push() and relay.emitter.emit_event() are
    themselves documented no-ops in various "nobody's listening yet"
    conditions (no open socket for this session_id; Pusher not
    configured in this environment).
    """
    print(f"  [notify] {event['kind']} -> session {event['session_id']}")
    try:
        _ws_push(event)
    except Exception:
        logger.exception(
            "[notify] ws_registry push failed for kind=%s session=%s", event["kind"], event["session_id"],
        )
    # NEW — step 4.2: mirrored onto the Pusher session channel too.
    # `event["kind"]` is passed straight through as emit_event()'s
    # `event_type` -- see relay/emitter.py's VALID_EVENT_TYPES comment
    # for why every kind in this file's VALID_KINDS needs a matching
    # entry there, and what to do when adding a new one (step 4.3 will).
    try:
        _emit_pusher_event(event["kind"], session_id=event["session_id"], payload=event["payload"])
    except Exception:
        logger.exception(
            "[notify] pusher mirror failed for kind=%s session=%s", event["kind"], event["session_id"],
        )
