"""
eo/conversation_memory.py — Part 23. A shared, per-session conversation
transcript, so a follow-up message ("make it shorter", "now add auth too")
has real prior context to work from instead of being treated as the very
first message ever sent.

Two read modes, deliberately different in size/detail:
  - get_full_context(): real prior turns, fuller detail — for the agents
    that actually generate content and need to build on what came before.
  - get_light_context(): compact one-line-per-turn summaries — for the
    Inspector/Panel, so a follow-up's tier/complexity can be re-judged
    against what's already been asked/built, without flooding the
    classifier's prompt with full prior answers or corrupting exact-match
    caching (eo/semantic_cache.py) with a growing wall of unrelated text.

Storage: memory/bus.py, under "conversation:{session_id}" — session-
namespaced, not app_slug-namespaced (see memory/bus.py's _namespaced()
exemption list, extended in this same part), since a single session isn't
reliably tied to one app_slug across its lifetime.

NEW — perf audit follow-up (#3): append_turn() used to store every turn
unconditionally, which meant bare acknowledgements ("ok", "thanks!",
"sounds good") sat in the transcript on equal footing with turns that
actually carry information — diluting get_light_context()'s window (the
Inspector/Panel's read of "what's already been asked") and padding every
rolling_summary fold with noise instead of signal. _should_store() below
is a cheap pre-filter in front of the write: two deterministic,
non-LLM tiers (an exact-match filler list to skip; a length floor to
store) handle almost every turn, and only genuinely short-but-unclear
text escalates to a real classification — which reuses eo/sga.py's own
cheap SGA tier, never a full eo/inspector.py Inspector call, so this
doesn't reintroduce the exact classification tax point #1 already
identified. See _should_store()'s own docstring for the store-if-unsure
bias this is built around.
"""
import math
import os
import re
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo import (
    chat_store,  # NEW — cross-chat memory sharing (see §4)
    chat_workspace,  # NEW — Part 0 §0.3, session_id -> workspace_id
    rolling_summary,  # NEW — Patch B9, Tier B: folds trimmed turns instead of dropping them
    user_profile,  # NEW — Patch B3, silent per-account personalization
    workspace_facts,  # NEW — Part 0 §0.3, tier-3 memory
)
from memory.bus import incr, read, write  # incr — NEW, perf audit follow-up (#3): store/skip stats

MAX_STORED_TURNS = 20      # hard cap on raw storage growth per session
FULL_CONTEXT_TURNS = 6     # how many recent turns generation agents see
LIGHT_CONTEXT_TURNS = 6    # how many recent turns the classifier sees
FULL_TURN_CHAR_LIMIT = 1500    # per-turn truncation for the full view
LIGHT_TURN_CHAR_LIMIT = 120     # per-turn truncation for the light view
CONVERSATION_TTL = int(os.getenv("CONVERSATION_TTL_SECONDS", str(60 * 60 * 24 * 30)))  # 30 days — NEW, perf audit §1: without this, conversation:{session_id} keys never expire and accumulate in Redis forever

# NEW — perf audit follow-up (#3): kill switches, same env-var-toggle
# convention as CONVERSATION_TTL above. FILTER_ENABLED turns the whole
# pre-filter off (every turn stored, exactly like before this patch).
# AMBIGUOUS_LLM_ENABLED turns off only the rare cheap-model escalation
# tier, so a provider outage or cost concern can be dialed back without
# losing the two free deterministic tiers.
CONVERSATION_MEMORY_FILTER_ENABLED = os.getenv("CONVERSATION_MEMORY_FILTER_ENABLED", "true").lower() != "false"
CONVERSATION_MEMORY_AMBIGUOUS_LLM_ENABLED = os.getenv("CONVERSATION_MEMORY_AMBIGUOUS_LLM_ENABLED", "true").lower() != "false"


def _key(session_id: str, thread_id: str = "main") -> str:
    # "main" keeps the exact legacy key shape ("conversation:{session_id}",
    # no suffix) that every pre-branching session's turns already live
    # under, and that scripts/verify_delete_leak.py's delete-leak check
    # looks for by name — so a session that never branches (the
    # overwhelming majority, per _route_to_thread()'s own bias toward
    # staying on the active thread) is byte-for-byte unaffected by this
    # patch. Only a session that actually drifts topic and gets routed
    # to a second thread ever touches the new suffixed key shape below.
    if thread_id == MAIN_THREAD_ID:
        return f"conversation:{session_id}"
    return f"conversation:{session_id}:thread:{thread_id}"


