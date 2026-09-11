"""
eo/sga.py — Starter General Agents (SGA), Layer 0 of the MiniMe v6
architecture. Runs BEFORE the Inspector on every task. Three agents in an
escalating relay — most tasks resolve at Stage 1 alone.

Stage 1: SGA #1 alone. Escalates if it predicts (or takes) >~1s.
Stage 2: SGA #1 + #2 in parallel. Escalates if >~2s combined.
Stage 3: SGA #1 + #2 + #3 in parallel. Aborts entirely if >~3s combined —
         full hand-off to eo/inspector.py, no partial SGA answer used.

Which SGA is "Stage 1" rotates round-robin across calls so token usage
stays balanced across all three dedicated accounts (see _rotate_start()).

Migration Part 26 §gap fix: SGA's own escalation judgment (via
SYSTEM_PROMPT below) only ever weighs "can I answer this content
confidently," never "did the user ask for something structurally beyond
what a single SGA call can provide" — e.g. "...and don't stop until a
reviewer approves it" is easy CONTENT (SGA can write the code fine) but
impossible for SGA to actually FULFILL alone (there's no reviewer in a
single-shot answer). A cheap, fast model asked to self-police a
qualitative instruction like that is not reliable enough to gate on by
itself, so _requests_verification() below is a deterministic keyword
check that short-circuits straight to escalation -- zero SGA calls spent
-- whenever the task text itself asks for review, approval, verification,
or iteration by another agent. SYSTEM_PROMPT is also updated as a
secondary defense for phrasings the keyword check doesn't catch.
"""
import concurrent.futures
import itertools
import json
import logging
import os
import re
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo import conversation_memory  # NEW — Part 23 fix, see _call_one() below
from memory.bus import incr, read  # NEW — perf audit follow-up (#1): resolve/escalate stats
from relay.emitter import emit_event
from utils.llm_client import generate_text

logger = logging.getLogger(__name__)

# llama-3.3-70b-versatile decommissioned by Groq; migrated to the two
# models Groq's decommission notice suggested in its place.
#
# Root-cause audit fix, Fix 4 (2026-08-27): every step below got an
# explicit "max_tokens": 1024 (always preferred over any model-based
# default -- see llm_client._max_tokens_for()'s docstring), instead of
# falling through to that function's flat DEFAULT_MAX_TOKENS/
# REASONING_MODEL_MAX_TOKENS (which reserved as much as double
# qwen/qwen3.6-27b's entire tpm budget before a single prompt token was
# counted). SGA's own module docstring and SYSTEM_PROMPT above are
# explicit that this tier exists ONLY for what it can answer "confidently
# and quickly" -- anything needing real research, multi-step planning, or
# cross-file code work is instructed to ESCALATE rather than attempt a
# partial answer -- so unlike Inspector's fixed-shape classification JSON,
# this budget has to cover a genuine (if deliberately scoped-small)
# free-text "answer" field, not just a short verdict. 1024 is sized for
# that: comfortably more than the ESCALATE sentinel or a short direct
# answer ever needs, while still well under every chain model's real tpm
# ceiling. A task whose honest answer needs more room than this is, by
# SGA's own design, exactly the kind of task that should ESCALATE to
# eo/inspector.py's routing instead of being force-fit into a truncated
# SGA response.
SGA_CHAINS = {
    "sga_1": [
        {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "SGA_GROQ_1", "max_tokens": 1024},
        {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "SGA_GROQ_1", "max_tokens": 1024},
    ],
    "sga_2": [
        {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "SGA_GROQ_2", "max_tokens": 1024},
        {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "SGA_GROQ_2", "max_tokens": 1024},
    ],
    "sga_3": [
        {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "SGA_GROQ_3", "max_tokens": 1024},
        {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "SGA_GROQ_3", "max_tokens": 1024},
    ],
}

