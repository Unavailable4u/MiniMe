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
import os
import re
import sys
import time
import json
import itertools
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from relay.emitter import emit_event
from eo import conversation_memory   # NEW — Part 23 fix, see _call_one() below

SGA_CHAINS = {
    "sga_1": [{"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "SGA_GROQ_1"}],
    "sga_2": [{"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "SGA_GROQ_2"}],
    "sga_3": [{"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "SGA_GROQ_3"}],
}

SYSTEM_PROMPT = """You are a fast, general-purpose first responder. Try to answer the task \
directly and quickly. If you cannot answer confidently and quickly — because it needs real \
research, multi-step planning, or writing/editing code across files — respond with exactly \
the single word ESCALATE and nothing else. Do not attempt a partial or guessed answer in \
that case.
Also ESCALATE — regardless of how easy the underlying content is — if the task explicitly \
asks for something you cannot actually provide alone as a single one-shot answer: a second \
agent's review or approval, iteration until some external party signs off, running/testing \
the result, or any other multi-step verification process. Answering the content without \
honoring that part of the request would be silently dropping a real requirement, which is \
worse than escalating a task you could otherwise answer easily."""

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

# Tuning defaults — not measured yet, see Part 1's note on calibrating
# these against real latency data once live.
STAGE_TIMEOUTS = {1: 1.0, 2: 2.0, 3: 3.0}

_rotation = itertools.cycle(["sga_1", "sga_2", "sga_3"])

def _rotate_start():
    """Round-robin which SGA leads Stage 1, so the three dedicated
    accounts drain evenly over time rather than SGA #1 absorbing nearly
    all of the layer's volume."""
    first = next(_rotation)
    order = ["sga_1", "sga_2", "sga_3"]
    idx = order.index(first)
    return order[idx:] + order[:idx]

def _call_one(agent_key: str, task_text: str, session_id: str = None) -> str:
    # Migration Part 26 §5 fix: this took session_id as a parameter but
    # never passed it into generate_text() below -- every SGA call's
    # usage/events went out unscoped (session_id=None) even mid-session,
    # same class of gap §5 found in six other agents at the eo/executor.py
    # boundary, just isolated here to the three Starter General Agents.
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

    return generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,   # CHANGED — Part 23 fix, was task_text
        chain=SGA_CHAINS[agent_key],
        agent_name=f"SGA ({agent_key})",
        session_id=session_id,
    ).strip()

def attempt(task_text: str, session_id: str = None) -> dict:
    """
    Returns {"resolved": True, "answer": str} on a successful SGA answer,
    or {"resolved": False} if all three stages escalate/time out, OR the
    task explicitly asked for review/approval/verification/iteration that
    SGA cannot itself provide (see _requests_verification() above) — the
    caller (eo/loop_v4.py) then falls through to eo/inspector.classify()
    exactly as it does today for every task.
    """
    if _requests_verification(task_text):
        emit_event("agent_start", session_id, agent="sga_relay",
                   payload={"label": "SGA — attempting direct answer"})
        emit_event("agent_done", session_id, agent="sga_relay",
                   payload={"summary": "escalated to Inspector — task explicitly "
                                        "requires review/approval SGA can't provide alone"})
        return {"resolved": False}

    order = _rotate_start()
    emit_event("agent_start", session_id, agent="sga_relay",
               payload={"label": "SGA — attempting direct answer"})

    started = time.monotonic()
    active = [order[0]]
    stage = 1
    while stage <= 3:
        deadline = STAGE_TIMEOUTS[stage]
        results = {}
        for agent_key in active:
            try:
                results[agent_key] = _call_one(agent_key, task_text, session_id)
            except Exception:
                continue
            if "ESCALATE" not in results[agent_key].upper():
                emit_event("agent_done", session_id, agent="sga_relay",
                           payload={"summary": f"resolved at stage {stage} ({agent_key})"})
                return {"resolved": True, "answer": results[agent_key]}
        elapsed = time.monotonic() - started
        if elapsed > deadline or stage == 3:
            break
        stage += 1
        active = order[:stage]

    emit_event("agent_done", session_id, agent="sga_relay",
               payload={"summary": "escalated to Inspector — no confident SGA answer"})
    return {"resolved": False}


if __name__ == "__main__":
    # Quick standalone smoke test — same pattern as responder.py's own
    # __main__ block. Run: python eo/sga.py
    test_task = "What is 2+2?"
    result = attempt(test_task, session_id="sga_smoke_test")
    print(json.dumps(result, indent=2))

    # Verification-request smoke test — should escalate with zero SGA
    # calls, regardless of how trivial the underlying content is.
    verification_task = ("Write a Python function to reverse a linked list, "
                          "and don't stop until a reviewer explicitly approves it.")
    result2 = attempt(verification_task, session_id="sga_smoke_test_verification")
    print(json.dumps(result2, indent=2))
    assert result2["resolved"] is False, "verification-request task should have escalated"