# ---------------------------------------------------------------------
# Topic-branched threads — perf audit follow-up (#3b), built last and
# only after the append_turn() "worth storing?" pre-filter above shipped
# on its own (see that patch's module-doc note and this feature's own
# docstrings below for why the order mattered).
#
# The problem this solves: a single flat per-session turn list means a
# user drifting off-topic and back pollutes get_full_context()/
# get_light_context()'s "recent turns" window with turns from an
# unrelated tangent, which is exactly the kind of noise the Inspector/
# Panel and generation agents shouldn't have to read through.
#
# The shape deliberately mirrors two primitives already in this
# codebase rather than inventing new patterns:
#   - Notebooks' topic_id (api/task_runner.py's _topic_scoped_task_text())
#     is the same idea one level up — "scope retrieval to a specific
#     topic instead of the whole workspace." A caller that already has
#     a topic_id (e.g. a Notebooks-backed chat) can pass it straight
#     through as `thread_id` below instead of paying for auto-routing.
#   - parent_thread_id below is the same shape as eo/chat_store.py's
#     linked_chat_ids — a lightweight pointer between related contexts,
#     not a full tree structure.
#
# Routing itself is deliberately NOT an LLM call — same reasoning
# eo/routing_memory.py already leans on for its own retrieval step:
# utils/embedding.embed_text() is already wired up and cheap, so a new
# user turn is embedded once and compared against each existing
# thread's running centroid via plain cosine similarity. This adds one
# embedding call per *user* turn (assistant turns just follow whatever
# thread the paired user turn landed on), never a second classification
# LLM call — so it doesn't reintroduce the exact "pay for a decision
# before real work" tax point #1 already identified elsewhere in this
# pipeline.
# ---------------------------------------------------------------------

MAIN_THREAD_ID = "main"

# How many distinct topic threads a single session is allowed to
# accumulate before routing starts reusing the stalest one instead of
# minting a new one. Bounded on purpose: an unbounded thread count
# turns one runaway session into unbounded storage keys, and in
# practice a chat session rarely juggles more than a handful of live
# topics at once.
MAX_THREADS_PER_SESSION = int(os.getenv("CONVERSATION_MAX_THREADS", "6"))

# Cosine-similarity floor for "this is the same topic as an existing
# thread." Chosen conservatively high (favoring under-branching over
# over-branching): a false "same thread" merge just costs a little
# context-window noise, same direction _should_store()'s own
# store-if-unsure bias already leans, whereas a false "new thread"
# split fragments context that should have stayed together. Tune from
# get_conversation_thread_stats() below rather than guessing further.
THREAD_SIMILARITY_THRESHOLD = float(os.getenv("CONVERSATION_THREAD_SIMILARITY_THRESHOLD", "0.72"))

CONVERSATION_THREAD_ROUTING_ENABLED = os.getenv("CONVERSATION_THREAD_ROUTING_ENABLED", "true").lower() != "false"

_THREAD_STATS_KEYS = {
    "routed_existing_thread": "conv_thread_stats:routed_existing",
    "created_new_thread": "conv_thread_stats:created_new",
    "reused_stale_thread": "conv_thread_stats:reused_stale",
    "routing_skipped_disabled": "conv_thread_stats:skipped_disabled",
    "routing_skipped_error": "conv_thread_stats:skipped_error",
}


def _threads_key(session_id: str) -> str:
    return f"conversation_threads:{session_id}"


def _active_thread_key(session_id: str) -> str:
    return f"conversation_active_thread:{session_id}"


def _note_thread_stat(reason: str) -> None:
    """Best-effort; never raises — same non-fatal posture as this
    module's _note_store_stat() right above."""
    try:
        incr(_THREAD_STATS_KEYS[reason], ex=_STATS_TTL_SECONDS)
    except Exception as exc:
        print(f"  [Conversation Memory] _note_thread_stat({reason!r}) failed (non-fatal): {exc}")