SYSTEM_PROMPT = """You are a fast, general-purpose first responder. Try to answer the task \
directly and quickly. If you cannot answer confidently and quickly — because it needs real \
research, multi-step planning, or writing/editing code across files — set "answer" to \
exactly the single word ESCALATE and nothing else. Do not attempt a partial or guessed \
answer in that case.
Also ESCALATE — regardless of how easy the underlying content is — if the task explicitly \
asks for something you cannot actually provide alone as a single one-shot answer: a second \
agent's review or approval, iteration until some external party signs off, running/testing \
the result, or any other multi-step verification process. Answering the content without \
honoring that part of the request would be silently dropping a real requirement, which is \
worse than escalating a task you could otherwise answer easily.

Respond with ONLY a single JSON object, no markdown fences, no commentary before or after \
it, in exactly this shape:
{"answer": <string — your answer, or exactly "ESCALATE">, \
"memorable": <true or false>, \
"category": <one of "preference", "decision", "idea", "context", or null>}

"memorable" is true only if the task or your answer establishes a durable fact worth \
recalling later for this workspace — a stated preference, a decision that was made, an \
idea worth keeping, or standing context (e.g. "use TypeScript for this", "the login bug is \
in session refresh", "target this for students"). It is false for ordinary lookups, \
one-off questions, and anything you're escalating. When "memorable" is false, set \
"category" to null. Never set "memorable" to true when "answer" is ESCALATE."""

# Deterministic pre-check, tried before any SGA call is made at all.
# Catches the common ways someone asks for a multi-agent review/iteration
# loop explicitly — these phrasings mean SGA structurally cannot fulfill
# the request alone, no matter how easy the underlying content is, so
# there's no reason to spend an LLM call finding that out. Not
# exhaustive by design (natural language has too many ways to say this);
# SYSTEM_PROMPT's own instruction above is the fallback for phrasings
# this list doesn't catch.
VERIFICATION_REQUEST_PATTERNS = [
    r"don'?t stop until",
    r"until (?:a |the )?(?:reviewer|review(?:er)?|approval|approved)",
    r"(?:reviewer|panel|another agent) (?:to )?(?:approve|review|verify|sign off)",
    r"requires? (?:a |an )?(?:\w+ )?(?:review|approval|verification|sign[- ]?off)",
    r"iterate until",
    r"keep (?:iterating|improving|going) until",
    r"run(?: it)? (?:through )?(?:tests?|the tests?) until",
    r"get (?:it|this) (?:reviewed|approved|verified)",
    r"peer[- ]review",
]
_VERIFICATION_REQUEST_RE = re.compile("|".join(VERIFICATION_REQUEST_PATTERNS), re.IGNORECASE)


def _requests_verification(task_text: str) -> bool:
    """True if the task text explicitly asks for review/approval/
    verification/iteration by another agent — something a single SGA
    call structurally cannot provide, regardless of how easy the
    underlying content is."""
    return bool(_VERIFICATION_REQUEST_RE.search(task_text or ""))


# Bug fix (Test tab audit, Bug 1): same shape as _requests_verification()
# above, for the same underlying reason — SGA's own escalation judgment
# only ever weighs "can I answer this content confidently," never "did
# this structurally need more than one independently-hired agent."
# RunSimulationPanel.run() in TestTab.jsx always dispatches one of a
# fixed, small set of task strings built from SIMULATION_TYPES[*].taskLead
# (e.g. "Simulate a focus group — an enthusiastic customer, a skeptical
# customer, and a professional critic, each reacting independently —
# to: ..."). Every one of those maps to eo/structure.py's
# STRUCTURE_TEMPLATES["simulate"] domain, which always hires the full
# persona roster (persona_customer, persona_skeptic, critic_reviewer,
# usability_walkthrough, red_team, pricing_sensitivity,
# support_ticket_predictor, competitor_response,
# marketplace_review_batch, simulation_synthesizer) so simulation_
# synthesizer has independent per-persona output to actually synthesize
# — see notebooks.py's get_simulation_results() comment. A plain-text
# SGA answer blends all of that into one paragraph and none of those
# roles ever run, so Friction Reports has nothing to read back. Because
# the frontend only ever sends one of these nine fixed prefixes for
# this tab (never arbitrary free text), matching them literally is as
# reliable as a real structural tag would be, without needing to add
# one — same "closed set, so deterministic is safe" reasoning as
# VERIFICATION_REQUEST_PATTERNS being a fixed list rather than a
# generic NLP classifier.
SIMULATION_DISPATCH_PATTERNS = [
    r"^simulate how real customers would react",
    r"^simulate an experienced, opinionated professional critic'?s published review",
    r"^generate a realistic distribution of marketplace-style reviews",
    r"^simulate a focus group",
    r"^simulate a first-time user'?s usability walkthrough",
    r"^simulate how a real prospective buyer would react to the pricing",
    r"^predict the concrete support tickets and confused-user questions",
    r"^run a red-team pass looking for ways to break, misuse, or exploit",
    r"^predict how a rational competitor would respond",
]
_SIMULATION_DISPATCH_RE = re.compile("|".join(SIMULATION_DISPATCH_PATTERNS), re.IGNORECASE)


