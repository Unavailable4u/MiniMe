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
import json
import os
import re
import sys
from datetime import UTC, datetime
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
    # _write_code_files() once per batch of successful workspace_code_files
    # writes (coalesced, perf follow-up -- was one CODE_FILE_UPDATED per
    # file, which meant N separate frontend file-tree re-fetches for a
    # single task), same "workspace channel, not session channel"
    # reasoning as PANEL_CONTENT_UPDATED immediately above — a code file
    # write needs to reach every dock/tab that has that WORKSPACE's Build
    # tab open, not just the session whose chat turn produced it. Payload
    # shape: {file_path, file_paths, workspace_id} -- file_paths is the
    # full list of changed paths for the batch; file_path is kept (set to
    # the last path) for any consumer still reading the old singular key.
    # Frontend handler: BuildTab.jsx's workspace-${selected.id}
    # subscription (patch 10, once the file-tree view exists to actually
    # react to it).
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

    # F2 Part 5: local-daemon tool-call log, fired on the WORKSPACE
    # channel (emit_workspace_event, same as PANEL_CONTENT_UPDATED/
    # CODE_FILE_UPDATED above) rather than a session channel -- a
    # local tool call isn't tied to any one chat turn the way an
    # agent_start/agent_done pair is, and (per F2's own docs) the
    # daemon itself has no session_id to hand back anyway. All four
    # land in the frontend's decisionEvents array (CO4 patch 3's
    # array, WorkspaceDockContext.jsx), the same non-agent-step
    # "something happened mid-run" home cache_hit/worker_pool_
    # selection already use -- see that file's handleDockEvent and
    # CO4 patch 4's rendering in AgentStepList.jsx/RoutingTraceGraph.jsx.
    #   - LOCAL_TOOL_PROPOSED: eo/local_workspace_tools.py's
    #     propose_action(), for write_file/delete/execute_command only
    #     -- nothing has touched the daemon yet.
    #   - LOCAL_TOOL_CONFIRMED: confirm_action(), right before the
    #     now-approved action is actually sent to the daemon.
    #   - LOCAL_TOOL_DENIED: deny_action() -- the daemon is never
    #     contacted for this proposal.
    #   - LOCAL_TOOL_EXECUTED: list_workspace_dir()/read_workspace_file()
    #     -- the read-only pair, which (per F2's plan) runs freely with
    #     no propose/confirm step, so this is both this pair's "start"
    #     and its "confirmed" equivalent, fired right before call_daemon().
    #   - LOCAL_TOOL_RESULT: fired after call_daemon() returns or raises,
    #     for BOTH the read path (right after LOCAL_TOOL_EXECUTED) and
    #     the mutating path (right after LOCAL_TOOL_CONFIRMED) -- one
    #     result event type regardless of which path got it there, since
    #     by that point it's the same {ok, error?} shape either way.
    LOCAL_TOOL_PROPOSED = "local_tool_proposed"
    LOCAL_TOOL_CONFIRMED = "local_tool_confirmed"
    LOCAL_TOOL_DENIED = "local_tool_denied"
    LOCAL_TOOL_EXECUTED = "local_tool_executed"
    LOCAL_TOOL_RESULT = "local_tool_result"
    # NEW -- Part 7. One event per line of live execute_command output
    # (eo/local_workspace.py's _forward_stream_chunk), fired on the same
    # workspace channel as the five LOCAL_TOOL_* events above, so the
    # terminal panel and the tool-call timeline both read off one
    # channel. Payload: {"action_id", "stream": "stdout"|"stderr",
    # "chunk"}. Deliberately NOT part of the AgentStepList/
    # RoutingTraceGraph timeline the other LOCAL_TOOL_* events feed --
    # a chip per output line would flood that trace UI; this one is
    # consumed only by Part 7's terminal panel.
    LOCAL_TOOL_STREAM_CHUNK = "local_tool_stream_chunk"

    # Patch A3: agent-triggered external MCP tool-call log, fired on the
    # WORKSPACE channel -- same reasoning as the LOCAL_TOOL_* block just
    # above (an MCP tool call isn't tied to one chat turn the way an
    # agent_start/agent_done pair is), and deliberately the SAME
    # emit_workspace_event() mechanism, not a parallel one, so an
    # agent-triggered "GitHub MCP" call lands in the exact same
    # decisionEvents timeline a daemon write_file call already does
    # (see eo/mcp_agent_tools.py's call_agent_mcp_tool(), and its own
    # docstring on why this is Patch A3's explicit key requirement).
    #   - MCP_TOOL_CALLED: fired right before eo.mcp_client.call_mcp_tool()
    #     -- for a read_only-classified MCP tool this is the whole story
    #     (no propose/confirm step, same as LOCAL_TOOL_EXECUTED); for a
    #     mutating-classified tool it fires once more, from INSIDE the
    #     confirm step below, right before the now-approved call actually
    #     reaches the MCP server.
    #   - MCP_TOOL_RESULT: fired after call_mcp_tool() returns or raises,
    #     same {ok, error?} shape as LOCAL_TOOL_RESULT.
    MCP_TOOL_CALLED = "mcp_tool_called"
    MCP_TOOL_RESULT = "mcp_tool_result"

    # Patch A4: propose/confirm/deny lifecycle for MUTATING-classified
    # MCP tools (eo.mcp_registry.classify_tool() == "mutating") --
    # the exact same three-event shape as LOCAL_TOOL_PROPOSED/
    # _CONFIRMED/_DENIED above, extended to a second action source
    # rather than inventing a parallel gating mechanism (see
    # eo/local_workspace_tools.py's propose_mcp_action(), and Patch
    # A4's own "extend it, don't duplicate it" scope note). A
    # read_only-classified MCP tool never emits any of these three --
    # it only ever gets MCP_TOOL_CALLED/_RESULT above, same as before
    # this patch.
    #   - MCP_TOOL_PROPOSED: propose_mcp_action() -- nothing has
    #     touched the MCP server yet.
    #   - MCP_TOOL_CONFIRMED: confirm_action(), right before the
    #     now-approved call is handed to call_agent_mcp_tool() (which
    #     then fires its own MCP_TOOL_CALLED/_RESULT pair around the
    #     actual eo.mcp_client.call_mcp_tool() round trip).
    #   - MCP_TOOL_DENIED: deny_action() -- the MCP server is never
    #     contacted for this proposal.
    MCP_TOOL_PROPOSED = "mcp_tool_proposed"
    MCP_TOOL_CONFIRMED = "mcp_tool_confirmed"
    MCP_TOOL_DENIED = "mcp_tool_denied"


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