def get_conversation_thread_stats() -> dict:
    """Returns counts for each path _route_to_thread() can resolve
    through, plus a rollup. Pull this before touching
    THREAD_SIMILARITY_THRESHOLD or MAX_THREADS_PER_SESSION — a low
    created_new_thread rate relative to routed_existing_thread means
    real sessions mostly stay on-topic already and branching rarely
    fires (nothing to tune); a high routing_skipped_error rate means
    the embedding call itself is failing more than the threshold
    matters."""
    counts = {reason: (read(key, default=0) or 0) for reason, key in _THREAD_STATS_KEYS.items()}
    total = sum(counts.values())
    return {**counts, "total_routing_decisions": total}


def _cosine_similarity(a: list, b: list) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embed_for_routing(text: str):
    """Returns an embedding vector for `text`, or None on any failure
    (missing HF key, network error, etc.) — routing always has a safe
    fallback (stay on the active thread, or "main") for a None return,
    so this never needs to raise. Imported lazily, same reasoning
    eo/routing_memory.py already documents for its own _embed import:
    keeps a network/HF dependency out of this module's own import
    chain for callers that never touch threading at all."""
    try:
        from utils.embedding import embed_text
        return embed_text(text)
    except Exception as exc:
        print(f"  [Conversation Memory] thread-routing embed skipped (non-fatal): {exc}")
        return None


def _route_to_thread(session_id: str, text: str, explicit_thread_id: str = None) -> str:
    """Returns the thread_id a new *user* turn belongs to, and updates
    the thread registry + active-thread pointer to match. Assistant
    turns never call this directly — see append_turn()'s own docstring
    — they just follow whatever the paired user turn already resolved.

    explicit_thread_id: a caller that already knows the topic (e.g. a
    Notebooks-backed chat passing its own topic_id straight through,
    mirroring api/task_runner.py's _topic_scoped_task_text() one layer
    up) skips auto-routing entirely and just registers/refreshes that
    thread's centroid. This is the escape hatch for a caller who
    shouldn't have to trust embedding similarity to find a topic it
    already has an id for.

    Auto-routing itself, when no explicit id is given:
      1. Disabled via env var, or the embedding call fails -> fall back
         to whatever thread is already active (or "main" if there
         isn't one yet). Bias, deliberately: an unavailable router
         should never fragment a session it can't confidently route,
         same "store-if-unsure" direction _should_store() already
         takes for the sibling filter above.
      2. No threads registered yet -> this turn starts "main".
      3. Compare against every existing thread's centroid; the best
         match at/above THREAD_SIMILARITY_THRESHOLD wins, and that
         thread's centroid is nudged toward the new vector (simple
         running average weighted by turn_count) so a long-lived
         thread's centroid tracks its topic as it evolves rather than
         freezing at its first message.
      4. No match clears the bar -> a new thread is created, with
         parent_thread_id set to whatever was active before (the same
         lightweight pointer shape as eo/chat_store.py's
         linked_chat_ids) — UNLESS the session is already at
         MAX_THREADS_PER_SESSION, in which case the least-recently-
         updated existing thread is reused (its centroid replaced
         outright, not averaged, since it's now genuinely a different
         topic) rather than growing storage without bound.
    """
    registry = read(_threads_key(session_id), default=[])
    now = time.time()

    if explicit_thread_id:
        entry = next((t for t in registry if t["thread_id"] == explicit_thread_id), None)
        vector = _embed_for_routing(text)
        if entry is None:
            registry.append({
                "thread_id": explicit_thread_id,
                "centroid": vector,
                "parent_thread_id": read(_active_thread_key(session_id), default=None),
                "updated_at": now,
                "turn_count": 1,
            })
        elif vector is not None:
            entry["centroid"] = _ema_centroid(entry.get("centroid"), vector, entry.get("turn_count", 1))
            entry["turn_count"] = entry.get("turn_count", 1) + 1
            entry["updated_at"] = now
        write(_threads_key(session_id), registry, ex=CONVERSATION_TTL)
        write(_active_thread_key(session_id), explicit_thread_id, ex=CONVERSATION_TTL)
        return explicit_thread_id

    if not CONVERSATION_THREAD_ROUTING_ENABLED:
        _note_thread_stat("routing_skipped_disabled")
        return read(_active_thread_key(session_id), default=MAIN_THREAD_ID)

    vector = _embed_for_routing(text)
    if vector is None:
        _note_thread_stat("routing_skipped_error")
        return read(_active_thread_key(session_id), default=MAIN_THREAD_ID)

    if not registry:
        registry = [{
            "thread_id": MAIN_THREAD_ID, "centroid": vector,
            "parent_thread_id": None, "updated_at": now, "turn_count": 1,
        }]
        write(_threads_key(session_id), registry, ex=CONVERSATION_TTL)
        write(_active_thread_key(session_id), MAIN_THREAD_ID, ex=CONVERSATION_TTL)
        _note_thread_stat("created_new_thread")
        return MAIN_THREAD_ID

    best_thread, best_score = None, -1.0
    for entry in registry:
        score = _cosine_similarity(vector, entry.get("centroid"))
        if score > best_score:
            best_thread, best_score = entry, score

    if best_score >= THREAD_SIMILARITY_THRESHOLD:
        best_thread["centroid"] = _ema_centroid(best_thread.get("centroid"), vector, best_thread.get("turn_count", 1))
        best_thread["turn_count"] = best_thread.get("turn_count", 1) + 1
        best_thread["updated_at"] = now
        write(_threads_key(session_id), registry, ex=CONVERSATION_TTL)
        write(_active_thread_key(session_id), best_thread["thread_id"], ex=CONVERSATION_TTL)
        _note_thread_stat("routed_existing_thread")
        return best_thread["thread_id"]

    if len(registry) >= MAX_THREADS_PER_SESSION:
        stalest = min(registry, key=lambda t: t.get("updated_at", 0))
        stalest["centroid"] = vector
        stalest["turn_count"] = 1
        stalest["updated_at"] = now
        stalest["parent_thread_id"] = read(_active_thread_key(session_id), default=None)
        write(_threads_key(session_id), registry, ex=CONVERSATION_TTL)
        write(_active_thread_key(session_id), stalest["thread_id"], ex=CONVERSATION_TTL)
        _note_thread_stat("reused_stale_thread")
        return stalest["thread_id"]

    new_thread_id = f"topic_{int(now * 1000)}"
    registry.append({
        "thread_id": new_thread_id, "centroid": vector,
        "parent_thread_id": read(_active_thread_key(session_id), default=None),
        "updated_at": now, "turn_count": 1,
    })
    write(_threads_key(session_id), registry, ex=CONVERSATION_TTL)
    write(_active_thread_key(session_id), new_thread_id, ex=CONVERSATION_TTL)
    _note_thread_stat("created_new_thread")
    return new_thread_id