def _requests_simulation_domain(task_text: str) -> bool:
    """True if this is one of the Test tab's fixed simulation dispatch
    strings, which always needs the multi-persona STRUCTURE_TEMPLATES
    "simulate" pipeline — a single SGA answer would silently blend
    every persona's reaction into one paragraph and skip the actual
    persona roles entirely, breaking Friction Reports for that run.
    Anchored at the start of the (stripped) text since the frontend
    always puts the taskLead phrase first — this deliberately does NOT
    try to detect the same intent in general free-form chat text, only
    this tab's own known dispatch shape."""
    return bool(_SIMULATION_DISPATCH_RE.match((task_text or "").strip()))

# Tuning defaults — describe when it's worth hedging with another SGA
# agent in parallel. Evaluated only AFTER a stage's calls actually
# settle (a resolved answer, a real per-call failure, or hitting
# _SGA_SAFETY_TIMEOUT below) — never used to cut off a call that's
# still genuinely in flight and might succeed. Not measured yet, see
# Part 1's note on calibrating these against real latency data once
# live.
STAGE_TIMEOUTS = {1: 1.0, 2: 2.0, 3: 3.0}

# Latency audit fix (2026-09-12): attempt()'s stage loop used to run
# _call_one() sequentially and only check STAGE_TIMEOUTS *after* every
# blocking call in a stage had already returned -- so Stage 2/3's
# "parallel" claim above was never true, and none of the 1s/2s/3s
# numbers were ever an enforced ceiling. Worse, _call_one() ->
# generate_text() can sleep-and-retry in place for up to
# _LEDGER_WAIT_CAP_SECONDS (20s, see utils/llm_client.py) per rate-
# limited chain step with nothing here able to cut it off, so a single
# unlucky "hi" landing on a rate-limited key could burn 30+ seconds.
#
# First attempt at a fix used STAGE_TIMEOUTS itself (1.0/2.0/3.0s) as a
# literal wait timeout on the in-flight call. That was wrong and made
# things worse: 1-3s is completely ordinary latency for a real Groq
# round trip, so a 1s hard cutoff abandoned almost every message --
# not just rate-limited ones -- mid-flight and kicked it to Inspector
# before its own SGA call had a chance to finish successfully.
# STAGE_TIMEOUTS was only ever meant to gate a *hedging* decision
# ("should we also start another agent"), never to preempt a call that
# hasn't actually failed or hung yet -- the original sequential code
# only ever checked it AFTER a call had already returned.
#
# _SGA_SAFETY_TIMEOUT is the separate, correct answer to "how do we
# stop a truly stuck/rate-limited call": a hard per-stage wait cap
# sized well above ordinary round-trip latency, so it essentially
# never fires on a healthy call and only kicks in once a call is
# genuinely hung.
_SGA_SAFETY_TIMEOUT = 8.0

# A single shared, module-level executor (rather than one created fresh
# per attempt() call) so concurrent inbound messages don't each spin up
# their own thread pool. This matters specifically because every SGA
# account is shared across every incoming message (see this module's
# docstring) -- under real traffic, a burst of messages landing during
# a rate-limited window would otherwise each abandon their own 3-thread
# pool via shutdown(wait=False) (which does NOT kill running threads,
# only stops accepting new work), and those orphaned threads can each
# stay alive for up to llm_client.py's own ~100s worst-case retry
# budget. A bounded shared pool caps how many of those can pile up at
# once instead of growing unbounded under load. max_workers=32
# comfortably covers three SGA agents times a healthy burst of
# concurrent requests.
_STAGE_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=32, thread_name_prefix="sga-relay"
)


