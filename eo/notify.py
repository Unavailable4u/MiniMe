"""
eo/notify.py — Data Layer architecture §9a/§9b: notify(session_id,
kind, payload) is the one boundary every call site below fires
through; §9b (this revision) is what makes it actually push, via a
real per-session WebSocket connection registry (api/server.py's new
/ws/{session_id} route + eo/ws_registry.py).

ASSUMPTION FLAGGED (carried over from §9a, still true): relay/emitter.py
already has a real, working transport (Pusher, one channel per
session_id) that much of this codebase already emits through
(agent_start/agent_done/routing_decision/etc., Part 6;
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
    """§9b: the real per-session WebSocket push. Still logs first --
    same reason §9a's stub did: loud enough to confirm a call site
    fired correctly, without that log line being mistaken for a
    delivery guarantee, since eo.ws_registry.push() below is itself a
    documented no-op if no browser has an open socket for this
    session_id (or the app's event loop hasn't been captured yet).
    Never raises past this point -- an emission failure must not take
    down the agent work that triggered it, same rule
    relay/emitter.py:emit_event() already follows for its own
    transport.
    """
    print(f"  [notify] {event['kind']} -> session {event['session_id']}")
    try:
        _ws_push(event)
    except Exception:
        logger.exception(
            "[notify] push failed for kind=%s session=%s", event["kind"], event["session_id"],
        )