def _ema_centroid(old_centroid, new_vector, turn_count: int):
    """Running average of a thread's centroid, weighted by how many
    turns have already contributed to it — a thread with 20 turns
    behind it shouldn't have its topic yanked by one new message the
    way a brand-new 1-turn thread should. Falls back to the new vector
    outright if there's no usable old centroid to blend with."""
    if not old_centroid or len(old_centroid) != len(new_vector):
        return new_vector
    weight_old = turn_count / (turn_count + 1)
    weight_new = 1 / (turn_count + 1)
    return [weight_old * o + weight_new * n for o, n in zip(old_centroid, new_vector)]


def list_threads(session_id: str) -> list:
    """Returns this session's thread registry as-is (thread_id,
    centroid, parent_thread_id, updated_at, turn_count per entry) —
    exposed for a future debugging endpoint or UI thread-picker, not
    called anywhere in this patch itself."""
    return read(_threads_key(session_id), default=[])


def _workspace_facts_text(session_id: str, owner_id: str = None) -> str:
    """NEW — Part 0 §0.3. session_id and chat_id are the same string
    everywhere in this system (api/server.py's own comment), so a
    session's workspace is just "whichever workspace this chat_id is a
    member of" — eo/chat_workspace.py's workspace_for_chat(). A session
    with no workspace (most ad-hoc chats) simply gets "", same
    no-history-yet convention every other lookup in this module already
    uses, so this is always safe to prepend unconditionally.

    owner_id: FIXED — workspace_for_chat() is now owner_id-scoped, same
    migration as chat_store.py's get_chat()/chat_exists(). Without an
    owner_id we have no ownership context to check, so — same
    fail-quiet convention as the chat_store.py linked-context lookup
    right above this function's call sites — skip the lookup and
    return "" rather than erroring."""
    if not session_id or not owner_id:
        return ""
    ws = chat_workspace.workspace_for_chat(session_id, owner_id)   # FIXED — now passes owner_id
    if not ws:
        return ""
    return workspace_facts.format_facts_for_prompt(ws["id"])


