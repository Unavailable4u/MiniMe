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
from enum import Enum

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class EventType(str, Enum):
    """Single source of truth for every event type this module will
    accept, on either channel (session or per-user). Was previously a
    bare `VALID_EVENT_TYPES` string set with no structural link to the
    ~20+ emit_event()/emit_user_event() call sites across agents/ and
    eo/ -- callers passed raw string literals, so a typo'd or newly
    added literal only surfaced as a runtime ValueError, mid-task,
    whenever that code path first executed (see this module's git
    history / PATCH-A for the incident that prompted this rewrite:
    nine literals -- architecture_diagram, plan_handoff, schema_diagram,
    device_spec, deploy_config_proposed, deploy_config_written,
    deploy_confirmed, deploy_declined, uptimerobot_registered,
    uptimerobot_registration_failed -- were in real use but never made
    it into the old set).

    Being an Enum instead of a set means:
      - Call sites should reference members (EventType.AGENT_START) so
        a typo is an AttributeError / static-type-checker error, not a
        thing that only fails once that branch runs in production.
      - `VALID_EVENT_TYPES` below is now *derived* from this enum
        (`{e.value for e in EventType}`) instead of being hand-copied,
        so the two can never drift apart from each other again.
      - eo/notify.py's own VALID_KINDS (a second, separately
        hand-maintained closed set that mirrors a subset of this one)
        is repointed at this same enum in the follow-up patch, closing
        the other half of the drift.

    str, Enum) mixin: members compare equal to, and hash the same as,
    their plain string value, so `"agent_start" == EventType.AGENT_START`
    and `"agent_start" in {EventType.AGENT_START, ...}` both hold --
    existing code/tests that compare against plain strings keep working
    unchanged.
    """
    AGENT_START = "agent_start"
    AGENT_TOKEN_CHUNK = "agent_token_chunk"
    AGENT_DONE = "agent_done"
    ROUTING_DECISION = "routing_decision"
    USAGE_UPDATE = "usage_update"
    CYCLE_UPDATE = "cycle_update"
    ERROR = "error"
    DISPATCH_EVENT = "dispatch_event"
    QUOTA_ALERT = "quota_alert"
    DEPENDENCY_MAP = "dependency_map"
    STRUCTURE_PLAN = "structure_plan"
    MACRO_LOOP_DECISION = "macro_loop_decision"
    AGENT_REQUESTED_ROLE = "agent_requested_role"

    # eo/dispatcher.py's next_step() emits these two on the rejection/cap
    # paths (hallucinated "next_destination" values, and the
    # MAX_STAGE_REVISITS ceiling).
    HALLUCINATED_ROLE_REJECTED = "hallucinated_role_rejected"
    REVISIT_CAP_REACHED = "revisit_cap_reached"

    # CO3: fired at the Human-in-the-loop pause point in eo/executor.py
    # (both the pre-existing approval_roles trigger and the on-demand
    # pause_requested:{session_id} trigger) so the frontend's step list
    # can flip the paused role's status and surface the resume affordance
    # live. Frontend handler: WorkspaceDockContext.jsx / SessionContext.jsx,
    # eventType === "awaiting_approval".
    AWAITING_APPROVAL = "awaiting_approval"

    # Part 8.4: cross-chat/cross-session notifications, fired on a
    # user-{user_id} channel (see _user_channel_name/emit_user_event
    # below) rather than the per-session channel every other member
    # above uses. One event type covers every notification "kind"
    # (note_proposed today, workspace_shared/etc. later) -- the kind
    # lives in payload, same as every other event's payload already
    # carries type-specific fields.
    NOTIFICATION = "notification"

    # Phase 4 steps 4.1-4.3 (Notebooks Chat-First refinement): eo/notify.py's
    # _deliver() mirrors every notify() call onto this Pusher channel too,
    # in addition to eo/ws_registry.py's self-hosted socket. Every kind in
    # eo/notify.py's VALID_KINDS needs a matching member here -- that file
    # now derives its set from this enum instead of hand-copying, so this
    # is the only place new session-scoped notify() kinds need to be added.
    UPLOAD_PROCESSED = "upload_processed"
    BACKLINKS_UPDATED = "backlinks_updated"
    WORKSPACE_PROMOTED = "workspace_promoted"
    TOPIC_ADDED = "topic_added"
    TOPIC_MERGED = "topic_merged"
    CONNECTION_ADDED = "connection_added"
    # Payload shape: {panel_key, workspace_id, label}.
    GENERATION_STARTED = "generation_started"
    GENERATION_DONE = "generation_done"
    GENERATION_ERROR = "generation_error"

    # Step 7 of the parallel-execution work: fired from
    # eo/executor.py's _run_concurrent_group() whenever a Panel-agreed
    # parallel group actually dispatches.
    PARALLEL_GROUP_DISPATCHED = "parallel_group_dispatched"

    # Part 2 §2.4's human-in-the-loop pause/resume checkpoint:
    # eo/executor.py's resume_graph() fires this right after applying a
    # human's approve/edit/reject_redo decision.
    EXECUTION_RESUMED = "execution_resumed"

    # CO4 patch 3: "cache_hit" fires from eo/semantic_cache.py's
    # check_cache() whenever a cached answer is actually returned
    # (trusted-fingerprint or LLM-verified). "worker_pool_selection"
    # fires from eo/worker_pool.py's _select_workers() whenever the
    # quota-ranked fairness rotation actually picks a worker pool (not
    # the Panel's own key_override path -- that's an explicit hire, not
    # a rotation decision).
    CACHE_HIT = "cache_hit"
    WORKER_POOL_SELECTION = "worker_pool_selection"

    # Live-refetch fix (patch 3 follow-up): fired from
    # api/task_runner.py's _write_plan_panels() after each successful
    # write_panel_from_role() call, on the workspace's own channel (see
    # _workspace_channel_name/emit_workspace_event below) rather than the
    # per-session channel every member above uses -- a panel write needs
    # to reach every dock/tab/chat that has that WORKSPACE open, not just
    # the one session whose chat turn happened to trigger the write.
    # Payload shape: {panel_key, workspace_id}. Frontend handler:
    # PlanTab.jsx's workspace-${activeWs.id} subscription.
    PANEL_CONTENT_UPDATED = "panel_content_updated"

    # Code sub-tab write-back, patch 9: fired from api/task_runner.py's
    # _write_code_files() after each successful workspace_code_files
    # write, same "workspace channel, not session channel" reasoning as
    # PANEL_CONTENT_UPDATED immediately above — a code file write needs
    # to reach every dock/tab that has that WORKSPACE's Build tab open,
    # not just the session whose chat turn produced it. Payload shape:
    # {file_path, workspace_id}. Frontend handler: BuildTab.jsx's
    # workspace-${selected.id} subscription (patch 10, once the file-tree
    # view exists to actually react to it).
    CODE_FILE_UPDATED = "code_file_updated"

    # PATCH-A additions: real call sites (agents/*.py) that were firing
    # these literals all along but had no matching entry, so every one
    # of them raised ValueError the first time that code path executed
    # in production -- same class of gap as the "notification" and
    # "execution_resumed" fixes above, just never caught until now.
    ARCHITECTURE_DIAGRAM = "architecture_diagram"       # agents/architecture_diagrammer.py
    PLAN_HANDOFF = "plan_handoff"                        # agents/handoff_packager.py
    SCHEMA_DIAGRAM = "schema_diagram"                    # agents/schema_diagrammer.py
    DEVICE_SPEC = "device_spec"                          # agents/hardware_speccer.py
    DEPLOY_CONFIG_PROPOSED = "deploy_config_proposed"    # agents/deploy_config_writer.py
    DEPLOY_CONFIG_WRITTEN = "deploy_config_written"      # agents/deploy_agent.py
    DEPLOY_CONFIRMED = "deploy_confirmed"                # agents/deploy_agent.py
    DEPLOY_DECLINED = "deploy_declined"                  # agents/deploy_agent.py
    UPTIMEROBOT_REGISTERED = "uptimerobot_registered"    # agents/deploy_agent.py
    UPTIMEROBOT_REGISTRATION_FAILED = "uptimerobot_registration_failed"  # agents/deploy_agent.py