def _run_stage(active: list, task_text: str, session_id: str, futures: dict) -> dict:
    """Submits any not-yet-submitted agents in `active` to the shared
    executor -- agents already submitted by an earlier stage are reused
    via `futures`, not re-invoked, fixing a pre-existing inefficiency
    where escalating to Stage 2/3 re-ran Stage 1's own agent a second
    time -- and waits up to _SGA_SAFETY_TIMEOUT for a usable result.

    Returns as soon as ANY agent in `active` produces a non-ESCALATE
    answer rather than waiting for the rest of the stage to settle.
    (In practice this stage-by-stage design never has more than one
    genuinely still-pending future in a single call here -- every
    earlier stage's agent has already resolved or the loop wouldn't
    have advanced past it -- but checking-as-we-go costs nothing and
    keeps this correct if that ever changes.) Calls still pending when
    the safety cap expires are abandoned -- left running in the
    background, see _STAGE_EXECUTOR's docstring above -- and simply
    excluded from the returned dict.

    Returns {agent_key: result} for every call that both completed
    within the cap AND didn't raise -- same shape callers already
    handle."""
    for agent_key in active:
        if agent_key not in futures:
            futures[agent_key] = _STAGE_EXECUTOR.submit(
                _call_one, agent_key, task_text, session_id
            )
    pending = {futures[k]: k for k in active}
    results = {}
    try:
        for fut in concurrent.futures.as_completed(pending, timeout=_SGA_SAFETY_TIMEOUT):
            agent_key = pending[fut]
            try:
                result = fut.result()
            except Exception:
                continue
            results[agent_key] = result
            if "ESCALATE" not in result["answer"].upper():
                break  # good answer in hand -- stop waiting on any stage sibling
    except concurrent.futures.TimeoutError:
        # Safety cap's up -- keep whatever already came back (already
        # collected above) and abandon the rest; do not block further.
        pass
    return results

# Perf audit follow-up (latency discussion, point #1): the "not
# measured yet" note above is exactly what this counter block answers.
# attempt() is the fast path this whole module exists for -- Stage 1
# alone resolving is the cheap case; Stage 2/3 means 2-3 parallel calls
# were already spent before an answer came back; an escalation means
# eo/inspector.py's Inspector (and possibly the Panel) runs next. Which
# of these actually happens, and — for escalations — WHY, is the thing
# to know before touching STAGE_TIMEOUTS or _requests_verification()'s
# keyword list: a deterministic keyword match (zero SGA calls spent) is
# a very different signal from "all three stages genuinely judged this
# beyond them." Same shape as eo/chat_page_cache.py's global hit/miss
# counters and utils/llm_client.py's matching ledger-event-stats added
# alongside this patch: one incr() per outcome, 30-day rolling TTL,
# read back by get_sga_stats() below.
_SGA_STATS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days -- matches
# chat_page_cache.py's STATS_TTL_SECONDS/llm_client.py's
# _LEDGER_EVENT_STATS_TTL_SECONDS.

_SGA_RESOLVED_STAGE_KEYS = {
    1: "sga_stats:resolved:stage1",
    2: "sga_stats:resolved:stage2",
    3: "sga_stats:resolved:stage3",
}
_SGA_ESCALATED_REASON_KEYS = {
    # Deterministic keyword short-circuits (_requests_verification()/
    # _requests_simulation_domain()) -- zero SGA calls spent, decided
    # before Stage 1 even starts.
    "verification_keyword": "sga_stats:escalated:verification_keyword",
    "simulation_domain": "sga_stats:escalated:simulation_domain",
    # Every stage actually ran (or the 1/2/3s predicted-latency budget
    # ran out) and none produced a non-ESCALATE answer.
    "no_confident_answer": "sga_stats:escalated:no_confident_answer",
}