def _user_profile_text(turns: list, owner_id: str = None) -> str:
    """NEW — Patch B3. Silent per-account personalization, structural
    sibling of _workspace_facts_text() right above: workspace_facts is
    scoped to a workspace_id, this is scoped to an owner_id, both feed
    into the same prepend below.

    Topic text is the most recent user turn already sitting in
    `turns` — every existing caller of run_task()/preview_task()
    (api/task_runner.py) calls conversation_memory.append_turn(...,
    "user", task_text) BEFORE dispatch ever reaches this module's
    get_full_context()/get_light_context(), so the current message is
    already the latest "user"-role entry by the time either function
    runs. That means format_profile_for_prompt()'s topic-relevance
    filtering (see that function's own docstring) works out of the box
    here with no new parameter threaded through the many call sites in
    agents/*.py and eo/loop_v4.py/sga.py — same reasoning
    _workspace_facts_text() already leans on for owner_id (fail-quiet
    to "" rather than requiring every caller to be touched)."""
    if not owner_id:
        return ""
    topic_text = next((t["text"] for t in reversed(turns) if t.get("role") == "user"), "")
    return user_profile.format_profile_for_prompt(owner_id, topic_text)


# ---------------------------------------------------------------------
# "Worth storing?" pre-filter — perf audit follow-up (#3). See the
# module docstring above for the overall shape; details below.
# ---------------------------------------------------------------------

# Exact-match (after stripping whitespace/punctuation/case) bare
# acknowledgements and filler. Not exhaustive by design — same
# deterministic-keyword-check-not-a-parser posture as eo/sga.py's
# _requests_verification()/VERIFICATION_REQUEST_PATTERNS, one layer up
# in this same pipeline. Applies to either role: a user's "thanks!" and
# an assistant's bare "You're welcome!" are equally uninformative to a
# later follow-up, though in practice this tier trips almost entirely
# on user turns since generated replies are rarely this short.
_ACK_ONLY_PATTERNS = [
    r"ok(?:ay)?", r"k", r"kk", r"sure", r"yep", r"yup", r"yeah", r"ya",
    r"no", r"nope", r"nah", r"cool", r"nice", r"great", r"perfect",
    r"awesome", r"got it", r"gotcha", r"sounds good", r"good", r"fine",
    r"alright", r"all right", r"thanks?(?: you)?(?: so much)?", r"thx", r"ty",
    r"np", r"lol", r"haha", r"hehe", r"\+1",
    r"\U0001F44D", r"\U0001F44C", r"\U0001F64F", r"\U0001F60A", r"\U0001F600",
]
_ACK_ONLY_RE = re.compile(
    r"^\s*(?:" + "|".join(_ACK_ONLY_PATTERNS) + r")[\s!.,?]*$", re.IGNORECASE,
)

# At/above this length a turn is presumed to carry enough of its own
# content to be worth keeping without any classification at all.
# Deliberately short — most genuine filler is well under this — so this
# tier stays "confidently long enough," not a real length judgment call.
_DEFINITELY_STORE_MIN_CHARS = 15

_STATS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days — matches eo/sga.py's _SGA_STATS_TTL_SECONDS
_STORE_STATS_KEYS = {
    "stored_definite": "conv_memory_stats:stored:definite",
    "stored_ambiguous_llm": "conv_memory_stats:stored:ambiguous_llm",
    "stored_ambiguous_fallback": "conv_memory_stats:stored:ambiguous_fallback",
    "skipped_ack": "conv_memory_stats:skipped:ack",
    "skipped_ambiguous_llm": "conv_memory_stats:skipped:ambiguous_llm",
}


