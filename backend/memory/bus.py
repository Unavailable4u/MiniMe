import os
import json
import re
import contextvars
from dotenv import load_dotenv
from upstash_redis import Redis
from upstash_vector import Index
load_dotenv()
redis = Redis(
    url=os.getenv("UPSTASH_REDIS_REST_URL"),
    token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
)
def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return slug[:max_len] or "untitled_app"

# Migration Part B (session isolation fix): replaces the old plain
# module-level global `_app_slug_cache`. A bare global is shared across
# EVERY concurrent request in this process -- with the tier-3 adaptive
# path never explicitly scoping it, every session's module_specs/
# current_plan/submitted_code/test_code/etc. silently collided in the
# same namespace. A ContextVar is per-request/per-task instead:
# FastAPI's threadpool for sync endpoints (Starlette's
# run_in_threadpool) correctly propagates context into the worker
# thread, so this isolates concurrent HTTP requests properly, unlike
# the old global.
_app_slug_ctx: "contextvars.ContextVar" = contextvars.ContextVar("app_slug", default=None)


def _current_app_slug():
    """Namespacing lookup used internally by _namespaced() for every
    ordinary bus key. Context-scoped value wins if set_app_slug() was
    called this request/task; otherwise falls back to the persisted
    Redis global, which preserves the original CLI/tier-2 behavior
    ("keep working on whatever app was last loaded") for callers that
    never explicitly scope a context."""
    ctx_value = _app_slug_ctx.get()
    if ctx_value is not None:
        return ctx_value
    raw = redis.get("app_slug")
    if raw is not None:
        return json.loads(raw)
    return None


def set_app_slug(slug: str) -> None:
    """Scopes every bus read/write in THIS request/task context to
    `slug`, WITHOUT touching the persisted global Redis "app_slug"
    record. Call this once, at the very top of every tier-3
    adaptive-path run, keyed by session_id -- this is what actually
    stops unrelated sessions from sharing module_specs/current_plan/
    submitted_code/test_code/etc.

    Deliberately does not persist to Redis: write(KEYS["app_slug"], ...)
    still does that, and is reserved for tier-2/CLI's "load this app and
    keep working on it across separate invocations" behavior, which
    this function must not interfere with."""
    _app_slug_ctx.set(slug)


def get_current_app_slug():
    """Public accessor for "the app slug this run is scoped to" as a
    piece of DATA (e.g. for building a disk path), not just as a key
    prefix. Agents that need this (see agents/file_manager.py) should
    call this directly rather than read(KEYS["app_slug"])/
    write(KEYS["app_slug"], ...) -- that pair talks to the raw,
    unnamespaced global Redis record (see _namespaced()'s exemption
    list below) and would silently clobber a session-scoped context
    value set via set_app_slug() above."""
    return _current_app_slug()
