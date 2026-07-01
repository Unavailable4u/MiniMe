"""
eo/inspector.py — Part 2.1 of the v5 Master Blueprint: the Inspector EO.
Runs on every incoming task. Classifies it into a tier + (optionally) a
directed_task_type, without doing any of the actual work itself.
Provider choice (Gemini is out per the user's own substitution, already
reflected in utils/llm_client.py):
  - Primary:   Groq, `qwen/qwen3-32b`, via EO_INSPECTOR_GROQ_KEY_1 — a key
               from a FRESH, DEDICATED Groq account (different signup
               than production's GROQ_API_KEY). Isolation here is
               account-level, not just key-level: a busy tier-3 cycle
               hammering the production account's rate limits doesn't
               touch this one at all, which is the actual property Part
               2.1 wanted from putting the Inspector on Gemini in the
               first place.
  - Fallback 1: same model, EO_INSPECTOR_GROQ_KEY_2 — a second dedicated
               Groq account, only used if KEY_1 is rate-limited. Fine to
               leave unset; generate_text() skips any chain step whose
               key_env isn't set rather than erroring, so this step is a
               harmless no-op until you add a second account.
  - Fallback 2: GitHub Models gpt-4.1-nano, via EO_PANEL_GITHUB_PAT — same
               PAT the EO Panel (Part 2.2) and Responder (Part 2.3) use,
               per Part 2's own "cheap, fast, last resort" framing.
Output schema is exactly Part 3's contract:
    {tier, directed_task_type, confidence, suggested_agents, reasoning}
This module classifies HONESTLY — it does not know about, and must never
be made to know about, whatever a caller intends to do with tier 0/1
execution not existing yet. Forcing tier 3 regardless of this output is
loop_v4.py's job (Stage 4.2 of the roadmap), not this module's — keeping
the Inspector's own output uncorrupted is what makes it possible to
validate classification quality against real traffic before it affects
anything.
"""
import os
import sys
import json
import re
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from relay.emitter import emit_event
VALID_DIRECTED_TASK_TYPES = {
    "debug", "review", "add_tests", "refactor",
    "security_scan", "write_docs", "explain_code", None,
}
CHAIN = [
    {"provider": "groq", "model": "qwen/qwen3-32b", "key_env": "EO_INSPECTOR_GROQ_KEY_1"},
    {"provider": "groq", "model": "qwen/qwen3-32b", "key_env": "EO_INSPECTOR_GROQ_KEY_2"},
    {"provider": "github", "model": "openai/gpt-4.1-nano", "key_env": "EO_PANEL_GITHUB_PAT"},
]
SYSTEM_PROMPT = """You are the Inspector for a multi-agent build system. \
You classify one incoming task into a routing tier — you do NOT do the \
task yourself.
Tiers:
- 0: trivial — a question, a one-line factual/explanatory answer, no code \
artifact requested.
- 1: small build — a small, self-contained script or single-file program, \
buildable in one pass, no multi-module architecture implied.
- 2: a DIRECTED task against an EXISTING codebase — one specific kind of \
work, not a fresh build. Must set directed_task_type to exactly one of: \
"debug", "review", "add_tests", "refactor", "security_scan", "write_docs", \
"explain_code".
- 3: a full build or ongoing multi-cycle project — "build and keep \
improving X", multi-module scope, or anything implying an app with \
several interacting parts.
Watch specifically for tasks worded to SOUND trivial but that imply \
multi-file/multi-module scope (e.g. "just make me a todo app with users, \
auth, and persistence" sounds casual but is tier 3, not tier 0/1) — this \
is the case most likely to be under-routed, so when in doubt about scope, \
prefer the higher tier and a lower confidence rather than guessing low.
Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly \
this shape:
{
  "tier": 0,
  "directed_task_type": null,
  "confidence": 0.91,
  "suggested_agents": ["responder"],
  "reasoning": "one short sentence"
}
"tier" must be an integer 0-3. "confidence" must be a float 0.0-1.0. \
"directed_task_type" must be null unless tier is exactly 2, in which case \
it must be one of the seven strings above — never invent a new one."""
def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
def _validate(parsed: dict) -> dict:
    tier = parsed.get("tier")
    if tier not in (0, 1, 2, 3):
        raise ValueError(f"Inspector returned invalid tier: {tier!r}")
    directed = parsed.get("directed_task_type")
    if directed not in VALID_DIRECTED_TASK_TYPES:
        raise ValueError(f"Inspector returned invalid directed_task_type: {directed!r}")
    if tier != 2 and directed is not None:
        # Same discipline the Panel synthesis rule uses (Part 2.2): don't
        # silently accept an inconsistent combination, and don't guess
        # which field is "right" — surface it.
        raise ValueError(
            f"Inspector set directed_task_type={directed!r} but tier={tier} "
            f"(only valid when tier == 2)."
        )
    confidence = parsed.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        raise ValueError(f"Inspector returned invalid confidence: {confidence!r}")
    if not isinstance(parsed.get("suggested_agents"), list):
        raise ValueError("Inspector's suggested_agents must be a list.")
    return {
        "tier": tier,
        "directed_task_type": directed,
        "confidence": float(confidence),
        "suggested_agents": parsed["suggested_agents"],
        "reasoning": parsed.get("reasoning", ""),
    }
def classify(task_text: str, context: str = None, session_id: str = None) -> dict:
    """
    Classifies `task_text`. Returns the Part 3 output schema dict.
    Raises RuntimeError if every step in CHAIN is exhausted (matches
    utils.llm_client.generate_text's existing contract), or ValueError if
    a response came back but failed schema validation (a prompt/parsing
    problem — deliberately NOT retried onto the next provider, per
    llm_client's own reasoning: that would just mask a real bug).

    `context`, if given, is appended as extra information (e.g. from
    eo/routing_memory.py's retrieve_similar_outcomes) — Stage 4.7's
    feedback loop. It is presented to the model as evidence about past
    similar tasks, never as an instruction about what to conclude this
    time, so the Inspector keeps classifying honestly per this module's
    own docstring.

    `session_id`, if given, fires relay events (Part 6.3) so a connected
    frontend can watch this classification happen live — Stage 6, step 1
    of the roadmap ("wire the event-emitting wrapper into one agent
    first ... as a proof of concept"). Omitting session_id (the default)
    makes this call byte-for-byte the same as before Stage 6 existed:
    every event call below becomes a no-op per relay/emitter.py's own
    contract, so existing callers (loop_v4.py without a session, all the
    EO tests) are unaffected.
    """
    emit_event("agent_start", session_id, agent="inspector",
                payload={"label": "Inspector — classifying task"})

    user_content = f"Task: {task_text}"
    if context:
        user_content += (
            f"\n\nFor reference, here is how some similar past tasks were "
            f"routed and what happened (this is informational only — use "
            f"your own judgment on the current task):\n{context}"
        )

    try:
        raw = generate_text(
            system_prompt=SYSTEM_PROMPT,
            user_content=user_content,
            chain=CHAIN,
            agent_name="Inspector",
        )
        parsed = _validate(json.loads(_strip_fences(raw)))
    except Exception as exc:
        emit_event("error", session_id, agent="inspector",
                    payload={"message": str(exc), "agent": "inspector"})
        raise

    emit_event("routing_decision", session_id, agent="inspector",
                tier=parsed["tier"], payload=parsed)
    emit_event("agent_done", session_id, agent="inspector",
                tier=parsed["tier"],
                payload={"summary": parsed["reasoning"]})
    return parsed