def _note_store_stat(reason: str) -> None:
    """Best-effort; never raises — a missed increment here must never
    block a turn from being (or not being) stored. Same non-fatal
    posture as eo/sga.py's _note_resolved()/_note_escalated()."""
    try:
        incr(_STORE_STATS_KEYS[reason], ex=_STATS_TTL_SECONDS)
    except Exception as exc:
        print(f"  [Conversation Memory] _note_store_stat({reason!r}) failed (non-fatal): {exc}")


def get_conversation_memory_store_stats() -> dict:
    """Returns counts for each path _should_store() can resolve
    through, plus rollups: {..._STORE_STATS_KEYS keys..., "total_stored",
    "total_skipped", "skip_rate"}. skip_rate is None (not 0.0) with no
    data yet, same "don't confuse cold with zero" convention
    eo/sga.py's get_sga_stats()/eo/chat_page_cache.get_cache_stats()
    already use.

    Pull this before touching _ACK_ONLY_PATTERNS or
    _DEFINITELY_STORE_MIN_CHARS — a low ambiguous-tier volume
    (stored_ambiguous_llm + skipped_ambiguous_llm relative to the
    total) means the two free deterministic tiers are already doing
    nearly all the work, so there's little left to gain by tuning the
    LLM escalation itself."""
    counts = {reason: (read(key, default=0) or 0) for reason, key in _STORE_STATS_KEYS.items()}
    total_stored = (counts["stored_definite"] + counts["stored_ambiguous_llm"]
                     + counts["stored_ambiguous_fallback"])
    total_skipped = counts["skipped_ack"] + counts["skipped_ambiguous_llm"]
    total = total_stored + total_skipped
    return {
        **counts,
        "total_stored": total_stored,
        "total_skipped": total_skipped,
        "skip_rate": (total_skipped / total) if total else None,
    }


_AMBIGUOUS_CLASSIFIER_SYSTEM_PROMPT = (
    "You decide whether a single chat message is worth keeping in a "
    "conversation's short-term memory, so a later follow-up message can "
    "refer back to it. Reply with exactly one word: STORE if the message "
    "carries any information, preference, question, or content a later "
    "turn might need to refer back to. SKIP only if it is pure filler — "
    "a bare acknowledgement, greeting, or reaction with nothing else in "
    "it. If you are at all unsure, answer STORE."
)


def _classify_ambiguous_turn(text: str, session_id: str = None) -> tuple:
    """Returns (store: bool, used_llm: bool). store is True (keep)
    unless the cheap SGA-tier model confidently answers SKIP. used_llm
    is False whenever this fell back without a real classification —
    the filter disabled, an import/provider error, or an unparseable
    answer — kept separate from the verdict so
    get_conversation_memory_store_stats() can tell "the model said
    store" apart from "we never got an answer and defaulted," which
    matters when judging whether this tier is worth its cost at all.

    Deliberately reuses eo/sga.py's own SGA_CHAINS/generate_text rather
    than routing through eo/inspector.py's Inspector — this is exactly
    the kind of fast, low-stakes yes/no Layer 0 already exists for.
    Imported lazily (not at module level) because eo/sga.py itself does
    `from eo import conversation_memory` — a top-level import here
    would be circular. Same lazy-import posture append_turn() already
    uses below for agents.note_taker.

    Bias, deliberately: false negatives (dropping a turn that mattered)
    are the dangerous direction; a false positive (storing a bit of
    filler) just costs a little context space. So every uncertain path
    here — disabled, erroring, timing out, an unparseable answer —
    resolves to STORE, never SKIP."""
    if not CONVERSATION_MEMORY_AMBIGUOUS_LLM_ENABLED:
        return True, False
    try:
        from eo.sga import SGA_CHAINS
        from utils.llm_client import generate_text
        answer = generate_text(
            system_prompt=_AMBIGUOUS_CLASSIFIER_SYSTEM_PROMPT,
            user_content=text,
            chain=SGA_CHAINS["sga_1"],
            agent_name="ConversationMemoryFilter",
            session_id=session_id,
        )
        verdict = (answer or "").strip().upper()
        if verdict.startswith("SKIP"):
            return False, True
        if verdict.startswith("STORE"):
            return True, True
        return True, False  # unparseable — bias toward store, not a real verdict
    except Exception as exc:
        print(f"  [Conversation Memory] ambiguous-turn classification skipped (defaulting to store): {exc}")
        return True, False


