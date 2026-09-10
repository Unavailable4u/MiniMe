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
import os
import re
import sys

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


def _key(session_id: str) -> str:
    return f"conversation:{session_id}"


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


def append_turn(session_id: str, role: str, text: str, owner_id: str = None) -> None:
    """Appends one turn ({"role": "user"|"assistant", "text": ...}) to
    this session's transcript. No-op if session_id is falsy — same
    fail-quiet convention relay/emitter.py already uses for a missing
    session_id, so every existing call site that doesn't have one yet
    stays a harmless no-op instead of erroring.

    NEW — perf audit follow-up (#3): also a no-op (turn neither stored
    nor mined by the note-taker) if _should_store() judges this turn
    pure filler — see that function and the module docstring above."""
    if not session_id or not text:
        return
    if not _should_store(text, session_id=session_id):
        return
    turns = read(_key(session_id), default=[])
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
    write(_key(session_id), turns, ex=CONVERSATION_TTL)
    if role == "assistant":
        try:
            from agents.note_taker import note_from_latest_turn_async
            user_text = next((t["text"] for t in reversed(turns[:-1]) if t["role"] == "user"), "")
            note_from_latest_turn_async(session_id, owner_id, user_text, text)   
        except Exception as exc:
            print(f"  [Conversation Memory] note-taker dispatch skipped: {exc}")


def get_full_context(session_id: str, owner_id: str = None, max_turns: int = FULL_CONTEXT_TURNS) -> str:
    """... (unchanged from previous fix) ..."""
    if not session_id:
        return ""
    turns = read(_key(session_id), default=[])
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


def get_light_context(session_id: str, owner_id: str = None, max_turns: int = LIGHT_CONTEXT_TURNS) -> str:
    """... (unchanged from previous fix) ..."""
    if not session_id:
        return ""
    turns = read(_key(session_id), default=[])
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