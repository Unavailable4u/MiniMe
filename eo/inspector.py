"""
eo/inspector.py — Part 2.1 of the v5 Master Blueprint: the Inspector EO.
Runs on every incoming task. Classifies it into a path + (optionally) a
directed_task_type, without doing any of the actual work itself.
Provider choice (Gemini is out per the user's own substitution, already
reflected in utils/llm_client.py):
  - Primary:   Groq, `qwen/qwen3-32b`, via EO_INSPECTOR_GROQ_KEY_1 — a key
               from a FRESH, DEDICATED Groq account (different signup
               than production's GROQ_API_KEY). Isolation here is
               account-level, not just key-level: a busy adaptive-path
               cycle hammering the production account's rate limits
               doesn't touch this one at all, which is the actual
               property Part 2.1 wanted from putting the Inspector on
               Gemini in the first place.
  - Fallback 1: same model, EO_INSPECTOR_GROQ_KEY_2 — a second dedicated
               Groq account, only used if KEY_1 is rate-limited. Fine to
               leave unset; generate_text() skips any chain step whose
               key_env isn't set rather than erroring, so this step is a
               harmless no-op until you add a second account.
  - Fallback 2: GitHub Models gpt-4.1-nano, via EO_PANEL_GITHUB_PAT — same
               PAT the EO Panel (Part 2.2) and Responder (Part 2.3) use,
               per Part 2's own "cheap, fast, last resort" framing.
Output schema is exactly Part 3's contract, updated per Migration Part 12
§8.2/§8.4 (tier int -> path string):
    {path, directed_task_type, confidence, suggested_agents, reasoning}
This module classifies HONESTLY — it does not know about, and must never
be made to know about, whatever a caller intends to do with "instant"/
"direct" execution not existing yet. Forcing path "adaptive" regardless
of this output is loop_v4.py's job (Stage 4.2 of the roadmap), not this
module's — keeping the Inspector's own output uncorrupted is what makes
it possible to validate classification quality against real traffic
before it affects anything.
"""
import os
import sys
import json
import re
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from relay.emitter import emit_event
from eo.structure import STRUCTURE_TEMPLATES, build_reference_structure_addition
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
You classify one incoming task into a routing path — you do NOT do the \
task yourself.
Classify this task's "path" as exactly one of:
- "instant": trivial — a question, a one-line factual/explanatory answer, \
no code artifact requested.
- "direct": small build — a small, self-contained script or single-file \
program, buildable in one pass, no multi-module architecture implied.
- "fixed": a DIRECTED task against an EXISTING codebase — one specific \
kind of work, not a fresh build. Must set directed_task_type to exactly \
one of: "debug", "review", "add_tests", "refactor", "security_scan", \
"write_docs", "explain_code".
- "adaptive": a full build or ongoing multi-cycle project — "build and \
keep improving X", multi-module scope, or anything implying an app with \
several interacting parts.
Watch specifically for tasks worded to SOUND trivial but that imply \
multi-file/multi-module scope (e.g. "just make me a todo app with users, \
auth, and persistence" sounds casual but is "adaptive", not "instant"/ \
"direct") — this is the case most likely to be under-routed, so when in \
doubt about scope, prefer the higher path and a lower confidence rather \
than guessing low.
Note: "sga" and "cache" are also valid path values elsewhere in this \
system, but they're resolved BEFORE you ever see a task (Part 2/4's \
short-circuit overrides) — you will never need to and must never output \
either of them yourself; your choice is always one of the four above.

For "suggested_agents", describe the KINDS OF EXPERTISE this task needs — \
not just names of agents you've seen before. Use short, general role \
labels (e.g. "implementer", "researcher", "fact_checker", "diagram_designer") \
that describe what the work requires. If a task needs a kind of expertise \
you don't have a standard label for, invent a clear, reusable one — the \
system maintains a growing library of these and will write a proper brief \
for any role it hasn't seen before. Do not limit yourself to roles you've \
used in past examples.

