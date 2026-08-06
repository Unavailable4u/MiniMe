"""
agents/output_organizer.py — Phase CO, CO1 (Master Guide v2, §5).

Takes the full {role_name: raw_output} tree a finished tier-3 multi-role
run produced and returns ONE clean markdown answer, instead of chat
answers being whichever role happened to run last (final_role). This is
the same "merge multiple independent agents' output into one verdict"
pattern agents/review_aggregator.py / agents/security_aggregator.py
already prove out for structured findings — except this one needs a real
LLM call, because deduplicating and structuring *prose* (not error/
finding objects with clean equality/fuzzy-match keys) is a language task,
not a mechanical merge. See utils/similarity.py's docstring for why that
aggregator pair stays deterministic and this one can't.

Deliberately its own module rather than a generic_worker role: generic_
worker.py's run() takes a single role_tag and reads its context off the
memory bus via input_keys (one earlier stage at a time); this call needs
the WHOLE finished role_outputs tree handed to it directly as a plain
dict, which is a different calling shape entirely — same reasoning
deploy_config_writer.py already documents for staying out of
generic_worker despite also being pure reasoning with no real action of
its own.

CHAIN below is a fixed, hardcoded fallback list (same shape and same
reasoning-role sizing as deploy_config_writer.py's own CHAIN) rather than
generic_worker.py's dynamic quota-ranked _build_fallback_chain(): this is
a single one-off call per finished run, not a parallel fan-out pool, so
the simpler static chain is the right fit — it still walks multiple
providers on a transient failure via generate_text()'s own chain-walk,
same free-tier rotation everything else in this codebase relies on, it's
just not quota-ranked per-call the way a pool selection is.

NOT wired into api/task_runner.py yet — that's CO1's second piece. This
piece only adds the standalone function; nothing calls it in production
until that wiring lands.
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text
from eo.result_render import render_agent_result

# Same reasoning-role chain shape as agents/deploy_config_writer.py /
# agents/prompt_writer.py -- a planning/reasoning call, not a tight,
# latency-sensitive one, so a 3-step chain (vs. those two modules' 2)
# is worth the extra resilience: this call sits at the very end of an
# already-completed multi-agent run, so a account exhausted deep enough
# to need a third hop is far more likely after every other role in the
# run already made its own calls first.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_1"},
    # Env-audit fix: the account is named MISTRAL_API_KEY (bare, "account 1"
    # per .env.example's own comment), not MISTRAL_API_KEY_1 -- that name
    # was never provisioned, so this third fallback step always resolved
    # to None and silently failed. Harmless today only because this
    # module isn't wired into api/task_runner.py yet (see docstring); the
    # bug was caught here before that wiring landed.
    {"provider": "mistral", "model": "mistral-large-latest", "key_env": "MISTRAL_API_KEY"},#gitleaks:allow
]

SYSTEM_PROMPT = """You are the final-answer organizer for an autonomous multi-agent
pipeline. Several independent agent roles just finished working on one user
request, each producing its own piece of the answer. Your only job is to merge
their outputs into ONE clean, well-organized markdown answer for the user --
you are not adding new information or opinions of your own, only reorganizing
and deduplicating what's already there.

Rules:
- Merge overlapping content across roles into one coherent answer.
- Drop duplicate restatements -- when two roles cover the same ground (a
  different agent re-adding "one more thing" that was already said), keep it
  once, in whichever phrasing is clearest.
- Keep every genuinely unique fact from every role. Never drop real content
  just to shorten the answer.
- Use headings/lists only where they actually help readability -- don't impose
  structure by default on an answer that reads fine as plain prose.
- Preserve any fenced code block or Mermaid diagram byte-for-byte, exactly as
  its originating role wrote it. You are reorganizing prose around code, never
  rewriting code or diagram syntax yourself.
- Respond with ONLY the final organized markdown answer -- no preamble like
  "Here is the organized answer", no meta-commentary about what you merged.
"""


def organize_final_answer(role_outputs: dict, user_request: str, final_role: str = None) -> str:
    """
    role_outputs: {role_name: raw_output} -- the full per-role result tree
        a finished tier-3 run produced (api/task_runner.py's `results`).
        Each raw_output can be any shape eo/result_render.py already
        knows how to render (str, {"text": ...}, {"issues": ...},
        {module: code}, ...).
    user_request: the original task text, so the organizer knows what
        question it's actually answering rather than merging in a vacuum.
    final_role: optional -- purely informational, not currently used to
        change merge behavior. Kept as a parameter (rather than omitted)
        so callers don't need special-casing when they already have it
        on hand from the same `looped` result task_runner.py reads it
        from, and so a future revision can weight the final role's own
        phrasing more heavily without a signature change.

    Returns the organized markdown answer as a plain string.

    Defensive guard: if role_outputs has 0 or 1 entries, there's nothing
    to merge -- return that one role's rendered text directly (or an
    empty string for 0) rather than spending an LLM call reorganizing a
    single voice. The real call site (api/task_runner.py, CO1's second
    piece) already gates on `len(results) > 1` before calling this at
    all; this guard just keeps the function itself safe to call directly,
    the same defensive spirit agents/review_aggregator.py's own
    aggregate_reviews() applies to an empty/single-review list.
    """
    if not role_outputs:
        return ""
    if len(role_outputs) == 1:
        only_role, only_output = next(iter(role_outputs.items()))
        return render_agent_result(only_output, role=only_role)

    sections = []
    for role, raw_output in role_outputs.items():
        rendered = render_agent_result(raw_output, role=role)
        label = f"{role} (final role)" if role == final_role else role
        sections.append(f"--- {label} ---\n{rendered}")

    user_content = (
        f"Original request: {user_request}\n\n"
        "Each of the following sections is one agent role's own output for this "
        "request. Merge them into one organized answer per the system instructions.\n\n"
        + "\n\n".join(sections)
    )

    organized = generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        chain=CHAIN,
        agent_name="output_organizer",
    )
    return organized.strip()


if __name__ == "__main__":
    # Real call, same convention as agents/deploy_config_writer.py's own
    # __main__ block -- not mocked, exercises the actual CHAIN.
    example = {
        "researcher": {"text": "The library uses semantic versioning. Breaking changes only land in major releases."},
        "fact_checker": {"text": "Confirmed: semantic versioning, major.minor.patch. No breaking changes outside major bumps."},
        "writer": {"text": "In short, you can safely pin to a minor version range without fear of breaking changes."},
    }
    print(organize_final_answer(
        example,
        user_request="Is it safe to auto-update this library on minor version bumps?",
        final_role="writer",
    ))