def _namespaced(key: str) -> str:
    """Prefixes every key with the active app_slug, except app_slug itself
    (bootstrap key, can't prefix itself), project_registry (Part 3 step 6
    -- Cross-Project File Control tracks projects across the whole
    system, not any one app_slug), usage:* keys (Part 7.1 -- quota is a
    property of your accounts, not any one project), registry:* keys
    (Part 7 -- the role-prompt and role-to-agent registries are also
    properties of the SYSTEM, not any one project, same reasoning as
    usage:* and project_registry above), conversation:* keys (Part 23
    -- a conversation is a property of the SESSION, not whatever
    app_slug happens to be active when a given message lands, since a
    single session isn't reliably tied to one app_slug across its
    lifetime), and workspace_facts:* keys (Bug fix 2026-08-12: a
    workspace is its own stable identity, not a property of whatever
    app_slug happens to be active in the calling context -- same
    reasoning as conversation:/paused_execution: below. Without this
    exemption, eo/workspace_facts.py's set_facts()/get_facts() silently
    disagree on the actual Redis key whenever the two calls run in
    different app_slug contexts: a POST /api/task request scopes
    app_slug to task_runner.py's per-run slug (so e.g.
    hardware_speccer's write lands under
    "{task_slug}:workspace_facts:{workspace_id}"), while a plain GET
    like /api/workspaces/{ws_id}/device-spec never calls
    set_app_slug() and falls back to the persisted GLOBAL app_slug
    record instead -- a different key, even though workspace_id is
    identical on both sides. The write succeeds, the read 200s with an
    empty-but-valid shape, and nothing anywhere raises -- it just
    silently looks like the data was never generated)."""
    if (key == "app_slug" or key == "project_registry"
            or key.startswith("usage:") or key.startswith("registry:")
            or key.startswith("conversation:")   # NEW — Part 23
            or key.startswith("workspace_facts:")   # Bug fix 2026-08-12 — see docstring above
            or key.startswith("cooldown_until:")   # Fix B (reliability guide):
            # same reasoning as usage: above -- a provider account's
            # rate-limit cooldown is a property of the ACCOUNT, not
            # whatever app_slug happens to be active. eo/panel.py's
            # _best_match() needs to see the same cooldown regardless of
            # which project/app triggered the 429 that set it.
            or key.startswith("paused_execution:")   # NEW — Part 2 §2.4:
            # a paused run's snapshot is a property of the SESSION, exactly the
            # same reasoning as conversation: above -- and it HAS to be exempt,
            # for a structural reason conversation: doesn't share: the snapshot
            # itself is what tells resume_graph() which app_slug the original
            # run used. If this key were namespaced, reading it back on a fresh
            # POST /api/resume request (which hasn't called set_app_slug() yet
            # -- that's literally the value this key is about to supply) would
            # look in the wrong namespace, or the default/global one, and
            # silently miss.
            or key.startswith("pending_synthesis:")):   # NEW — CO5 Finding A:
        # same exemption, same reasoning as paused_execution: above. This
        # key is written mid-_run_tier3_hires() (still inside the
        # POST /api/task request, before set_app_slug() context is torn
        # down) and read back later from GET /api/task/{session_id}/stream
        # -- a fresh request that hasn't called set_app_slug() either. If
        # this were namespaced, the stream endpoint's read() would land in
        # the wrong (or default/global) namespace and silently miss, same
        # failure mode paused_execution: was exempted to avoid.
        return key
    slug = _current_app_slug()
    return f"{slug}:{key}" if slug else key
# DB4 — Vector (Part 5, memory schema). Used by Cross-Cycle Memory Search
# (#0) and the Duplication/Similarity Checker (#7). Both share this one
# index but use different id-prefix namespaces (see each agent's docstring)
# so their embeddings never collide or get queried against each other.
_vector_index = None
def vector_index() -> Index:
    """Lazy singleton so importing bus.py doesn't require Vector env vars
    to be set for code paths that never touch it (most agents don't)."""
    global _vector_index
    if _vector_index is None:
        url = os.getenv("UPSTASH_VECTOR_REST_URL")
        token = os.getenv("UPSTASH_VECTOR_REST_TOKEN")
        if not url or not token:
            raise RuntimeError(
                "UPSTASH_VECTOR_REST_URL / UPSTASH_VECTOR_REST_TOKEN not set. "
                "Create the Vector index in your Upstash console (Part 5, DB4) "
                "and add both to .env."
            )
        _vector_index = Index(url=url, token=token)
    return _vector_index
def write(key: str, value, ex: int = None):
    """Write any JSON-serializable value to memory.

    ex: optional TTL in seconds. NEW — CO5 Step 7 follow-up: a safety
    net for keys like pending_synthesis:{session_id} that are meant to
    be short-lived and explicitly deleted by whatever consumes them
    (see api/routes/tasks.py's stream_answer()), but would otherwise
    accumulate in the bus forever if that consumer never runs (client
    never opens the SSE connection, tab closed mid-request, etc.).
    Omitted (None) preserves every existing caller's behavior exactly
    -- a plain, non-expiring SET, same as before this param existed.
    """
    redis.set(_namespaced(key), json.dumps(value), ex=ex)
    if key == "app_slug":
        # Keeps the context-local value in sync with an explicit
        # write(KEYS["app_slug"], ...) call within the SAME
        # request/task (e.g. tier-2's load_existing_app()), so a
        # later read in this same context sees it immediately without
        # a round-trip to Redis.
        _app_slug_ctx.set(value)