def _note_resolved(stage: int) -> None:
    """Best-effort; never raises -- a missed increment here must never
    take down the actual SGA answer it's counting. Same non-fatal
    posture as utils/llm_client.py's _record_ledger_event()."""
    try:
        incr(_SGA_RESOLVED_STAGE_KEYS[stage], ex=_SGA_STATS_TTL_SECONDS)
    except Exception as exc:
        print(f"  [sga] _note_resolved failed (non-fatal): {exc}")


def _note_escalated(reason: str) -> None:
    """Same non-fatal posture as _note_resolved() above."""
    try:
        incr(_SGA_ESCALATED_REASON_KEYS[reason], ex=_SGA_STATS_TTL_SECONDS)
    except Exception as exc:
        print(f"  [sga] _note_escalated failed (non-fatal): {exc}")


def get_sga_stats() -> dict:
    """Returns {"resolved_by_stage": {1: int, 2: int, 3: int},
    "escalated_by_reason": {"verification_keyword": int,
    "simulation_domain": int, "no_confident_answer": int},
    "total_resolved": int, "total_escalated": int, "resolve_rate":
    float | None}. resolve_rate is None (not 0.0) with no data yet,
    same "don't confuse cold with zero" convention
    eo/chat_page_cache.get_cache_stats()'s hit_rate already uses.

    Pull this before touching STAGE_TIMEOUTS's guessed cutoffs or
    _requests_verification()'s keyword list (see this module's own
    "not measured yet" note above STAGE_TIMEOUTS) -- a low
    resolve_rate with most escalations landing in
    "verification_keyword" points at an overly broad keyword match,
    not at Stage 1-3 themselves being too conservative."""
    resolved = {stage: (read(key, default=0) or 0)
                for stage, key in _SGA_RESOLVED_STAGE_KEYS.items()}
    escalated = {reason: (read(key, default=0) or 0)
                 for reason, key in _SGA_ESCALATED_REASON_KEYS.items()}
    total_resolved = sum(resolved.values())
    total_escalated = sum(escalated.values())
    total = total_resolved + total_escalated
    return {
        "resolved_by_stage": resolved,
        "escalated_by_reason": escalated,
        "total_resolved": total_resolved,
        "total_escalated": total_escalated,
        "resolve_rate": (total_resolved / total) if total else None,
    }


_rotation = itertools.cycle(["sga_1", "sga_2", "sga_3"])

def _rotate_start():
    """Round-robin which SGA leads Stage 1, so the three dedicated
    accounts drain evenly over time rather than SGA #1 absorbing nearly
    all of the layer's volume."""
    first = next(_rotation)
    order = ["sga_1", "sga_2", "sga_3"]
    idx = order.index(first)
    return order[idx:] + order[:idx]

_VALID_CATEGORIES = {"preference", "decision", "idea", "context"}

# Bug fix (Test tab audit, Bug 2): when JSON parsing fails, the raw text
# still sometimes *looks* like a JSON object (model emitted `{...}` that
# didn't quite parse — trailing comma, unescaped quote, etc.) rather than
# genuine prose. Dumping that straight to the user reads as a broken app.
# This strips a wrapper that's clearly JSON-shaped (starts with `{`, ends
# with `}`) so the fallback degrades to *something* readable instead of
# raw braces; anything that isn't brace-wrapped is left untouched.
_STRAY_JSON_WRAPPER = re.compile(r"^\{.*\}$", re.DOTALL)