def _patch_pusher_double_escaping(pusher_module) -> None:
    """Works around a bug in the third-party `pusher` package itself
    (pusher/http.py's Request.__init__), not our code -- so it can't be
    fixed by editing anything else in this repo.

    Request.__init__ builds the outer HTTP body with a plain
    `json.dumps(params)`, which defaults to ensure_ascii=True. But
    `params["data"]` was already serialized upstream by
    pusher/util.py's data_to_string() WITH ensure_ascii=False (so that
    non-ASCII bytes count as 1-4 UTF-8 bytes each). Wrapping that
    already-serialized string in a second, ascii-only json.dumps()
    re-escapes every non-ASCII character into a 6-byte \\uXXXX sequence,
    silently inflating the real wire size Pusher measures against its
    10,240-byte event cap -- well past whatever budget our own
    truncation upstream (see eo/executor.py's render_agent_result() /
    _summarize()) correctly enforced against the raw UTF-8 string. This
    is exactly why symbol-dense payloads (e.g. hardware_speccer's specs,
    full of Ω/µF/°C/etc.) can trip the 413 even though they measured
    in-budget before being handed to Pusher.

    Scoped to pusher.http.Request only (not a global json.dumps
    monkeypatch) so it can't change JSON encoding behavior anywhere else
    in the app. Idempotent -- safe to call every time _get_client()
    builds (or would build) a client.
    """
    import json as _json

    request_cls = pusher_module.http.Request
    if getattr(request_cls, "_minime_ascii_patch_applied", False):
        return
    _orig_init = request_cls.__init__

    def _patched_init(self, client, method, path, params=None):
        _orig_init(self, client, method, path, params)
        if method == pusher_module.http.POST and params is not None:
            # Re-encode with ensure_ascii=False and re-run auth signing,
            # since body_md5/auth_signature were computed off the
            # (wrong) ascii-escaped body by _orig_init above.
            #
            # Bug fix: _orig_init()'s call to _generate_auth() already
            # wrote an 'auth_signature' key into self.query_params (Pusher's
            # own Request._generate_auth() does query_params.update({...})
            # for auth_key/body_md5/auth_version/auth_timestamp, then
            # separately sets query_params['auth_signature'] afterwards --
            # nothing ever clears that key). Calling _generate_auth() again
            # here recomputes body_md5/auth_timestamp/etc. correctly, but
            # the auth_string it signs is built from make_query_string(self.
            # query_params) BEFORE the new auth_signature is assigned --
            # which means it still contains the STALE auth_signature from
            # the first call as a query param, so this second signature is
            # computed over a string Pusher's server never expects (it only
            # ever signs auth_key/body_md5/auth_version/auth_timestamp,
            # never auth_signature itself). Pusher then rejects every event
            # with "Invalid signature: you should have sent
            # HmacSHA256Hex(<our 4-param string>, secret), but you sent
            # <our 5-param signature>" -- exactly the errors flooding the
            # logs. Popping the stale key before re-signing makes this
            # second _generate_auth() call produce the same 4-param auth
            # string Pusher itself computes.
            self.query_params.pop("auth_signature", None)
            self.body = _json.dumps(params, ensure_ascii=False).encode("utf8")
            self._generate_auth()

    request_cls.__init__ = _patched_init
    request_cls._minime_ascii_patch_applied = True


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

    _patch_pusher_double_escaping(pusher)

    _pusher_client = pusher.Pusher(
        app_id=app_id, key=key, secret=secret, cluster=cluster, ssl=True,
    )
    return _pusher_client


