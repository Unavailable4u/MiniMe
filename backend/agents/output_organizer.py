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

Wired into api/task_runner.py's tier-3 result packaging (CO1's second
piece): called whenever a finished run produced more than one role's
output, fails open to the final_role's own text if the synthesis call
itself errors. See task_runner.py's own comment at that call site for
the exact gating condition.

CHANGED — CO4 patch 2: organize_final_answer() now returns
{"answer": str, "dedup_notes": dict} instead of a bare string. The
model is asked to report, alongside the merged answer, which roles'
content got folded into another role's section rather than kept as its
own — e.g. a role whose "one more thing" restated an earlier role's
point and was dropped as a duplicate. dedup_notes maps
{role_name: short plain-language note} for exactly those roles;
a role that kept fully distinct content simply has no entry. This is
what lets the frontend's AgentStepList answer "why didn't role 4 show
up separately in the final answer" inline on that role's own step,
instead of leaving it a silent gap between the per-role step list and
the merged answer.
"""
import json
import os
import sys


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text, stream_completion
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

Output format -- respond with ONLY these two parts, nothing else (no preamble
like "Here is the organized answer", no meta-commentary):

1. The final organized markdown answer.
2. On its own line, the exact marker ---DEDUP_NOTES--- followed by a single
   line of JSON: an object mapping each role name whose content you folded
   into another role's section (rather than keeping as its own distinct
   contribution) to a short, plain-language note on what happened to it, e.g.
   {"reviewer": "its point about input validation was already covered by
   implementer's section, so it was merged in rather than repeated"}. Only
   include roles that lost a distinct voice this way -- a role whose content
   stayed genuinely unique does not need an entry. If no role's content was
   folded away, output an empty object: {}.
"""

# NEW — CO4 patch 2: matches the exact marker SYSTEM_PROMPT instructs the
# model to emit between the markdown answer and the trailing dedup-notes
# JSON line.
DEDUP_NOTES_MARKER = "---DEDUP_NOTES---"


def organize_final_answer(role_outputs: dict, user_request: str, final_role: str = None) -> dict:
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

    Returns {"answer": str, "dedup_notes": dict} -- CHANGED, CO4 patch 2
    (previously a bare string). dedup_notes maps {role_name: note} for
    exactly the roles whose content got folded into another role's
    section rather than kept distinct; a role with no entry either kept
    its own visible section or wasn't merged away.

    Defensive guard: if role_outputs has 0 or 1 entries, there's nothing
    to merge -- return that one role's rendered text directly (or an
    empty string for 0) rather than spending an LLM call reorganizing a
    single voice, with an empty dedup_notes either way (there is no
    second role for anything to have been folded into). The real call
    site (api/task_runner.py, CO1's second piece) already gates on
    `len(results) > 1` before calling this at all; this guard just keeps
    the function itself safe to call directly, the same defensive spirit
    agents/review_aggregator.py's own aggregate_reviews() applies to an
    empty/single-review list.
    """
    if not role_outputs:
        return {"answer": "", "dedup_notes": {}}
    if len(role_outputs) == 1:
        only_role, only_output = next(iter(role_outputs.items()))
        return {"answer": render_agent_result(only_output, role=only_role), "dedup_notes": {}}

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

    raw_response = generate_text(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        chain=CHAIN,
        agent_name="output_organizer",
    )
    return _parse_organizer_response(raw_response)


async def organize_final_answer_stream(role_outputs: dict, user_request: str, final_role: str = None):
    """
    CO5 (Master Guide v2, §5) -- async-generator twin of
    organize_final_answer() above. Same synthesis prompt, same CHAIN,
    same merge/dedupe rules; the only difference is that this yields
    text chunks as the model generates them instead of returning once
    at the end. Built second, wrapping CO1's already-correct,
    already-tested non-streaming logic, per the guide's own build-order
    note -- get organize_final_answer() right first, then stream it,
    so a synthesis-quality bug and a streaming-plumbing bug are never
    debugged at the same time.

    role_outputs / user_request / final_role: same meaning and same
    defensive 0/1-role short-circuit as organize_final_answer() -- see
    that function's docstring.

    Yields: str chunks of the merged markdown answer only. Deliberately
    does NOT yield dedup_notes -- that's structured JSON, not prose the
    user is reading live, so per CO5's scope it goes out as one final
    non-streamed payload once the caller sees the stream close (the SSE
    endpoint layer, not this function, is responsible for attaching it
    after the last chunk).

    Depends on utils.llm_client.stream_completion() (CO5 patch 2),
    which wraps the same OpenAI-SDK-shaped providers generate_text()
    uses but relays chunks back through an asyncio.Queue fed from a
    worker thread, instead of returning one blocking round-trip. See
    that function's docstring for the streaming contract (Cloudflare
    steps skipped, raises rather than falls back once a chunk has
    already gone out).
    """
    if not role_outputs:
        return
    if len(role_outputs) == 1:
        only_role, only_output = next(iter(role_outputs.items()))
        yield render_agent_result(only_output, role=only_role)
        return

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

    async for chunk in stream_completion(
        system_prompt=SYSTEM_PROMPT,
        user_content=user_content,
        chain=CHAIN,
        agent_name="output_organizer",
    ):
        yield chunk


def _parse_organizer_response(raw_response: str) -> dict:
    """
    Splits SYSTEM_PROMPT's two-part response (markdown answer, then
    DEDUP_NOTES_MARKER, then one line of JSON) into the {"answer",
    "dedup_notes"} shape organize_final_answer() returns.

    Deliberately tolerant of the model dropping or malforming the second
    part -- the merged answer is the part that actually matters to the
    user, so a missing/invalid dedup-notes line degrades to an empty
    dict rather than losing the answer itself (same fail-open spirit as
    task_runner.py's own try/except around this whole call).
    """
    if DEDUP_NOTES_MARKER not in raw_response:
        return {"answer": raw_response.strip(), "dedup_notes": {}}

    answer_part, _, notes_part = raw_response.partition(DEDUP_NOTES_MARKER)
    dedup_notes = {}
    try:
        parsed = json.loads(notes_part.strip())
        if isinstance(parsed, dict):
            dedup_notes = parsed
    except (ValueError, TypeError):
        pass
    return {"answer": answer_part.strip(), "dedup_notes": dedup_notes}


if __name__ == "__main__":
    # Real call, same convention as agents/deploy_config_writer.py's own
    # __main__ block -- not mocked, exercises the actual CHAIN.
    example = {
        "researcher": {"text": "The library uses semantic versioning. Breaking changes only land in major releases."},
        "fact_checker": {"text": "Confirmed: semantic versioning, major.minor.patch. No breaking changes outside major bumps."},
        "writer": {"text": "In short, you can safely pin to a minor version range without fear of breaking changes."},
    }
    result = organize_final_answer(
        example,
        user_request="Is it safe to auto-update this library on minor version bumps?",
        final_role="writer",
    )
    print(result["answer"])
    print(DEDUP_NOTES_MARKER)
    print(result["dedup_notes"])