def _strip_stray_json_wrapper(text: str) -> str:
    text = (text or "").strip()
    if _STRAY_JSON_WRAPPER.match(text):
        # Best-effort: pull an "answer" value out if present, otherwise
        # just drop the wrapper so we're not showing bare braces.
        m = re.search(r'"answer"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.DOTALL)
        if m:
            try:
                return json.loads(f'"{m.group(1)}"')
            except (json.JSONDecodeError, ValueError):
                return m.group(1)
        return ""
    return text


def _parse_structured_response(raw: str) -> dict:
    """Part 5 — SGA now asks each model for a JSON object shaped like
    {"answer", "memorable", "category"} instead of plain text (see
    SYSTEM_PROMPT above). Models don't reliably honor "no markdown
    fences" instructions, and a cheap/fast chain like this one won't
    always emit valid JSON at all, so this parses defensively and
    fails open to the old plain-text behavior rather than ever raising
    — same discipline as _invalidate_facts_cache() and
    fact_summarizer.extract_fact(): a malformed response degrades to
    "not memorable," it never blocks the actual SGA answer.

    Fail-open shape on any parse problem: {"answer": <raw text as-is>,
    "memorable": False, "category": None}. This also transparently
    covers a bare "ESCALATE" reply with no JSON wrapper at all, since
    that raw text becomes the "answer" and the ESCALATE check downstream
    still works unchanged.
    """
    text = (raw or "").strip()
    # Strip a ```json ... ``` or ``` ... ``` fence if the model added one
    # despite being told not to.
    fenced = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()

    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        logger.warning(
            "SGA _parse_structured_response: malformed JSON from model, "
            "falling back to raw text (%s): %r",
            exc, text[:200],
        )
        return {
            "answer": _strip_stray_json_wrapper(raw),
            "memorable": False,
            "category": None,
        }

    if not isinstance(parsed, dict) or "answer" not in parsed:
        return {
            "answer": _strip_stray_json_wrapper(raw),
            "memorable": False,
            "category": None,
        }

    answer = parsed.get("answer")
    if not isinstance(answer, str):
        return {
            "answer": _strip_stray_json_wrapper(raw),
            "memorable": False,
            "category": None,
        }

    memorable = bool(parsed.get("memorable")) and "ESCALATE" not in answer.upper()
    category = parsed.get("category")
    if category not in _VALID_CATEGORIES:
        category = None
    if not memorable:
        category = None

    return {"answer": answer.strip(), "memorable": memorable, "category": category}


def _call_one(agent_key: str, task_text: str, session_id: str = None) -> dict:
    # Migration Part 26 §5 fix: this took session_id as a parameter but
    # never passed it into generate_text() below -- every SGA call's
    # usage/events went out unscoped (session_id=None) even mid-session,
    # same class of gap §5 found in six other agents at the eo/executor.py
    # boundary, just isolated here to the three Starter General Agents.
    #
    # Part 5 fix: returns {"answer", "memorable", "category"} now,
    # not a bare string — see _parse_structured_response() above.
    #
    # Part 23 fix: SGA is the FIRST thing every task hits (before the
    # Inspector, before responder.py) and, until now, it never looked at
    # this session's conversation history at all -- a follow-up like "who
    # is older between the two of us?" had no way to resolve here, so SGA
    # would either guess wrong or (more often) legitimately ESCALATE for
    # lack of context, only to land on tier-0 responder, which -- before
    # its own Part 23 fix -- had the exact same blind spot. Same pattern
    # as prompt_writer_lean.py/responder.py: prepend get_full_context()
    # ahead of the task text sent to the model; the task_text argument
    # itself, and anything the caller does with it afterward, is
    # untouched.
    conv_context = conversation_memory.get_full_context(session_id)   # NEW — Part 23 fix
    user_content = task_text
    if conv_context:
        user_content = f"Recent conversation:\n{conv_context}\n\nTask: {task_text}"   # NEW — Part 23 fix

    raw = generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,   # CHANGED — Part 23 fix, was task_text
        chain=SGA_CHAINS[agent_key],
        agent_name=f"SGA ({agent_key})",
        session_id=session_id,
    )
    return _parse_structured_response(raw)   # CHANGED — Part 5, was a bare .strip() string