# Bug fix 2026-08-14: Pusher's ~10KB cap is measured against the fully
# JSON-serialized event *envelope* on the wire ({type, session_id,
# agent, path, timestamp, payload}), not against any single field's raw
# byte length. eo/result_render.py's render_agent_result() already
# truncates payload["summary"] to a raw-UTF-8-byte budget (its own
# 2026-08-12 fix), which correctly bounds *that field's* bytes -- but
# JSON string-escaping (every '"', '\\', and control character,
# including every '\n', becomes 2 bytes on the wire) is mandatory per
# the JSON spec and has nothing to do with non-ASCII bytes. A summary
# that's pure ASCII but dense in quotes/newlines -- exactly the shape
# of the "```json\n" + json.dumps(result, indent=2) + "\n```" fallback
# a few lines up in that file -- can measure safely under
# render_agent_result()'s own limit and still push the assembled
# envelope over Pusher's cap once escaping is applied. This is the one
# place that actually assembles (and can measure) the final wire size,
# so it's the right place to enforce the real cap.
PUSHER_EVENT_BYTE_CAP = 10240
_FALLBACK_SUMMARY = "[output too large to stream -- see final result]"


def _fit_event_to_pusher_cap(event: dict, cap: int = PUSHER_EVENT_BYTE_CAP) -> dict:
    """Returns `event` unchanged if it already serializes under `cap`
    bytes, otherwise a shrunk copy that (best-effort) does. Degrades in
    three steps, each attempted only if the previous one wasn't enough:

      1. Drop payload["image"] -- routinely the single largest field,
         and the least essential to a live "done" transition.
      2. Truncate payload["summary"] on a byte boundary (same
         errors="ignore" decode render_agent_result() itself uses),
         shrinking just enough to clear the cap once the rest of the
         envelope's own (escaped) size is accounted for.
      3. If the envelope is still over cap even with the summary
         emptied out -- some other field is what's actually oversized
         -- replace the whole payload with a minimal placeholder.

    Step 3 is what actually closes the "frontend never learns the step
    finished" gap: this function is called before client.trigger()
    below, so a run that would have silently 413'd instead sends
    *something* that fits -- letting SessionContext.jsx still pop its
    openStepStack and flip the step to "done" -- rather than emit_event()
    swallowing the exception and returning False with nothing sent.

    0b fix: this function only returns a shrunk COPY (`shrunk`) when it
    had to degrade the payload -- callers (emit_event/emit_user_event/
    emit_workspace_event) compare the return value against the original
    `event` by identity and, on a mismatch, stamp `truncated: True` onto
    the envelope before sending. That flag plus the identifier already
    on every envelope (session_id/user_id/workspace_id) is what lets the
    frontend tell "this is the whole result" apart from "this is a
    shrunk stand-in, go re-fetch the real thing" instead of trusting
    whatever arrived as complete.

    Never raises. Worst case (a genuinely unserializable event, e.g. a
    stray non-JSON-safe object slipping into payload), returns `event`
    unchanged and lets the caller's own try/except around
    client.trigger() handle it exactly like any other Pusher failure.
    """
    try:
        if len(json.dumps(event, ensure_ascii=False).encode("utf-8")) <= cap:
            return event
    except Exception:
        return event

    payload = dict(event.get("payload") or {})
    shrunk = dict(event)

    # Step 1: image first -- biggest and least essential to a live
    # status update.
    if "image" in payload:
        trimmed = {k: v for k, v in payload.items() if k != "image"}
        shrunk["payload"] = trimmed
        try:
            if len(json.dumps(shrunk, ensure_ascii=False).encode("utf-8")) <= cap:
                return shrunk
        except Exception:
            pass
        payload = trimmed

    # Step 2: truncate payload["summary"] to whatever budget is left
    # once the rest of the (already-escaped) envelope is accounted for.
    summary = payload.get("summary")
    if isinstance(summary, str) and summary:
        probe = dict(payload)
        probe["summary"] = ""
        probe_event = dict(shrunk)
        probe_event["payload"] = probe
        try:
            overhead = len(json.dumps(probe_event, ensure_ascii=False).encode("utf-8"))
        except Exception:
            overhead = cap  # forces a fall-through to step 3 below
        marker = "\n\n... [truncated for delivery]"
        marker_bytes = len(marker.encode("utf-8"))
        budget = cap - overhead - marker_bytes
        if budget > 0:
            encoded = summary.encode("utf-8")
            cut = min(budget, len(encoded))
            # JSON escaping isn't 1:1 with raw bytes, so a slice that's
            # under `budget` raw bytes can still overshoot after
            # re-serializing if it happens to land on a lot of
            # quote/backslash/newline characters -- shrink and retry
            # rather than assume the first cut clears the cap.
            while cut > 0:
                candidate = encoded[:cut].decode("utf-8", errors="ignore") + marker
                probe["summary"] = candidate
                probe_event["payload"] = probe
                try:
                    size = len(json.dumps(probe_event, ensure_ascii=False).encode("utf-8"))
                except Exception:
                    break
                if size <= cap:
                    shrunk["payload"] = probe
                    return shrunk
                cut = int(cut * 0.9)
        # Budget collapsed to ~nothing even for an emptied-out summary
        # -- fall through to step 3 instead of sending an unreadable
        # sliver of text.

    # Step 3: something other than image/summary is oversized, or the
    # summary budget above collapsed to nothing -- send a minimal
    # placeholder instead of nothing at all.
    minimal_payload = {"summary": _FALLBACK_SUMMARY}
    if isinstance(payload.get("duration_ms"), (int, float)):
        minimal_payload["duration_ms"] = payload["duration_ms"]
    shrunk["payload"] = minimal_payload
    return shrunk


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
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": payload or {},
    }

    # Bug fix 2026-08-14: enforce Pusher's real ~10KB cap against the
    # fully assembled envelope, not just render_agent_result()'s own
    # raw-byte budget on the summary text alone -- see
    # _fit_event_to_pusher_cap()'s docstring above for why those two
    # numbers can diverge.
    fitted = _fit_event_to_pusher_cap(event)
    if fitted is not event:
        # Bug 7 fix (0b): mark the envelope itself as truncated, not just
        # the shrunk payload -- session_id is already on every envelope
        # (Part 6.3's schema), so this is enough for the frontend to
        # know both THAT it needs to re-fetch and WHICH session's step
        # to re-fetch it for, instead of silently trusting a shrunk
        # summary as the complete result.
        fitted = dict(fitted)
        fitted["truncated"] = True
        print(f"  [relay] emit_event({event_type!r}): payload exceeded Pusher's "
              f"{PUSHER_EVENT_BYTE_CAP}-byte cap once serialized, sent a "
              f"shrunk/fallback payload instead so the frontend still gets "
              f"a completion event (truncated=True, session_id={session_id!r}).")
    event = fitted

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
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": payload or {},
    }

    # Same envelope-size gap as emit_event() above -- see
    # _fit_event_to_pusher_cap()'s docstring.
    fitted = _fit_event_to_pusher_cap(event)
    if fitted is not event:
        # Same 0b fix as emit_event() above -- user_id is already on
        # this envelope, so it doubles as the re-fetch identifier here.
        fitted = dict(fitted)
        fitted["truncated"] = True
        print(f"  [relay] emit_user_event({event_type!r}): payload exceeded "
              f"Pusher's {PUSHER_EVENT_BYTE_CAP}-byte cap once serialized, "
              f"sent a shrunk/fallback payload instead (truncated=True, "
              f"user_id={user_id!r}).")
    event = fitted

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
        "timestamp": datetime.now(UTC).isoformat(),
        "payload": payload or {},
    }

    # Same envelope-size gap as emit_event() above -- see
    # _fit_event_to_pusher_cap()'s docstring.
    fitted = _fit_event_to_pusher_cap(event)
    if fitted is not event:
        # Same 0b fix as emit_event() above -- workspace_id is already on
        # this envelope, so it doubles as the re-fetch identifier here.
        fitted = dict(fitted)
        fitted["truncated"] = True
        print(f"  [relay] emit_workspace_event({event_type!r}): payload exceeded "
              f"Pusher's {PUSHER_EVENT_BYTE_CAP}-byte cap once serialized, "
              f"sent a shrunk/fallback payload instead (truncated=True, "
              f"workspace_id={workspace_id!r}).")
    event = fitted

    try:
        client.trigger(_workspace_channel_name(workspace_id), event_type, event)
        return True
    except Exception as exc:
        print(f"  [relay] emit_workspace_event({event_type!r}) failed: {exc}")
        return False