def _should_store(text: str, session_id: str = None) -> bool:
    """The pre-filter itself. Two deterministic tiers first (no LLM
    cost, no latency): an exact filler match skips, a length floor
    stores. Only text that clears neither escalates to
    _classify_ambiguous_turn() above — see that function's docstring
    for the store-if-unsure bias carried through every fallback path."""
    if not CONVERSATION_MEMORY_FILTER_ENABLED:
        return True
    stripped = text.strip()
    if _ACK_ONLY_RE.match(stripped):
        _note_store_stat("skipped_ack")
        return False
    if len(stripped) >= _DEFINITELY_STORE_MIN_CHARS:
        _note_store_stat("stored_definite")
        return True
    store, used_llm = _classify_ambiguous_turn(stripped, session_id=session_id)
    _note_store_stat(
        ("stored_ambiguous_llm" if store else "skipped_ambiguous_llm") if used_llm
        else "stored_ambiguous_fallback"
    )
    return store


def append_turn(session_id: str, role: str, text: str, owner_id: str = None,
                 thread_id: str = None) -> None:
    """Appends one turn ({"role": "user"|"assistant", "text": ...}) to
    this session's transcript. No-op if session_id is falsy — same
    fail-quiet convention relay/emitter.py already uses for a missing
    session_id, so every existing call site that doesn't have one yet
    stays a harmless no-op instead of erroring.

    NEW — perf audit follow-up (#3): also a no-op (turn neither stored
    nor mined by the note-taker) if _should_store() judges this turn
    pure filler — see that function and the module docstring above.

    thread_id — NEW, perf audit follow-up (#3b), topic-branched
    threads: optional. A caller that already knows the topic (e.g. a
    Notebooks-backed chat with its own topic_id) can pass it straight
    through and skip auto-routing entirely — see _route_to_thread()'s
    own docstring for exactly how an explicit id is handled.

    Every existing call site in this codebase (api/task_runner.py,
    eo/loop_v4.py, agents/note_taker.py, etc.) calls this without
    thread_id at all, which is intentional and unchanged behavior for
    them: a "user" turn with no explicit thread_id auto-routes via
    embedding similarity against the session's existing threads
    (falling back to "main" whenever routing can't confidently place
    it — see _route_to_thread()); an "assistant" turn never re-routes
    on its own, it simply lands in whatever thread the user turn it's
    replying to already resolved to, via the active-thread pointer
    _route_to_thread() maintains. So a session that never drifts topic
    behaves exactly as it did before this patch — same single "main"
    thread, same legacy storage key (see _key()'s own docstring)."""
    if not session_id or not text:
        return
    if not _should_store(text, session_id=session_id):
        return

    if role == "user":
        resolved_thread_id = _route_to_thread(session_id, text, explicit_thread_id=thread_id)
    elif thread_id:
        # An assistant turn with an explicit thread_id (rare — mainly
        # useful for tests/backfills) still registers/refreshes that
        # thread rather than silently ignoring the hint.
        resolved_thread_id = _route_to_thread(session_id, text, explicit_thread_id=thread_id)
    else:
        resolved_thread_id = read(_active_thread_key(session_id), default=MAIN_THREAD_ID)

    key = _key(session_id, resolved_thread_id)
    turns = read(key, default=[])
    turns.append({"role": role, "text": text})
    if len(turns) > MAX_STORED_TURNS:
        dropped = turns[:len(turns) - MAX_STORED_TURNS]
        # NEW — Patch B9 (Tier B): these turns are about to fall out of
        # Tier A's storage window for good. Fold them into the rolling
        # summary instead of just discarding them — fire-and-forget, so
        # the summarizer LLM call never adds latency to this turn.
        # owner_id — NEW, Patch B10: threaded through so the fold's
        # durable-fact routing step can resolve session_id -> workspace.
        rolling_summary.fold_turns_async(session_id, dropped, owner_id=owner_id)
        turns = turns[-MAX_STORED_TURNS:]
    write(key, turns, ex=CONVERSATION_TTL)
    if role == "assistant":
        try:
            from agents.note_taker import note_from_latest_turn_async
            user_text = next((t["text"] for t in reversed(turns[:-1]) if t["role"] == "user"), "")
            note_from_latest_turn_async(session_id, owner_id, user_text, text)   
        except Exception as exc:
            print(f"  [Conversation Memory] note-taker dispatch skipped: {exc}")


