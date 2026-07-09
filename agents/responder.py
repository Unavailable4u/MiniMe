"""
agents/responder.py — Responder, Tier-0 execution agent (Part 2.3 of the
v5 Master Blueprint).

Not a classifier — the Inspector (eo/inspector.py) already decided this
task is tier 0. This agent just answers it directly, once, no plan/spec/
code/review pipeline involved.

Provider substitution note (matches the one already made in
eo/inspector.py and documented in utils/llm_client.py's own docstring):
Gemini is not used anywhere in this codebase. Part 2.3 says "same key as
the Inspector" — here that means the same dedicated Groq account
(EO_INSPECTOR_GROQ_KEY_1), not a Gemini key. The reasoning the blueprint
gave for sharing still applies: a tier-0 task is cheap and high-volume,
and this account has zero production-agent traffic on it, so doubling up
a classify-then-answer pair here is the right place to spend that
account's quota.

Per Part 5.1's engagement table, tier 0 touches NO Upstash Redis (DB1-3)
and NO E2B — this function takes a string in, returns a string out, and
writes nothing to memory. (eo/loop_v4.py's own DB5 routing-log writes,
from Part 7, are a separate concern and happen regardless of tier.)

Part 23 fix: this agent was never wired into the conversation-memory
bridge at all — eo/executor.py's TASK_TEXT_ENTRYPOINTS dispatch case
calls responder.run() with only task_text/key_override, no session_id,
and this module's own run() signature had nowhere to put one even if it
had been passed. That's why a tier-0 follow-up ("who is older between
the two of us?") was answered with zero memory of the turn before it,
even though eo/conversation_memory.py has existed since Part 23 —
prompt_writer_lean.py and generic_worker.py were wired to it, this one
was missed. Same fix as those two: accept session_id, prepend
get_full_context() ahead of the task text sent to the model. Only the
text sent to the LLM changes — the returned answer is unaffected.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from eo import conversation_memory   # NEW — Part 23 fix

CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "EO_INSPECTOR_GROQ_KEY_1"},
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "EO_PANEL_GITHUB_PAT"},
]

SYSTEM_PROMPT = """You are a fast, direct assistant answering a single trivial \
question or request inside a larger build system. You are not the build \
system itself — you are only invoked when a task is simple enough that no \
code needs to be written, planned, or reviewed. Answer directly and \
concisely. If the task actually requires writing or editing code across \
one or more files, say so plainly rather than guessing at a small answer, \
since that means you were routed here in error.

Format your answer in Markdown: use fenced code blocks with a language tag \
for any code, use tables for tabular data, use headers/bullet lists to \
structure longer answers, and use bold/italic sparingly for emphasis."""


def run(task_text: str = None, key_override=None, session_id: str = None, path: str = None) -> str:
    """
    Answers `task_text` directly. Unlike the other agents in this
    codebase, this one takes its input as an argument rather than reading
    it from memory.bus — tier 0 deliberately never touches Upstash (Part
    5.1), so there is no shared memory state for it to read.

    `task_text` defaults to None only so this still matches the registry's
    no-arg-callable shape (eo/registry.py) when called incidentally with
    no argument; a real tier-0 run must always pass the task text.

    Migration Part 5 §2.3 addition — key_override, if given, is the
    Panel's specific account choice for this hire (a "researcher" or
    "writer" role, per ROLE_TO_AGENT, both of which resolve to this
    module). Responder makes exactly ONE call, unlike the parallel-pool
    agents — so a list here has no multi-worker meaning; the first entry
    is used and the rest are ignored.

    key_override: None (default) -> today's exact behavior,
        EO_INSPECTOR_GROQ_KEY_1 as the primary key.
    key_override: a single key_env string -> use that account as the
        primary key instead.
    key_override: a list of key_env strings -> use only the first entry
        as the primary key (no parallel pool to spread the rest across).
    The GitHub fallback step is unaffected either way.

    session_id/path (Part 23 fix): session_id, if given, is used to pull
    this session's recent conversation turns (eo/conversation_memory.py's
    get_full_context()) and prepend them ahead of task_text in the text
    actually sent to the model, so a tier-0 follow-up ("who's older
    between the two of us?") can be answered from what was already said
    in this session instead of in a vacuum. Only the text sent to the LLM
    is changed — the raw task_text argument, and this session's stored
    transcript, are untouched either way. path is forwarded to
    generate_text() purely for usage-log labeling (same as every other
    agent); it has no effect on the answer.
    """
    if not task_text:
        raise ValueError(
            "responder.run() needs task_text — tier 0 has no memory.bus "
            "state to fall back on (Part 5.1)."
        )

    if key_override is None:
        primary_key_env = "EO_INSPECTOR_GROQ_KEY_1"
    elif isinstance(key_override, list):
        primary_key_env = key_override[0]
    else:
        primary_key_env = key_override

    chain = [
        {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": primary_key_env},
        {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "EO_PANEL_GITHUB_PAT"},
    ]

    conv_context = conversation_memory.get_full_context(session_id)   # NEW — Part 23 fix
    user_content = task_text
    if conv_context:
        user_content = f"Recent conversation:\n{conv_context}\n\nTask: {task_text}"   # NEW — Part 23 fix

    answer = generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,   # CHANGED — Part 23 fix, was task_text
        chain=chain,
        agent_name="Responder",
        session_id=session_id,   # NEW — Part 23 fix
        path=path,   # NEW — Part 23 fix
    )
    return answer.strip()


if __name__ == "__main__":
    import sys as _sys
    text = " ".join(_sys.argv[1:]) or "What's the difference between a list and a tuple in Python?"
    print(run(text))