def read(key: str, default=None):
    """Read a value back from memory. Returns default if not found."""
    raw = redis.get(_namespaced(key))
    if raw is None:
        return default
    value = json.loads(raw)
    if key == "app_slug":
        _app_slug_ctx.set(value)
    return value
def read_stage_output_text(session_id: str, role: str) -> str | None:
    """Bug fix (2026-08-12): reads a stage_output:{session_id}:{role} key
    and returns its text content regardless of which of the two shapes
    that key legitimately holds -- the two write sites had silently
    drifted apart, and every one of the four existing readers only
    handled ONE of the two shapes, not both:

      1. A plain string -- what agents/generic_worker.py's own run()
         writes on an ordinary (non-paused) completion
         (bus_write(f"stage_output:{{session_id}}:{{role}}", body), body
         being a str). agents/hardware_speccer.py, agents/
         schema_diagrammer.py, and agents/architecture_diagrammer.py's
         near-identical _read_prd_context() helpers only checked
         isinstance(..., dict), so this shape -- the NORMAL case for an
         ordinary, non-approval-gated run -- always fell through to
         MissingDependencyError even when prd_writer had just finished
         successfully. This is what surfaced as hardware_speccer
         raising MissingDependencyError("prd_writer") immediately after
         the executor's own self-heal branch had already run prd_writer
         and confirmed it "done".
      2. A dict shaped like {"text": ..., ...}. -- what
         eo/executor.py's own approval-pause "edit" action writes
         (write(f"stage_output:{{session_id}}:{{role}}", edited)) when a
         role sits in approval_roles and a person edits its output
         before approving it. agents/handoff_packager.py's own
         _read_stage_text() only checked isinstance(..., str), so THIS
         shape -- an approval-edited prd_writer -- would have hit the
         mirror-image bug there instead: MissingDependencyError raised
         even though prd_writer had genuinely run, just via the
         edit-then-approve path rather than a plain completion.

    Single shared helper so both shapes are handled in exactly one
    place, rather than four independent copies that can each drift out
    of sync with the two real write sites again. Returns None (not "")
    for "nothing usable yet" either way, matching every existing
    caller's own `if not prd_text: raise MissingDependencyError(...)`
    (or equivalent) convention.
    """
    stage_output = read(f"stage_output:{session_id}:{role}", default=None)
    if isinstance(stage_output, dict):
        text = stage_output.get("text")
        return text if isinstance(text, str) and text.strip() else None
    if isinstance(stage_output, str):
        return stage_output if stage_output.strip() else None
    return None


def read_many(keys: list, default=None) -> dict:
    """Batch-read multiple keys in a SINGLE Redis round trip via MGET,
    instead of one request per key. Since bus.py talks to Upstash Redis
    over REST, every individual read() is a full HTTPS request -- calling
    read() in a loop over N keys means N sequential (or, at best,
    N concurrent) network round trips. MGET fetches all of them in one
    request instead.

    Returns {key: value_or_default}, keyed by the ORIGINAL (un-namespaced)
    keys passed in, mirroring read()'s per-key namespacing behavior so
    callers don't need to think about _namespaced() themselves. Does not
    special-case "app_slug" the way read() does -- this is meant for
    bulk, read-only rollups (e.g. eo/quota_sentinel.py's usage history),
    not for the single bootstrap key that participates in the app_slug
    context-var sync.
    """
    if not keys:
        return {}
    namespaced_keys = [_namespaced(k) for k in keys]
    # Fix: Redis.mget() is variadic (mget(*keys)), not a single list arg.
    # Passing the list directly made the client treat it as ONE bogus key,
    # so raw_values came back as a single-element result. zip(keys, raw_values)
    # then silently truncated to just the first key, dropping every other
    # key from the returned dict -- causing KeyError downstream (e.g.
    # eo/quota_sentinel.py's get_usage_history()) for anything but the
    # first key requested. Unpacking with * sends each key as its own
    # argument, matching the client's real signature.
    raw_values = redis.mget(*namespaced_keys)
    return {
        original_key: (json.loads(raw) if raw is not None else default)
        for original_key, raw in zip(keys, raw_values)
    }