def get_full_context(session_id: str, owner_id: str = None, max_turns: int = FULL_CONTEXT_TURNS,
                      thread_id: str = None) -> str:
    """... (unchanged from previous fix) ...

    thread_id — NEW, perf audit follow-up (#3b): optional. Defaults to
    whatever thread append_turn() most recently routed this session's
    active turn to (via the active-thread pointer), so an existing
    caller that doesn't pass this reads exactly the narrow, on-topic
    window that thread-branching exists to produce, with no call-site
    changes required. Pass an explicit thread_id to read a *different*
    (e.g. earlier, now-inactive) thread's context instead — see
    list_threads() for discovering what thread ids exist for a
    session."""
    if not session_id:
        return ""
    resolved_thread_id = thread_id or read(_active_thread_key(session_id), default=MAIN_THREAD_ID)
    turns = read(_key(session_id, resolved_thread_id), default=[])
    recent = turns[-max_turns:]
    lines = []
    for t in recent:
        text = t["text"]
        if len(text) > FULL_TURN_CHAR_LIMIT:
            text = text[:FULL_TURN_CHAR_LIMIT] + "..."
        lines.append(f"[{t['role']}]: {text}")
    own = "\n\n".join(lines)

    linked = chat_store.get_linked_context_text(session_id, owner_id, max_turns_per_chat=6,
                                                 char_limit=400) if owner_id else ""
    body = linked + "\n\n--- current conversation ---\n\n" + own if (linked and own) else (linked or own)

    # NEW — Patch B9 (Tier B): older material that's already fallen out
    # of `own` above (see append_turn()'s trim) isn't just gone — surface
    # it here, ahead of the full-detail recent turns, so a generation
    # agent still has a (narrower) sense of what happened earlier in a
    # long-running session.
    summary = rolling_summary.get_summary(session_id)
    if summary:
        body = f"--- earlier in this conversation (summarized) ---\n\n{summary}\n\n{body}" if body else summary

    facts = _workspace_facts_text(session_id, owner_id)   # FIXED — now passes owner_id
    profile = _user_profile_text(turns, owner_id)   # NEW — Patch B3
    memory_blocks = "\n\n".join(block for block in (facts, profile) if block)
    if memory_blocks and body:
        return memory_blocks + "\n\n" + body
    return memory_blocks or body


def get_light_context(session_id: str, owner_id: str = None, max_turns: int = LIGHT_CONTEXT_TURNS,
                       thread_id: str = None) -> str:
    """... (unchanged from previous fix) ...

    thread_id — NEW, perf audit follow-up (#3b): same default/override
    behavior as get_full_context()'s own thread_id parameter — see
    that function's docstring."""
    if not session_id:
        return ""
    resolved_thread_id = thread_id or read(_active_thread_key(session_id), default=MAIN_THREAD_ID)
    turns = read(_key(session_id, resolved_thread_id), default=[])
    recent = turns[-max_turns:]
    lines = []
    for t in recent:
        text = t["text"].strip().replace("\n", " ")
        if len(text) > LIGHT_TURN_CHAR_LIMIT:
            text = text[:LIGHT_TURN_CHAR_LIMIT] + "..."
        lines.append(f"- {t['role']}: {text}")
    own = "\n".join(lines)

    linked = chat_store.get_linked_context_text(session_id, owner_id, max_turns_per_chat=3,
                                                 char_limit=150) if owner_id else ""
    body = linked + "\n--- current conversation ---\n" + own if (linked and own) else (linked or own)

    facts = _workspace_facts_text(session_id, owner_id)   # FIXED — now passes owner_id
    profile = _user_profile_text(turns, owner_id)   # NEW — Patch B3
    memory_blocks = "\n".join(block for block in (facts, profile) if block)
    if memory_blocks and body:
        return memory_blocks + "\n" + body
    return memory_blocks or body