You will also be given, below the task, an explanation of two more \
fields to decide: "domain" and "execution_order" (Migration Part 10). \
Follow those instructions exactly as given there.

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly \
this shape:
{
  "path": "fixed",
  "directed_task_type": "refactor",
  "confidence": 0.87,
  "suggested_agents": ["implementer", "verifier"],
  "reasoning": "one short sentence",
  "domain": "coding",
  "execution_order": ["implementer", "verifier"]
}
"path" must be exactly one of "instant", "direct", "fixed", "adaptive" — \
never a number, never "sga"/"cache" (see note above). "confidence" must \
be a float 0.0-1.0. "directed_task_type" must be null unless path is \
exactly "fixed", in which case it must be one of the seven strings above \
— never invent a new one. "domain" must be null, or one of the domain \
names given below the task. "execution_order" must be a list containing \
only role names that also appear in "suggested_agents" — never a role \
you didn't already choose."""
def _strip_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()
VALID_PATHS = {"instant", "direct", "fixed", "adaptive"}


def _validate(parsed: dict) -> dict:
    # Migration Part 12 §8.2/§8.4: "tier" (int 0-3) -> "path" (string).
    # Unlike "domain" (light-touch, defaults to None on a bad value),
    # "path" should genuinely never be null, so this stays a hard enum
    # check -- same as the old tier check was.
    path = parsed.get("path")
    if path not in VALID_PATHS:
        raise ValueError(f"Inspector returned invalid path: {path!r}")
    directed = parsed.get("directed_task_type")
    if directed not in VALID_DIRECTED_TASK_TYPES:
        raise ValueError(f"Inspector returned invalid directed_task_type: {directed!r}")
    if path != "fixed" and directed is not None:
        # Same discipline the Panel synthesis rule uses (Part 2.2): don't
        # silently accept an inconsistent combination, and don't guess
        # which field is "right" — surface it.
        raise ValueError(
            f"Inspector set directed_task_type={directed!r} but path={path!r} "
            f"(only valid when path == 'fixed')."
        )
    confidence = parsed.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        raise ValueError(f"Inspector returned invalid confidence: {confidence!r}")
    if not isinstance(parsed.get("suggested_agents"), list):
        raise ValueError("Inspector's suggested_agents must be a list.")

    # Migration Part 10 §3 — two additional fields. Validated loosely
    # and defaulted rather than raised on, since a model omitting these
    # (e.g. an older cached response, or a member that just forgot) is a
    # normal case Part 10 explicitly wants handled by falling through to
    # "the Panel builds an order from scratch," not a hard failure the
    # way a bad tier/confidence is.
    domain = parsed.get("domain")
    if domain is not None and domain not in STRUCTURE_TEMPLATES:
        # Not a recognized domain name -- treat as "none genuinely fits"
        # rather than rejecting the whole classification over it.
        domain = None
    execution_order = parsed.get("execution_order")
    if not isinstance(execution_order, list):
        execution_order = []
    else:
        # Silently drop anything not in suggested_agents -- the prompt
        # asks for this already, but a model can still slip up, and
        # dropping is safer here than raising over an otherwise-good
        # classification.
        execution_order = [r for r in execution_order if r in parsed["suggested_agents"]]

    return {
        "path": path,
        "directed_task_type": directed,
        "confidence": float(confidence),
        "suggested_agents": parsed["suggested_agents"],
        "reasoning": parsed.get("reasoning", ""),
        "domain": domain,
        "execution_order": execution_order,
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
    # Migration Part 10 §3 — same reference-structure text block is used
    # by eo/panel.py's members B and C (via the same helper), so all
    # three panel votes see identical domain/execution_order framing.
    user_content += build_reference_structure_addition(task_text)

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
                path=parsed["path"], payload=parsed)
    emit_event("agent_done", session_id, agent="inspector",
                path=parsed["path"],
                payload={"summary": parsed["reasoning"]})
    return parsed