def delete(key: str) -> None:
    """Delete a key from memory entirely (as opposed to write(key, [])
    or write(key, None), which leave a namespaced key sitting in Redis
    with an empty/null value forever). Used by eo/chat_store.py's
    delete_chat() to actually clear a session's conversation history."""
    redis.delete(_namespaced(key))
def append_cycle_history(cycle_num: int, report: dict):
    """Store each cycle's report under its own key, for long-term memory."""
    write(f"cycle:{cycle_num}:report", report)
# Standard memory keys used across the loop
KEYS = {
    "original_idea": "original_idea",
    "current_plan": "current_plan",
    "module_specs": "module_specs",
    "submitted_code": "submitted_code",
    "test_code": "test_code",
    "review_notes": "review_notes",
    "fixed_code": "fixed_code",
    "test_results": "test_results",
    "commit_message": "commit_message",
    "changelog_entry": "changelog_entry",
    "latest_report": "latest_report",
    "cycle_count": "cycle_count",
    "loop_decision": "loop_decision",
    "feature_status": "feature_status",
    "file_map": "file_map",
    "app_slug": "app_slug",
    # --- new: the 6 backfilled tier-3 agents ---
    "retrieved_context": "retrieved_context",        # #0 Cross-Cycle Memory Search output
    "dependency_map": "dependency_map",               # #4 Module Dependency Mapper
    "duplication_report": "duplication_report",         # #7 Duplication/Similarity Checker
    "academic_search_report": "academic_search_report",  # Part 3 §3.3
    "web_researcher_report": "web_researcher_report",     # task 13c
    "extraction_table": "extraction_table",              # Part 3 §3.5
    "contradiction_candidates": "contradiction_candidates",  # Part 3 §3.6
    "dataset_analysis": "dataset_analysis",              # Part 3 §3.7
    "dataset_path": "dataset_path",                      # Part 3 §3.7 — reserved, no writer yet
    "source_quality_report": "source_quality_report",    # Part 3 §3.8
    "citation_graph": "citation_graph",                   # Part 3
    "security_scan_results": "security_scan_results", # #12 Security/Dependency Scanner Pool
    "doc_output": "doc_output",                        # #14 Documentation Agent
    "final_qa_verdict": "final_qa_verdict",             # #16 Final QA/Acceptance Reviewer
    # --- Tier-1 lean pipeline (Part 2.4) --- deliberately separate key
    # names from the tier-3 keys above, even though the shapes are
    # similar in places, so a tier-1 run and a tier-3 cycle can never
    # collide if they're ever run back to back on the same app_slug.
    "tier1_task_text": "tier1_task_text",
    "tier1_module_spec": "tier1_module_spec",
    "tier1_code": "tier1_code",
    "tier1_review_notes": "tier1_review_notes",
    "tier1_fixed_code": "tier1_fixed_code",
    "tier1_test_results": "tier1_test_results",
    # --- Part 6 §6.2 — growth domain's content fan-out pool. Same
    # relationship to "content_adapter_pool" that "module_specs"/
    # "submitted_code" have to "code_writers": an upstream generic_worker
    # role (or a task-text fallback) writes content_targets, the pool
    # reads it and writes platform_content back.
    "content_targets": "content_targets",
    "platform_content": "platform_content",
    # Patch 8 (rollout guide §3) — performance_reviewer's real-execution
    # profiling result. Same shape family as "dataset_analysis" above:
    # {"passed", "stdout", "stderr", "error"} straight from
    # sandbox_tester.py's _run_one_module(), plus this module's own
    # "parsed_result" (the JSON summary the profiling harness was told to
    # print as its last stdout line) and "module" (which fixed_code/
    # submitted_code entry this result is about, since -- unlike
    # dataset_analyst, which analyzes one dataset -- this role picks ONE
    # module per run from a dict of several; see agents/
    # performance_reviewer.py's own docstring for the selection rule).
    "performance_review": "performance_review",
}