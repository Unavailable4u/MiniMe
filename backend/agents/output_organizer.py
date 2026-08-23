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

Patch 8.5: CHAIN (below, renamed FALLBACK_CHAIN) used to be the ONLY
chain both call sites below ever tried -- its hardcoded CEREBRAS_API_KEY_1
third step was one of six agents in Patch 8.1's audit quietly sharing
that single unmonitored, un-fallback-able account (idea_planner.py,
deploy_config_writer.py, dataset_analyst.py, responder.py,
prompt_writer_lean.py, reviewer_fixer_lean.py were the other five) -- a
cooldown on that one key took out every one of their Cerebras fallback
steps at once. Same fix as agents/hardware_speccer.py /
agents/architecture_diagrammer.py: both organize_final_answer() and
organize_final_answer_stream() below now build a live, quota-ranked,
multi-provider chain via eo/dynamic_chain.py's build_fallback_chain()
instead, falling back to FALLBACK_CHAIN only if that comes back empty.

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
import contextlib
import json
import os
import sys


sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils.llm_client import generate_text, stream_completion
from eo.result_render import render_agent_result
from eo.tracing import get_tracer, TRACING_ENABLED

# FALLBACK_CHAIN: last-resort static chain, used ONLY if
# eo/dynamic_chain.py's build_fallback_chain() comes back empty (every
# registered account excluded/cooling down at once -- should be very
# rare). Same reasoning-role chain shape as agents/deploy_config_writer.py
# / agents/prompt_writer.py -- a planning/reasoning call, not a tight,
# latency-sensitive one, so a 3-step chain (vs. those two modules' 2)
# is worth the extra resilience: this call sits at the very end of an
# already-completed multi-agent run, so a account exhausted deep enough
# to need a third hop is far more likely after every other role in the
# run already made its own calls first.
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "GROQ_API_KEY"},
    # OR-3c (reliability_overhaul_plan.md): Cerebras retired to a paid
    # tier (see OR-2's .env.example note) -- was CEREBRAS_API_KEY_1,
    # "openrouter/free" now (not a pinned model slug -- see
    # utils/llm_client.py's OPENROUTER_BASE_URL comment). Note this one
    # is NOT a rarely-hit last resort like the other OR-3c files: no key
    # in eo/registry.py's AGENT_CAPABILITIES is tagged with the
    # "output_organizer" role, so build_fallback_chain("output_organizer")
    # below always returns empty and FALLBACK_CHAIN is the ONLY chain
    # this file ever actually uses -- this step carried real, regular
    # Cerebras traffic, not occasional overflow. Also: this file's own
    # stream_completion() call site is covered by the streaming guard
    # (reliability_overhaul_plan.md Priority 3), confirmed done, so this
    # migration doesn't reintroduce the empty-output/reasoning bug in the
    # streaming path either.
    {"provider": "openrouter", "model": "openrouter/free", "key_env": "OPENROUTER_API_KEY_1"},
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


@contextlib.contextmanager
def _open_organizer_span(session_id: str, user_content: str):
    """
    CO5 patch 4 -- one span labeled "output_organizer" per synthesis
    call, nested under D1 patch 3a's session trace (eo/executor.py's
    _open_session_trace()) so this call shows up as its own labeled
    node in Langfuse instead of an anonymous generation. The inner LLM
    call itself is already covered by D1 patch 2's wrapping inside
    utils/llm_client.py's generate_text()/stream_completion() -- this
    span exists purely to label the CALL SITE, the same "step span
    wraps the generation(s) inside it" shape eo/executor.py's
    _open_role_span() establishes for every other step in the graph.

    Reattaches to the SAME trace 3a opened for this session_id, even
    though that `with` block has already closed by the time this runs
    -- organize_final_answer()/organize_final_answer_stream() are
    called from api/task_runner.py AFTER run_with_looping() returns,
    not from inside execute_graph() itself. Uses the identical
    deterministic tracer.create_trace_id(seed=session_id) 3a uses for
    exactly that reason (see _open_session_trace()'s own docstring),
    so no new lookup table is needed and a paused/resumed run's
    synthesis still lands on the same trace as its role spans.

    Same no-op-when-tracing-off / never-swallow-the-real-exception
    contract as _open_role_span() -- see that function's docstring.
    Returns the span object on success (so the caller can attach
    output once the answer text is known) or None when tracing is
    off, session_id is missing, or the span failed to open --
    callers must guard on that before touching the returned value.
    """
    if not TRACING_ENABLED or not session_id:
        yield None
        return

    stack = contextlib.ExitStack()
    try:
        tracer = get_tracer()
        trace_id = tracer.create_trace_id(seed=session_id)
        span = stack.enter_context(tracer.start_as_current_observation(
            name="output_organizer",
            as_type="span",
            trace_context={"trace_id": trace_id},
            input=user_content,
            metadata={"session_id": session_id},
        ))
    except Exception as trace_exc:
        stack.close()
        print(f"  [output_organizer] Langfuse span failed to open for "
              f"session_id={session_id!r} (non-fatal): {trace_exc}")
        yield None
        return

    with stack:
        yield span


def organize_final_answer(role_outputs: dict, user_request: str, final_role: str = None,
                           session_id: str = None) -> dict:
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
    session_id: NEW -- CO5 patch 4. Optional purely so the 0/1-role
        short-circuit paths below (and any direct/test call, e.g. this
        module's own __main__ block) don't have to pass one. When given,
        used only to open a "output_organizer" tracing span (see
        _open_organizer_span() below) -- never touches merge behavior.

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

    # Patch 8.5: deferred import -- see eo/dynamic_chain.py's module
    # docstring for why this can't be a module-level import (eo.registry
    # imports agent modules at load time; eo.dynamic_chain imports
    # eo.registry at ITS module level). Quota-ranked, cooldown-aware,
    # spread across providers -- replaces FALLBACK_CHAIN's single shared
    # Cerebras key with no fallback.
    from eo.dynamic_chain import build_fallback_chain
    chain = build_fallback_chain("output_organizer") or FALLBACK_CHAIN

    with _open_organizer_span(session_id, user_content) as _span:
        raw_response = generate_text(
            system_prompt=SYSTEM_PROMPT,
            user_content=user_content,
            chain=chain,
            agent_name="output_organizer",
        )
        result = _parse_organizer_response(raw_response)
        if _span is not None:
            _span.update(output=result["answer"])
    return result


async def organize_final_answer_stream(role_outputs: dict, user_request: str, final_role: str = None,
                                        session_id: str = None):
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

    role_outputs / user_request / final_role / session_id: same meaning
    and same defensive 0/1-role short-circuit as organize_final_answer()
    -- see that function's docstring. session_id (CO5 patch 4) is used
    only to open the same "output_organizer" tracing span, same as the
    non-streaming twin.

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

    # Patch 8.5: deferred import, same reasoning as
    # organize_final_answer()'s call site above.
    from eo.dynamic_chain import build_fallback_chain
    chain = build_fallback_chain("output_organizer") or FALLBACK_CHAIN

    with _open_organizer_span(session_id, user_content) as _span:
        _chunks = [] if _span is not None else None
        async for chunk in stream_completion(
            system_prompt=SYSTEM_PROMPT,
            user_content=user_content,
            chain=chain,
            agent_name="output_organizer",
        ):
            if _chunks is not None:
                _chunks.append(chunk)
            yield chunk
        if _span is not None:
            _span.update(output="".join(_chunks))


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