# Backward-compat view: existing code/tests that do
# `"foo" in emitter.VALID_EVENT_TYPES` or iterate/sort it as plain
# strings keep working unchanged. This is now *derived*, never
# hand-edited -- add new event types to the EventType enum above, not
# here.
VALID_EVENT_TYPES = {e.value for e in EventType}

# The session-scoped subset that eo/notify.py's notify() accepts as a
# `kind`. Previously eo/notify.py kept its own separately hand-typed
# VALID_KINDS string set that was meant to mirror this one -- the two
# were expected to be "kept in lockstep by hand" (that file's own old
# comment), which is exactly the kind of manual-sync requirement that
# let the original bug happen in the first place. eo/notify.py now
# imports this constant and derives its VALID_KINDS from it instead of
# retyping the strings, so adding/renaming a notify()-eligible event
# type only ever needs to happen in one place: here, by adding the
# EventType member above AND listing it below. A member left out of
# this set simply isn't notify()-eligible (e.g. AGENT_START is a valid
# EventType for emit_event() but was never meant to be a notify() kind)
# -- that's a deliberate, visible choice made in this file, not silent
# drift discovered later in a traceback.
NOTIFY_KINDS = frozenset({
    EventType.UPLOAD_PROCESSED,
    EventType.BACKLINKS_UPDATED,
    EventType.WORKSPACE_PROMOTED,
    EventType.TOPIC_ADDED,
    EventType.TOPIC_MERGED,
    EventType.CONNECTION_ADDED,
    EventType.GENERATION_STARTED,
    EventType.GENERATION_DONE,
    EventType.GENERATION_ERROR,
})

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


