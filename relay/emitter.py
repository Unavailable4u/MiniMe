"""
relay/emitter.py — Stage 6, step 1 of the roadmap (Part 10):
    "Stand up the Pusher app (free tier), wire the event-emitting wrapper
    into one agent first (e.g. the Inspector) as a proof of concept."

This is the ONE place that talks to Pusher (Part 6.2: "Every agent call
wraps its work in a small event-emitting helper ... instead of calling
Pusher directly inline"). Agents call emit_event(); they never import
the pusher SDK themselves.

Design rules, straight from the blueprint:
  - One HTTP call per event, fire-and-forget (Part 1: "agents never talk
    to the frontend directly"). If the call fails or Pusher isn't
    configured, we log and move on -- an event-emission failure must
    NEVER take down the actual agent work riding alongside it.
  - Channel is per session_id (Part 6.2: "one chat 'conversation' = one
    channel, so multiple users/sessions never cross streams").
  - Event shape is exactly Part 6.3's schema.
  - No session_id -> no-op. This is what makes it safe to add
    session_id=None params to existing agents without changing their
    behavior for every caller that doesn't pass one yet (CLI usage,
    existing tests).
"""
import os
import re
import sys
from datetime import datetime, timezone

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

VALID_EVENT_TYPES = {
    "agent_start", "agent_token_chunk", "agent_done",
    "routing_decision", "usage_update", "cycle_update", "error",
    "dispatch_event", "quota_alert", "dependency_map", "structure_plan",
}

_pusher_client = None
_pusher_unavailable = False  # sticky: don't retry client construction every call


def _channel_name(session_id: str) -> str:
    """Pusher channel names allow only [A-Za-z0-9_=@,.;-]. session_id
    should already be safe (we generate it), but sanitize defensively
    since it may eventually come from a frontend-supplied value."""
    safe = re.sub(r"[^A-Za-z0-9_=@,.;-]", "-", session_id)
    return f"session-{safe}"


def _get_client():
    """Lazy singleton. Returns None (and stays None) if PUSHER_* env vars
    aren't set, so this module imports cleanly and emit_event() becomes a
    documented no-op in any environment that hasn't done Stage 6 setup yet
    -- exactly the same "skip cleanly if key_env not set" pattern
    utils/llm_client.py already uses for provider keys."""
    global _pusher_client, _pusher_unavailable
    if _pusher_client is not None:
        return _pusher_client
    if _pusher_unavailable:
        return None

    app_id = os.getenv("PUSHER_APP_ID")
    key = os.getenv("PUSHER_KEY")
    secret = os.getenv("PUSHER_SECRET")
    cluster = os.getenv("PUSHER_CLUSTER")

    if not all([app_id, key, secret, cluster]):
        _pusher_unavailable = True
        return None

    try:
        import pusher
    except ImportError:
        print("  [relay] 'pusher' package not installed -- run "
              "01_setup_environment.ps1, or pip install pusher. "
              "Events will be skipped.")
        _pusher_unavailable = True
        return None

    _pusher_client = pusher.Pusher(
        app_id=app_id, key=key, secret=secret, cluster=cluster, ssl=True,
    )
    return _pusher_client


def emit_event(
    event_type: str,
    session_id: str = None,
    agent: str = None,
    path: str = None,
    payload: dict = None,
) -> bool:
    """
    Fires one event on session_id's channel. Part 6.3 schema:
        {type, session_id, agent, path, timestamp, payload}

    Returns True if the event was sent, False if it was skipped (no
    session_id, Pusher not configured) or failed. Callers should NOT
    branch on this return value for control flow -- it exists for
    tests and optional logging only. An agent's real work must never
    depend on whether its event emission succeeded (Part 1's whole
    point: the relay is a side channel, never the source of truth).
    """
    if session_id is None:
        return False  # no-op path: no channel to publish on

    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"[relay] Unknown event type {event_type!r}. "
            f"Must be one of {sorted(VALID_EVENT_TYPES)}."
        )

    client = _get_client()
    if client is None:
        return False

    event = {
        "type": event_type,
        "session_id": session_id,
        "agent": agent,
        "path": path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload or {},
    }

    try:
        client.trigger(_channel_name(session_id), event_type, event)
        return True
    except Exception as exc:
        # Fire-and-forget: log, never raise. A dead relay must not take
        # down the agent whose progress it was trying to report.
        print(f"  [relay] emit_event({event_type!r}) failed: {exc}")
        return False