def attempt(task_text: str, session_id: str = None) -> dict:
    """
    Returns {"resolved": True, "answer": str, "memorable": bool,
    "category": str|None} on a successful SGA answer, or
    {"resolved": False} if all three stages escalate/time out, OR the
    task explicitly asked for review/approval/verification/iteration that
    SGA cannot itself provide (see _requests_verification() above) — the
    caller (eo/loop_v4.py) then falls through to eo/inspector.classify()
    exactly as it does today for every task.

    Part 5: "memorable"/"category" come straight from whichever SGA
    stage resolved the task (see SYSTEM_PROMPT / _parse_structured_response()
    above) — always present and always safe to read on a resolved result,
    since _call_one() fails open to {"memorable": False, "category": None}
    on any parse problem rather than raising or omitting the keys.
    Callers that only care about "answer" (the only key this function
    returned before Part 5) are unaffected.
    """
    skip_reason = None
    skip_stat_reason = None  # NEW — perf audit follow-up (#1)
    if _requests_verification(task_text):
        skip_reason = "task explicitly requires review/approval SGA can't provide alone"
        skip_stat_reason = "verification_keyword"
    elif _requests_simulation_domain(task_text):
        skip_reason = ("Test tab simulation dispatch — requires the multi-persona "
                        "simulate pipeline, not a single blended SGA answer")
        skip_stat_reason = "simulation_domain"

    if skip_reason:
        emit_event("agent_start", session_id, agent="sga_relay",
                   payload={"label": "SGA — attempting direct answer"})
        emit_event("agent_done", session_id, agent="sga_relay",
                   payload={"summary": f"escalated to Inspector — {skip_reason}"})
        _note_escalated(skip_stat_reason)  # NEW — perf audit follow-up (#1)
        return {"resolved": False}

    order = _rotate_start()
    emit_event("agent_start", session_id, agent="sga_relay",
               payload={"label": "SGA — attempting direct answer"})

    started = time.monotonic()
    futures = {}
    active = [order[0]]
    stage = 1
    while stage <= 3:
        deadline = STAGE_TIMEOUTS[stage]
        results = _run_stage(active, task_text, session_id, futures)

        # CHANGED — Part 5: each result is {"answer", "memorable",
        # "category"}, not a bare string. Checked in `active` order
        # (i.e. Stage 1's own agent first) so a tie between agents that
        # both resolved within the safety cap still prefers the same
        # agent the old sequential loop would have returned first.
        for agent_key in active:
            result = results.get(agent_key)
            if result and "ESCALATE" not in result["answer"].upper():
                emit_event("agent_done", session_id, agent="sga_relay",
                           payload={"summary": f"resolved at stage {stage} ({agent_key})"})
                _note_resolved(stage)  # NEW — perf audit follow-up (#1)
                return {
                    "resolved": True,
                    "answer": result["answer"],
                    "memorable": result["memorable"],
                    "category": result["category"],
                }
        # Same escalation decision the original code made: only
        # measured after this stage's calls actually settled (or hit
        # the safety cap), never by cutting a healthy call short.
        elapsed = time.monotonic() - started
        if elapsed > deadline or stage == 3:
            break
        stage += 1
        active = order[:stage]

    emit_event("agent_done", session_id, agent="sga_relay",
               payload={"summary": "escalated to Inspector — no confident SGA answer"})
    _note_escalated("no_confident_answer")  # NEW — perf audit follow-up (#1)
    return {"resolved": False}


if __name__ == "__main__":
    # Quick standalone smoke test — same pattern as responder.py's own
    # __main__ block. Run: python eo/sga.py
    test_task = "What is 2+2?"
    result = attempt(test_task, session_id="sga_smoke_test")
    print(json.dumps(result, indent=2))
    assert "memorable" in result and "category" in result, (
        "Part 5: resolved SGA result should carry memorable/category"
    )

    # Verification-request smoke test — should escalate with zero SGA
    # calls, regardless of how trivial the underlying content is.
    verification_task = ("Write a Python function to reverse a linked list, "
                          "and don't stop until a reviewer explicitly approves it.")
    result2 = attempt(verification_task, session_id="sga_smoke_test_verification")
    print(json.dumps(result2, indent=2))

    # Simulation-dispatch smoke test — should escalate with zero SGA
    # calls, same as the verification-request case above (Bug 1 fix).
    simulation_task = ("Simulate a focus group — an enthusiastic customer, a skeptical "
                        "customer, and a professional critic, each reacting independently — "
                        "to: the new $12/mo Pro tier with unlimited exports.")
    result3 = attempt(simulation_task, session_id="sga_smoke_test_simulation")
    print(json.dumps(result3, indent=2))
    assert result3["resolved"] is False, (
        "Bug 1 fix: Test tab simulation dispatches must escalate past SGA, "
        "never be answered as a single blended paragraph"
    )
    assert result2["resolved"] is False, "verification-request task should have escalated"