def _workspace_channel_name(workspace_id: str) -> str:
    """Live-refetch fix (patch 3 follow-up): the third channel scheme —
    one per WORKSPACE rather than one per running task (_channel_name)
    or one per person (_user_channel_name). A panel write-back needs to
    reach every dock/tab/chat currently looking at that workspace, not
    just the single session whose chat turn triggered the write, which
    is exactly the gap the session-scoped channel can't cover. Same
    sanitization as the other two channel helpers, since workspace_id is
    a generated id today but this is defensive against that changing."""
    safe = re.sub(r"[^A-Za-z0-9_=@,.;-]", "-", workspace_id)
    return f"workspace-{safe}"


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
        missing = [name for name, val in (
            ("PUSHER_APP_ID", app_id), ("PUSHER_KEY", key),
            ("PUSHER_SECRET", secret), ("PUSHER_CLUSTER", cluster),
        ) if not val]
        print(f"  [relay] Pusher not configured -- missing env var(s): {', '.join(missing)}. "
              f"emit_event() will silently no-op for every call this session (agent_start/"
              f"agent_done/routing_decision/etc. included), so the Working Panel will show "
              f"nothing live and only populate from the final HTTP response. Set these in "
              f"backend/.env (see backend/.env.example) to enable live updates.")
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
    event_type: "EventType | str",
    session_id: str = None,
    agent: str = None,
    path: str = None,
    payload: dict = None,
) -> bool:
    """
    Fires one event on session_id's channel. Part 6.3 schema:
        {type, session_id, agent, path, timestamp, payload}

    event_type may be an EventType member (preferred -- a typo becomes
    an AttributeError at the call site, before this function is ever
    reached) or a plain string (kept for backward compat with existing
    call sites and tests). Either way it's normalized to a plain str
    below, so the event actually sent over the wire always has a plain
    string `type`, never an Enum instance.

    Returns True if the event was sent, False if it was skipped (no
    session_id, Pusher not configured, or an unrecognized event_type)
    or failed. Callers should NOT branch on this return value for
    control flow -- it exists for tests and optional logging only. An
    agent's real work must never depend on whether its event emission
    succeeded (Part 1's whole point: the relay is a side channel, never
    the source of truth).

    An unrecognized event_type is logged and skipped, never raised --
    this used to raise ValueError, which meant a single orphaned string
    literal (missing from VALID_EVENT_TYPES, or a plain typo) could
    take down an entire task run mid-execution, well after real agent
    work had already happened. That directly contradicted this module's
    own design rule above ("an event-emission failure must NEVER take
    down the actual agent work riding alongside it") -- the raise was
    the one place this function didn't actually follow that rule. Every
    other failure mode here (no session_id, Pusher unconfigured, Pusher
    trigger() throwing) already degrades to a logged False instead of
    propagating; this makes unknown-event-type consistent with those,
    the same way a lint rule catches inconsistent error handling.
    """
    if session_id is None:
        return False  # no-op path: no channel to publish on

    if isinstance(event_type, EventType):
        event_type = event_type.value

    if event_type not in VALID_EVENT_TYPES:
        print(f"  [relay] emit_event(): unknown event type {event_type!r}, skipping "
              f"(not one of {len(VALID_EVENT_TYPES)} known EventType values). "
              f"This event was NOT sent -- add it to EventType in relay/emitter.py "
              f"if it's a real, intentional event.")
        return False

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
    event_type: "EventType | str",
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

    event_type accepts an EventType member or plain string, same as
    emit_event() -- see that function's docstring for why, including
    why an unrecognized event_type is now logged and skipped rather
    than raised.
    """
    if user_id is None:
        return False  # no-op path: no channel to publish on

    if isinstance(event_type, EventType):
        event_type = event_type.value

    if event_type not in VALID_EVENT_TYPES:
        print(f"  [relay] emit_user_event(): unknown event type {event_type!r}, "
              f"skipping (not one of {len(VALID_EVENT_TYPES)} known EventType "
              f"values). This event was NOT sent -- add it to EventType in "
              f"relay/emitter.py if it's a real, intentional event.")
        return False

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


def emit_workspace_event(
    event_type: "EventType | str",
    workspace_id: str = None,
    agent: str = None,
    payload: dict = None,
) -> bool:
    """Live-refetch fix (patch 3 follow-up): same fire-and-forget
    contract as emit_event()/emit_user_event() above, just publishing on
    a workspace's own channel instead of a session's or a person's. Kept
    as its own function for the same reason emit_user_event() is its own
    function rather than a branch on emit_event() — a different "no-op
    if missing" key (workspace_id) and a different intended call site
    (api/task_runner.py's _write_plan_panels(), a background write-back
    step, not a mid-run agent) would make one branchy function harder to
    read than three small ones.

    event_type accepts an EventType member or plain string, same as
    emit_event()/emit_user_event() -- see emit_event()'s docstring for
    why, including why an unrecognized event_type is logged and skipped
    rather than raised.
    """
    if workspace_id is None:
        return False  # no-op path: no channel to publish on

    if isinstance(event_type, EventType):
        event_type = event_type.value

    if event_type not in VALID_EVENT_TYPES:
        print(f"  [relay] emit_workspace_event(): unknown event type {event_type!r}, "
              f"skipping (not one of {len(VALID_EVENT_TYPES)} known EventType "
              f"values). This event was NOT sent -- add it to EventType in "
              f"relay/emitter.py if it's a real, intentional event.")
        return False

    client = _get_client()
    if client is None:
        return False

    event = {
        "type": event_type,
        "workspace_id": workspace_id,
        "agent": agent,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "payload": payload or {},
    }

    try:
        client.trigger(_workspace_channel_name(workspace_id), event_type, event)
        return True
    except Exception as exc:
        print(f"  [relay] emit_workspace_event({event_type!r}) failed: {exc}")
        return False