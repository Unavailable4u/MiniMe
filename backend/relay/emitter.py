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
    "macro_loop_decision",
    # eo/dispatcher.py's next_step() emits these two on the rejection/cap
    # paths (hallucinated "next_destination" values, and the
    # MAX_STAGE_REVISITS ceiling) -- both were missing here, which meant
    # either path crashed the whole run with a ValueError instead of
    # degrading gracefully like every other dispatcher event does.
    "hallucinated_role_rejected", "revisit_cap_reached",
    # CO3: fired at the Human-in-the-loop pause point in eo/executor.py
    # (both the pre-existing approval_roles trigger and the new on-demand
    # pause_requested:{session_id} trigger) so the frontend's step list
    # can flip the paused role's status and surface the resume affordance
    # live, instead of the human only finding out on next reload. The
    # frontend handler for this (WorkspaceDockContext.jsx /
    # SessionContext.jsx, eventType === "awaiting_approval") already
    # existed before this patch — this entry and the executor.py call
    # site are what actually complete that wiring.
    "awaiting_approval",
    # NEW — Part 8.4: cross-chat/cross-session notifications, fired on
    # a user-{user_id} channel (see _user_channel_name/emit_user_event
    # below) rather than the per-session channel every other event
    # above uses. One event type covers every notification "kind"
    # (note_proposed today, workspace_shared/etc. later) — the kind
    # lives in payload, same as every other event's payload already
    # carries type-specific fields.
    # NEW — Phase 4 step 4.2 (Notebooks Chat-First refinement): the
    # transport decision (step 4.1, see decisions/step-4.1-notification-
    # transport.md) is for eo/notify.py's _deliver() to ALSO mirror every
    # event onto this Pusher channel, not just eo/ws_registry.py's
    # self-hosted socket -- see that file's own comment on why "also",
    # not "only" (SessionContext.jsx's real, working /ws/{session_id}
    # consumer stays on the old transport unchanged; this is additive).
    # _deliver() passes eo/notify.py's `kind` straight through as this
    # module's `event_type` (no wrapping "notification" envelope --
    # that one's reserved for Part 8.4's per-USER channel, a different
    # scope than these session-scoped kinds), so every kind in
    # eo/notify.py's own VALID_KINDS needs a matching literal entry
    # here or emit_event() raises on it. The two sets are meant to be
    # kept in lockstep by hand -- whoever next adds a kind to
    # eo/notify.py's VALID_KINDS (step 4.3 will, for
    # generation_started/generation_done/generation_error) must add the
    # same string here too.
    "upload_processed", "backlinks_updated", "workspace_promoted",
    "topic_added", "topic_merged", "connection_added",
    # NEW — step 4.3 (see the comment block above): matching literals for
    # eo/notify.py's own new VALID_KINDS entries of the same names. Payload
    # shape: {panel_key, workspace_id, label}. Step 4.4 fires these from the
    # generate flow itself; this step just makes _deliver()'s Pusher mirror
    # stop raising on them.
    "generation_started", "generation_done", "generation_error",
    # FIX — confirmed 2026-08-01: Part 8.4's own design comment above
    # (see "NEW — Part 8.4" block) says per-user notifications use the
    # literal event_type "notification" as an envelope, with the actual
    # kind (note_proposed, etc.) living in payload["kind"] -- exactly
    # what eo/note_candidates.py's propose_note() already calls
    # (emit_user_event("notification", ..., payload={"kind": "note_proposed", ...})).
    # That literal was never actually added to this set, so every such
    # call has been raising inside emit_user_event()'s try/except and
    # silently failing (caught and printed by the caller, never
    # crashing, but never delivering the notification either) since
    # Part 8.4 landed. Confirmed via scripts/seed_test_note.py:
    # "[note_candidates] notification emit failed: [relay] Unknown
    # event type 'notification'."
    "notification",
    # NEW — Step 7 of the parallel-execution work: fired from
    # eo/executor.py's _run_concurrent_group() whenever a Panel-agreed
    # parallel group actually dispatches (i.e. it already cleared Step
    # 3's sanitize_parallel_groups() and Step 5's approval_roles
    # backstop) — the observability point for seeing this feature fire
    # on real traffic and evaluating whether the Panel's proposed
    # groups are actually sensible over time.
    "parallel_group_dispatched",
    # FIX — confirmed via tests/integration/test_resume_graph.py (Part 2
    # §2.4's human-in-the-loop pause/resume checkpoint): eo/executor.py's
    # resume_graph() has fired emit_event("execution_resumed", ...) right
    # after applying a human's approve/edit/reject_redo decision since
    # that checkpoint landed, but the literal was never added here. Since
    # resume_graph() always has a real session_id (a paused run can't
    # exist without one), this check ran unconditionally on every real
    # resume call and raised ValueError immediately after the human's
    # decision was applied -- confirmed by exercising resume_graph()
    # directly against a real paused snapshot in the test above, same
    # class of gap as the "notification" fix a few entries up.
    "execution_resumed",
}

_pusher_client = None
_pusher_unavailable = False  # sticky: don't retry client construction every call


def _channel_name(session_id: str) -> str:
    """Pusher channel names allow only [A-Za-z0-9_=@,.;-]. session_id
    should already be safe (we generate it), but sanitize defensively
    since it may eventually come from a frontend-supplied value."""
    safe = re.sub(r"[^A-Za-z0-9_=@,.;-]", "-", session_id)
    return f"session-{safe}"


def _user_channel_name(user_id: str) -> str:
    """Part 8.4: the new, second channel scheme — one per PERSON rather
    than one per running task. Same sanitization as _channel_name above,
    since user_id (a Supabase auth uuid) is safe today but this is
    defensive against that ever changing."""
    safe = re.sub(r"[^A-Za-z0-9_=@,.;-]", "-", user_id)
    return f"user-{safe}"


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


def emit_user_event(
    event_type: str,
    user_id: str = None,
    agent: str = None,
    payload: dict = None,
) -> bool:
    """Part 8.4: same fire-and-forget contract as emit_event() above,
    just publishing on a user's personal channel instead of a session's.
    Kept as a separate function rather than an optional param on
    emit_event() — the two channel schemes have different "no-op if
    missing" keys (session_id vs user_id) and different intended
    callers (agents mid-run vs internal system code like
    eo/note_candidates.py), so collapsing them into one function with
    branching behavior would make both call sites harder to read than
    two small functions.
    """
    if user_id is None:
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
        "user_id": user_id,
        "agent": agent,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload or {},
    }

    try:
        client.trigger(_user_channel_name(user_id), event_type, event)
        return True
    except Exception as exc:
        print(f"  [relay] emit_user_event({event_type!r}) failed: {exc}")
        return False