"""
eo/notify.py — Data Layer architecture §9a: notify(session_id, kind,
payload) boundary stub, plus the call sites wired in ahead of §9b's
real WebSocket transport.

ASSUMPTION FLAGGED: relay/emitter.py already has a real, working
transport (Pusher, one channel per session_id) that much of this
codebase already emits through (agent_start/agent_done/routing_decision/
etc., Part 6; emit_user_event()'s per-user channel, Part 8.4). §9's own
text asks for something separate, though -- "9a: notify() stub ... no
transport yet" followed by "9b: WebSocket endpoint in api/server.py +
per-session connection registry, notify() now actually pushes" -- which
only reads as "stand up a new, self-hosted WebSocket channel," not
"wrap the existing Pusher one." Taking that literally: this module is
its own thing, not a thin wrapper around relay/emitter.py:emit_event().
If that's not what was meant and this was actually meant to piggyback
on the existing Pusher channel, this file is the one thing to revise --
notify()'s call sites below and its event-shape contract should still
hold either way.

For now (§9a only): notify() takes a session_id, a kind, and a payload
dict, validates the shape, and no-ops past a log line -- there is
nowhere for it to actually push to yet. §9b adds the real delivery (a
per-session WebSocket connection registry in api/server.py) behind
this exact same function signature, so no call site wired in here
needs to change once that lands -- purely a transport swap inside
_deliver_stub()'s replacement, not a call-site rewrite.

Place this file at: eo/notify.py
"""
from datetime import datetime, timezone

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
    """§9a's boundary -- every call site below fires through this.

    Returns the event dict that WOULD be pushed once §9b wires a real
    transport behind this, or None on the no-op path (no session_id) --
    same "return value is for tests/logging only, never control flow"
    contract relay/emitter.py:emit_event() already documents for its
    own callers. Raises on an unrecognized kind (a caller bug, not a
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
    _deliver_stub(event)
    return event


def _deliver_stub(event: dict) -> None:
    """§9a placeholder for §9b's real per-session WebSocket push.
    Logs only -- loud enough to confirm a call site fired correctly
    while this step's own wiring is being tested, quiet enough (one
    line, never raises) that it can't be mistaken for an actual
    delivery guarantee. §9b replaces this function's body; nothing
    that calls notify() needs to know when that happens.
    """
    print(f"  [notify] (stub, no transport yet) {event['kind']} -> session {event['session_id']}")
