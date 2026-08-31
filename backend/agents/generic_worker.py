"""
agents/generic_worker.py — v6 migration Part 10. Runs ANY role that
doesn't perform a real action (no file writes, no external API calls),
using that role's stored brief (eo/registry.py's get_role_prompt) and the
memory-bus hand-off contract each execution-order step implies: it reads
whichever earlier stages' outputs it's told to (input_keys), and writes
its own output back under its own role name so a later stage can read it
in turn.

Migration Part 12 §3.4: idea_planner/prompt_writer/test_writer are not in
REAL_ACTION_ROLES (Part 10 §2.1), so they run through this module, which
writes output only to stage_output:{session_id}:{role}. But
code_writers.py (a real-action module, untouched since v5) still reads
its input from the ORIGINAL v5 bus keys (module_specs, current_plan,
etc.) via memory.bus.read(KEYS[...]). Unifying the execution path doesn't
unify the bus convention -- nothing wrote those legacy keys anymore once
prompt_writer moved to generic_worker. LEGACY_BUS_KEY_MAP below bridges
that: for the handful of roles a real-action module still expects a key
from, run() also reads/writes that original key, so code_writers.py etc.
keep working completely unmodified.

Honest caveat (not fully solved by this bridge): stage_output:* keys are
namespaced by session_id; the legacy keys (module_specs, current_plan)
are namespaced by app_slug (memory/bus.py's original design). For a
single task run these usually align in practice, but they're genuinely
two different namespacing dimensions -- a true unification is a bigger
change than this bridge attempts. This map covers coding's specific
early-stage hand-off, which is what's actually needed for coding tasks to
work through the unified pipeline.

Part 23: also prepends this session's full conversation-memory context
(eo/conversation_memory.py's get_full_context()) ahead of the rest of the
context this role sees, so a follow-up like "make it shorter" or "add
three more features" has real prior content to build on instead of being
treated as the first message in the session.

Part 2 §2.6: that prepend is now opt-out, per role, via
`include_conversation_context` (default True — today's exact behavior
for every existing caller). `input_keys` already gave a role an exact,
enforced view of *which prior stage outputs* it can see; the full
conversation transcript was the one piece of context every role got
unconditionally regardless of whether it had any business seeing it. A
narrow persona or single-purpose role can now be marked, in a workflow
template (eo/structure.py's `no_conversation_context_roles`), to skip it.

Patch C3 / C3b (MiniMe-Patch-Series-C-Plan.md): input_keys resolution
no longer just dumps every prior stage's entire output into context.
run() below now ALSO writes each role's finished output to
eo/data_store.py (one section per role, section_id == role name) in
addition to its existing stage_output:{session_id}:{role} bus write —
"also," not "instead of," despite eo/data_store.py's own C1 docstring
framing write_section() as replacing that write: this codebase has a
wide, confirmed set of OTHER direct readers of stage_output:*
(architecture_diagrammer.py, schema_diagrammer.py, handoff_packager.py,
hardware_speccer.py, code_writers.py, eo/executor.py's edit-resume
path, api/routes/tasks.py, api/routes/notebooks.py, and a long list of
tests) that C3's own plan text never mentions touching. Dropping the
bus_write to satisfy that docstring literally would break all of them;
keeping both writes costs one extra in-process call and stays backward
compatible with every consumer that hasn't migrated to
eo/data_store.py yet.

On the READ side, an input_keys entry now resolves one of three ways:
  1. `role:section_id` (C3b's qualifier syntax) — resolved directly via
     data_store.read_section(), full text, no ToC step. A role that
     named a specific section at definition time gets exactly that
     section's content.
  2. a plain `role` name for which data_store has a same-named section
     (i.e. that role already ran through this C3-patched run() at
     least once) — resolved as a table-of-contents pointer
     (list_sections()'s section_id/written_by/version/byte_len), not
     full text. The role's own brief is told, via
     DATA_HANDLER_INSTRUCTION below, that it's looking at a table of
     contents — NOT how to call run_data_command() itself. (BUGFIX:
     this comment used to claim the instruction teaches the role how
     to invoke run_data_command(session_id, "read <section_id>"), which
     was never true of DATA_HANDLER_INSTRUCTION's actual text — see
     that constant below. There is no tool-calling round trip in run()
     to catch such a call anyway, per the "Honest caveat" a few
     paragraphs down; the instruction correctly tells the role to name
     the section it needs in prose instead, not to emit a command
     nothing will execute.)
  3. a plain `role` name with no matching data_store section (a
     REAL_ACTION_ROLES entry from LEGACY_BUS_KEY_MAP that never calls
     this module's run() at all, e.g. academic_search/web_researcher/
     handoff_packager/extraction_table_builder — or simply a role that
     ran before this patch existed) — falls back to today's exact
     behavior: full-text bus_read, dumped inline. These roles never
     get a data_store section (write_section is only ever called from
     inside run(), below), so there is no ToC entry to point at.

Honest caveat this module cannot resolve on its own: DATA_HANDLER_
INSTRUCTION tells a role how to ASK for a section in full by emitting
a run_data_command(...) call, but run() itself makes a single
generate_text() call and returns — there is no tool-calling round trip
here that would intercept that request mid-generation and feed the
result back for a second pass. Wiring an actual agentic loop around
generate_text() (or teaching the executor to notice and re-dispatch on
a data-command line the same way parse_next_tag() already notices a
NEXT: line) is a bigger change than "wire it into generic_worker.py's
context building" and isn't attempted here. Until that exists, a role
that hits the ToC case above and genuinely needs a section's full text
has to be the kind of task where the ToC's own summary (byte_len,
written_by) is enough context to answer well, or where a human/
reviewer step reads the section directly through eo/capabilities.py's
run_data_command() outside this generation call.

Patch C7: get_relevant_skill()/ensure_skill_for_task() are now called
through eo/capabilities.py's re-exports instead of importing
eo/skill_library.py directly — same shared-surface principle B5a
already applied to dispatcher/executor/router. Deferred to inside
run() (not a top-level import) for the exact same circular-import
reason eo.registry/eo.panel are already deferred in this module:
eo.capabilities imports eo.registry, and eo.registry's own
bottom-of-file import of this module would find generic_worker.run
undefined if capabilities were imported before run() exists.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Part 6 §6.4 bridge — see LEGACY_BUS_KEY_MAP below. No circularity risk:
# agents/handoff_packager.py imports only memory.bus/relay.emitter/
# eo.errors/agents.architecture_diagrammer/agents.schema_diagrammer, none
# of which import this module or eo.registry.
from agents.handoff_packager import PLAN_HANDOFF_PACKAGE_KEY
from eo import conversation_memory  # NEW — Part 23
from eo.data_store import list_sections as _ds_list_sections  # NEW — Patch C3
from eo.data_store import read_section as _ds_read_section  # NEW — Patch C3b
from eo.data_store import write_section as _ds_write_section  # NEW — Patch C3
from eo.quota_sentinel import get_quota_snapshot
# AGENT_CAPABILITIES/get_role_prompt deliberately NOT imported here at the
# top of the file -- see the matching import at the BOTTOM of this file
# for why. (eo.quota_sentinel above is fine at the top: its own
# generic_worker-reaching name, PROVIDER_DEFAULT_MODEL, is resolved via a
# deferred import inside _model_for(), not eagerly at eo.quota_sentinel's
# own top level, so it doesn't need anything from this module to already
# exist yet.)
from eo.user_profile import default_format_hint  # NEW — Patch B5
# NOTE: `from eo.capabilities import get_relevant_skill, ensure_skill_for_task`
# (Patch C7 — was `from eo.skill_library import (...)` directly, Part 6
# §E2 task 14) is deliberately NOT imported at the top of the file here,
# same circular-import reason as the eo.registry/eo.panel notes above:
# eo.capabilities imports eo.registry, and eo.registry's own
# bottom-of-file `from agents import (..., generic_worker, ...)` would
# find this module's own `run` attribute undefined if that chain ran
# before run() is defined below. Deferred to inside run() instead.
from memory.bus import KEYS
from memory.bus import read as bus_read
from memory.bus import write as bus_write
from utils.llm_client import DROPPABLE_CONTEXT_MARKER  # NEW — Patch 1b
from utils.llm_client import generate_text

# NOTE: `from eo.panel import _best_match` is deliberately NOT imported at
# module level here. eo.registry.py now imports this module (generic_worker)
# at load time so resolve("generic_worker") works, and eo.panel.py imports
# FROM eo.registry (AGENT_CAPABILITIES, get_role_prompt) -- importing
# eo.panel here too would close a circular loop:
#   eo.registry -> agents.generic_worker -> eo.panel -> eo.registry
# Deferring this one import to inside run() (below) breaks the cycle: by
# the time run() is actually CALLED, both modules have finished loading.

PROVIDER_DEFAULT_MODEL = {
    # llama-3.3-70b-versatile decommissioned by Groq; migrated to
    # openai/gpt-oss-120b, one of the two models Groq's decommission
    # notice suggested in its place (the other, qwen/qwen3.6-27b, is
    # used explicitly in per-agent CHAINs elsewhere -- see e.g.
    # architecture_diagrammer.py/idea_planner.py). This dict only has
    # room for a single value per provider (PROVIDER_DEFAULT_MODEL.get()
    # below feeds exactly one step, not a chain), so gpt-oss-120b was
    # picked as the default the same way utils/llm_client.py's
    # CLASSIFY_INTENT_MODEL was: it's the closer capability match of the
    # two suggested replacements.
    "groq": "openai/gpt-oss-120b",
    # FIX — bug audit: "llama-3.3-70b" was retired from Cerebras'
    # catalog (confirmed via GET /v1/models against a live account:
    # only gpt-oss-120b/gemma-4-31b/zai-glm-4.7 are served now). Every
    # generic_worker role with no natural_roles match (fact_detector,
    # flashcard_writer, quiz_writer, study_guide_writer, workflow_suggester)
    # falls through to the full account pool ranked by quota, so landing
    # on ANY Cerebras account 404'd with the exact "model_not_found"
    # error seen in Notebooks' Facts/Quiz/Flashcards/Study guide/Workflows
    # generation. gpt-oss-120b is already the proven-working Cerebras
    # model used directly by idea_planner.py/dataset_analyst.py/
    # deploy_config_writer.py/reviewer_fixer_lean.py's own CHAINs.
    "cerebras": "gpt-oss-120b",
    # OR-3e: added alongside the cerebras entry above (not replacing it --
    # CEREBRAS_API_KEY_6/7/8 and EO_PANEL_CEREBRAS_KEY are still live
    # Cerebras accounts in the registry, just outside this pool). Needed
    # because code_writers.py's _extra_fallback_chain_steps() ->
    # _chain_step_for() builds a chain step from PROVIDER_DEFAULT_MODEL.get(
    # provider, "") for whatever account eo/panel.py's _best_match() picks
    # as a "different provider" fallback -- now that the implementer/
    # content_writer/mech_* pool has OpenRouter accounts in it (see
    # eo/registry.py), that picker can land on one, and without this entry
    # the resulting step would carry model="" (empty string) straight into
    # generate_text(). "openrouter/free" is OpenRouter's own auto-router,
    # not a pinned slug (see utils/llm_client.py's OR-1 notes), same as
    # every other OR-3 agent's chain.
    "openrouter": "openrouter/free",
    "mistral": "mistral-large-latest",
    # Quota-reality fix, §4 (2026-07-30): "github" entry removed --
    # GitHub Models retired in full today and no CHAIN or tag-driven
    # role anywhere in the repo still resolves to this provider.
    # PATCH 1 (Gemini/Mistral/HF rollout): flash is the sane default for any
    # Gemini-tagged AGENT_CAPABILITIES entry that doesn't specify its own
    # model via a hardcoded CHAIN elsewhere -- cheaper/faster than pro,
    # capable enough for generic_worker's reasoning-role traffic. Entries
    # that specifically want flash-lite or pro get that model explicitly
    # via their own CHAIN step (see structure_architect.py / inspector.py
    # patches), not through this shared default.
    #
    # UPDATED post-Patch-5 field test: gemini-2.5-flash started 404'ing
    # ("This model ... is no longer available to new users") on
    # GEMINI_API_KEY_8, a newly-created key -- confirmed against Google's
    # own current docs that this is a real, general restriction (multiple
    # 2.5-tier models are now blocked for new API keys/projects), not a
    # problem with that one key. gemini-3.6-flash was the model Google's
    # own OpenAI-compatibility docs used as the example at that fix's
    # time, confirmed GA then. Deliberately NOT using the
    # "gemini-flash-latest" rolling alias despite the -latest precedent
    # elsewhere in this file (Mistral) -- Gemini's own "-latest" family
    # aliases get hot-swapped with as little as 2 weeks' notice and have
    # themselves been deprecated outright before, which is worse for an
    # unattended pipeline than a pinned version that fails loudly and
    # gets fixed deliberately. If a pinned version 404s later, re-check
    # https://ai.google.dev/gemini-api/docs/models for the current
    # generation's stable model ID rather than reaching for an alias.
    #
    # Quota-reality fix, §2: gemini-3.6-flash's real free-tier RPD turned
    # out to be 20, not "generous" -- confirmed against Google AI
    # Studio's Rate Limit page, 2026-07-30. This is
    # PROVIDER_DEFAULT_MODEL["gemini"], the model every tag-driven Gemini
    # role runs as (formatter/outliner/brainstormer/writer/editor/
    # researcher/analyst/fact_checker/final_qa/logic_architect) -- 20
    # calls/day/account shared across nine unrelated roles is much
    # thinner than "13 Gemini accounts" sounds like. gemini-3.1-flash-lite's
    # real RPD is 500 -- a dramatically better fit for this shared
    # default's actual job (high-frequency, low-stakes, tag-driven
    # catch-all traffic). Same pinned-version reasoning as above applies
    # to this string too.
    #
    # This is NOT a downgrade for roles where quality matters more than
    # volume -- gemini-3.6-flash is still reachable for those, just
    # explicitly, via a hardcoded CHAIN step on that role's own file
    # (see structure_architect.py / inspector.py / performance_reviewer.py),
    # not through this shared default that nine unrelated roles inherit.
    "gemini": "gemini-3.1-flash-lite",  # was gemini-3.6-flash -- RPD 500 vs 20
    # PATCH 2: HF's router needs an explicit ":provider"/":fastest"/etc.
    # suffix on the model id (see utils/llm_client.py's HF_ROUTER_BASE_URL
    # comment) -- this default uses ":fastest" so it always resolves to
    # *some* live backend rather than pinning one that might get retired.
    # Confirmed live against HF's own docs at wiring time; re-check
    # GET https://router.huggingface.co/v1/models if this 404s later.
    "huggingface": "openai/gpt-oss-120b:fastest",
    # BUGFIX: was missing entirely, so _chain_step_for()'s cloudflare
    # branch had no default to fall back on. Same model string already
    # used for cloudflare steps elsewhere (agents/dependency_mapper.py,
    # utils/llm_client.py's own docstring example).
    "cloudflare": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
}

# FIX (Study tab formatting bug): get_relevant_skill() below embeds
# task_text -- which for every source-grounded Generate target (slide
# deck, podcast, rehearsal, quiz, flashcards, ...) is just "Source
# material:\n\n<the notebook's actual content>", with NO mention of the
# task TYPE anywhere in it (the instruction to write a slide deck vs. a
# podcast vs. a quiz lives only in `brief`, the role's system prompt,
# never in task_text/the embedded string). So skill retrieval is really
# a TOPIC search ("what have I written before about alternators?"), not
# a task-type search ("what have I learned about writing slide decks?")
# -- it will happily return a skill doc written for a totally different
# role (e.g. a rich study-guide/report skill full of tables and Mermaid
# diagrams) purely because it covered the same source material, and
# that guidance then gets injected into the system prompt as "Guidance
# from a similar task type you've handled before," actively steering
# the model away from a strict role brief like slide_planner's toward
# whatever unrelated format that retrieved doc modeled. This is exactly
# what produced a full Markdown report (tables, blockquotes, a Mermaid
# diagram, no "# Deck Title"/"## Slide Title" structure at all) out of
# slide_planner on a blank Presentation-tab prompt: the retrieved skill
# was almost certainly written for this same notebook's Study Guide or
# topic-notes content, not for a slide deck.
#
# Proper fix would be scoping skill retrieval by task type, not just
# content (e.g. embedding "role:{role}\n{task_text}" instead of raw
# task_text) -- a larger change affecting every role's self-improvement
# loop, out of scope for a targeted fix. The safe, surgical fix here:
# skip skill retrieval ENTIRELY for roles whose brief already specifies
# an exact, machine-parsed output structure a downstream frontend
# component depends on byte-for-byte -- for these roles a cross-topic
# "similar task" addition is pure downside (it can only ever compete
# with the required structure, never usefully supplement it, since
# there's no "free-form part" of the answer left for it to improve).
# Each entry's downstream parser: slide_planner -> markdown_text_to_
# artifact()/video_overview_builder.py's per-'## '-heading frames;
# flashcard_writer -> FlashcardFlipper.jsx; quiz_writer -> QuizRunner.jsx
# checkbox parsing; podcast_scriptwriter/rehearsal_scriptwriter ->
# tts_synthesizer.py's "LABEL:" line parser (this is the same subsystem
# the "no speaker-labeled dialogue lines" bug already lived in);
# wireframe_sketcher -> its own docstring's "output ONLY one fenced
# ```html code block" contract.
STRICT_FORMAT_ROLES = frozenset({
    "slide_planner",
    "flashcard_writer",
    "quiz_writer",
    "podcast_scriptwriter",
    "rehearsal_scriptwriter",
    "wireframe_sketcher",
})

# Bug fix (audit follow-up): the two roles above whose output feeds
# tts_synthesizer.py's "LABEL:" line parser specifically -- podcast_
# scriptwriter's "HOST A:"/"HOST B:" lines and rehearsal_scriptwriter's
# "JUDGE:"/"HOST A:"/"ADVOCATE:" lines. Both are STRICT_FORMAT_ROLES
# already, but that set only controls skill-retrieval scoping (see the
# comment above it) -- it never actually checked the model's OWN output
# against the format it demands. Under provider/quota pressure a reroute
# can land on a weaker fallback model that ignores the "HOST A:"-style
# instruction entirely and returns plain prose instead (this is exactly
# what produced the "no speaker-labeled dialogue lines found in
# script_text" ValueError two whole pipeline stages later, in
# tts_synthesizer.synthesize_podcast() -- after the LLM call was already
# spent). Validating here, right after generation, catches that failure
# at its source and gets one retry instead of silently handing a broken
# script forward to a caller that has no way to regenerate it cheaply.
DIALOGUE_LABEL_ROLES = frozenset({
    "podcast_scriptwriter",
    "rehearsal_scriptwriter",
})


def _has_speaker_labeled_dialogue(text: str) -> bool:
    """True if `text` contains at least one line tts_synthesizer.py's own
    parser would recognize as a speaker turn. Deferred import, not a
    top-level one -- same circular-import posture every other cross-
    module import in this file already uses -- and reuses tts_synthesizer's
    actual parser (not a hand-rolled duplicate regex here) so this check
    can never drift out of sync with what the real downstream consumer
    accepts."""
    from agents.tts_synthesizer import _parse_script
    return any(entry[0] == "speech" for entry in _parse_script(text or ""))


# Bug fix (audit follow-up, gap 3): slide_planner is in STRICT_FORMAT_ROLES
# for the exact same reason podcast_scriptwriter/rehearsal_scriptwriter
# are (see that set's own comment above) -- its output feeds
# graph/adapters.py's markdown_text_to_artifact()/video_overview_builder.py
# per-'## '-heading frames just as rigidly as a "LABEL:" line feeds
# tts_synthesizer.py -- but only the two dialogue roles got a generation-
# time check. A reroute onto a weaker model can just as easily ignore
# "one '## Slide Title' per slide" and hand back an ordinary Markdown
# report instead (this is the literal failure this set's own docstring
# above already documents happening on a blank Presentation-tab prompt).
HEADING_FORMAT_ROLES = frozenset({
    "slide_planner",
})


def _has_slide_headings(text: str) -> bool:
    """True if `text` parses into at least one real '## '-headed section
    via the exact same parser markdown_text_to_artifact() (and therefore
    build_video_overview()) will use on it downstream -- not just a
    non-empty string. Deferred import, same reasoning as
    _has_speaker_labeled_dialogue() above."""
    from agents.importer import parse_markdown_text
    if not (text or "").strip():
        return False
    return bool(parse_markdown_text(text, default_title="").get("sections"))


# One shared table instead of one bespoke if-block per role category:
# role -> (checker, what-went-wrong instruction appended to the retry's
# system prompt). Adding a THIRD structurally-validated role in the
# future (flashcard_writer/quiz_writer's checkbox format is the obvious
# next candidate -- see STRICT_FORMAT_ROLES's own comment for their
# downstream parsers) means adding one entry here, not duplicating the
# retry machinery below again.
STRUCTURAL_FORMAT_CHECKS = {
    **{role: (
        _has_speaker_labeled_dialogue,
        "your previous answer did not use the required speaker-label "
        "format at all (e.g. 'HOST A: ...', 'JUDGE: ...') -- it must be "
        "dialogue, not prose or notes. Rewrite it as real spoken lines, "
        "each one starting with a short, consistent, ALL-CAPS label "
        "immediately followed by a colon, exactly as instructed above.",
        "speaker-labeled dialogue lines (e.g. 'HOST A: ...')",
    ) for role in DIALOGUE_LABEL_ROLES},
    **{role: (
        _has_slide_headings,
        "your previous answer did not use the required slide structure "
        "at all -- it must be a '# Deck Title' line followed by one "
        "'## Slide Title' heading per slide, not a plain report or "
        "unheaded prose. Rewrite it with that exact heading structure, "
        "exactly as instructed above.",
        "any '## Slide Title' headings",
    ) for role in HEADING_FORMAT_ROLES},
}


MARKDOWN_INSTRUCTION = (
    "\n\nFormat your answer in Markdown: use fenced code blocks with a "
    "language tag for any code, use tables for tabular data, use headers/"
    "bullet lists to structure longer answers, and use bold/italic "
    "sparingly for emphasis. "
    # BUGFIX (rendering audit, round 2): this suffix is appended after
    # EVERY role's own brief (see run() below), including roles like
    # quiz_writer that already spell out an exact required structure
    # (its brief: "- [ ] <wrong option>" / "- [x] <correct option>"
    # task-list lines). Without this sentence, "use tables for tabular
    # data" reads as an equally-valid alternative and the model would
    # sometimes reach for a table instead of the checkbox format the
    # frontend's QuizRunner.jsx parser actually needs -- silently
    # producing a quiz with zero parseable options. This line makes the
    # precedence explicit instead of leaving two competing instructions
    # for the model to arbitrate itself.
    "These are general defaults for free-form answers only -- if the "
    "role instructions above already specify an exact output structure "
    "(a particular heading pattern, required fields, checkbox-style "
    "options, JSON, etc.), follow that exact structure instead; do not "
    "substitute a table or any other format for it. "
    "If the task calls for a mind map, flowchart, process diagram, or "
    "any other visual/structural diagram, output it as "
    "a fenced code block tagged ```mermaid using real Mermaid syntax "
    "(e.g. flowchart TD, mindmap, or graph LR) — do NOT describe a diagram "
    "as an indented text outline; write actual Mermaid syntax that can be "
    "rendered. "
    # BUGFIX (rendering audit, round 2): this is the single biggest cause
    # of "couldn't render this diagram" fallbacks across Mind Map,
    # Workflows, and any Study content that happens to include a
    # diagram -- Mermaid's flowchart grammar treats an unquoted "(" or
    # ")" inside a `[...]` node label as the start of a *different* node
    # shape, not literal text, so a totally reasonable label like
    # `A[Mechanical Input (prime mover)]` fails to parse. The fix is
    # simply always quoting node text that contains punctuation, which
    # is valid Mermaid syntax the model already knows -- it just wasn't
    # being told this particular gotcha exists.
    "Mermaid gotcha: inside a node's square-bracket label (e.g. "
    "A[label text]), an unquoted parenthesis, colon, or other "
    "punctuation character breaks parsing because Mermaid reads it as "
    "the start of a different node shape. Whenever a node's label "
    "contains punctuation like ( ) : ; or /, wrap the whole label in "
    "double quotes, e.g. A[\"Mechanical Input (prime mover)\"] instead of "
    "A[Mechanical Input (prime mover)]. When in doubt, quoting a plain "
    "label is always safe too."
)


DATA_HANDLER_INSTRUCTION = (
    # NEW — Patch C3: the "companion prompt change" the plan itself
    # flags as necessary. Only appended when at least one input_keys
    # entry actually resolved to a table-of-contents pointer this call
    # (see run() below) — a role with no ToC entries in its context
    # shouldn't be told about a tool it has nothing to use it on.
    "\n\nSome of the context above is a TABLE OF CONTENTS, not full "
    "text — a list of section_id / written_by / version / byte_len "
    "entries describing content that exists but isn't shown in full "
    "here. Do not guess at, invent, or paraphrase a listed section's "
    "content from its section_id or byte_len alone. If a listed "
    "section's full text is genuinely necessary to do this task well "
    "and isn't recoverable from what's shown, say plainly in your "
    "answer which section_id you'd need and why, rather than filling "
    "the gap with an unsupported guess."
)

NEXT_TAG_INSTRUCTION = (
    "\n\nAfter your answer, on its own final line, write exactly one of:\n"
    "NEXT: DONE                 (your part is genuinely complete)\n"
    "NEXT: <role_name>          (this needs another pass from a specific "
    "earlier or later role, name it exactly)\n"
    "Default to NEXT: DONE unless something is genuinely unresolved.\n"
    "IMPORTANT: this NEXT: line must be plain text, NOT inside a markdown "
    "code block or any other formatting, so it can still be parsed "
    "correctly."
)

# Migration Part 12 §3.4 — see module docstring. A role not in this map
# (most non-coding roles) only gets the normal stage_output:* treatment.
#
# Migration Part A fix: idea_planner, prompt_writer, and test_writer were
# moved back to their dedicated real-action modules (they produce
# structured JSON, not free-text reasoning output), so none of the three
# resolve to "generic_worker" anymore.
#
# Part 3 §3.8: extraction_table_builder is a real-action role that writes
# KEYS["extraction_table"], never a stage_output:* entry. Without this
# bridge, any generic_worker role hired after it (consensus_meter,
# contradiction_detector, researcher, writer, editor...) would list it in
# input_keys but find nothing there. academic_search/web_researcher need
# the exact same bridge (see BUGFIX note on the map below) --
# contradiction_prefilter/source_quality_flagger are the only Part 3
# real-action roles that don't, since they already write their own
# stage_output entry directly.
LEGACY_BUS_KEY_MAP = {
    # BUGFIX (bug audit): academic_search/web_researcher are
    # REAL_ACTION_ROLES modules dispatched directly by eo/executor.py, so
    # -- same as handoff_packager below -- they never get a
    # stage_output:{session_id}:{role} entry the way a generic_worker
    # role's own output automatically does. The old comment here claimed
    # "academic_search's output isn't read by name downstream," but
    # researcher/writer/fact_checker/editor DO list "academic_search" (or
    # "web_researcher") in their input_keys for the "research" domain
    # (eo/structure.py) expecting to see what was actually found. Without
    # this bridge that lookup always silently resolved to None, so those
    # roles wrote a confident-sounding response with zero visibility into
    # whether academic_search/web_researcher found real papers, found
    # nothing, or hit API errors -- indistinguishable from never having
    # been hired at all.
    "academic_search": KEYS["academic_search_report"],
    "web_researcher": KEYS["web_researcher_report"],
    "extraction_table_builder": KEYS["extraction_table"],
    # Part 6 §6.4 — handoff_packager is a REAL_ACTION_ROLES module
    # (dispatched directly by eo/executor.py, not through this file), so
    # it never gets a stage_output:{session_id}:handoff_packager entry
    # the way a generic_worker role's own output automatically does.
    # Without this bridge, content_calendar_builder listing
    # "handoff_packager" in its input_keys would ALWAYS find nothing —
    # even when handoff_packager genuinely ran earlier in the same
    # execution graph — and would silently always take the "no handoff
    # exists" relative-sequencing fallback instead of the real one.
    # PLAN_HANDOFF_PACKAGE_KEY is app_slug-namespaced (memory/bus.py's
    # _namespaced()), scoped by the set_app_slug() call
    # handoff_packager.py itself makes right before writing it — this
    # bridge only finds real data when content_calendar_builder runs in
    # the SAME session/task context afterward (the ContextVar-scoped
    # app_slug is still active), which is exactly the "hired in the same
    # plan" case this bridge exists for.
    "handoff_packager": PLAN_HANDOFF_PACKAGE_KEY,
}

# BUGFIX (bug audit, patch 4) — the result-count field to check per real-
# action role bridged above, so a genuinely-empty report gets flagged
# instead of silently blending into the rest of the context. Keyed by
# the SAME name a role lists in input_keys (i.e. LEGACY_BUS_KEY_MAP's
# keys), not the bus key itself.
_RESULT_LIST_FIELD = {
    "academic_search": "papers",
    "web_researcher": "sources",
}


def _zero_results_notice(role_key: str, prior) -> str | None:
    """None for anything that isn't a tracked real-action role, or that
    role genuinely found results. Otherwise an explicit instruction --
    not just a data point -- telling the model not to paper over an
    empty search with its own general knowledge presented as findings."""
    field = _RESULT_LIST_FIELD.get(role_key)
    if field is None or not isinstance(prior, dict):
        return None
    if len(prior.get(field) or []) > 0:
        return None
    return (
        f"IMPORTANT: '{role_key}' found ZERO real results (see \"{field}\": [] "
        f"above) -- likely a source API failure or rate limit, not an "
        f"absence of literature on the topic. Do not write a literature "
        f"review, cite paper titles/authors, or otherwise present "
        f"specific sources as if '{role_key}' had found them. State "
        f"plainly that the search returned no results and, if useful, "
        f"answer from general knowledge WITHOUT inventing citations."
    )


def _cloudflare_token_env_for(account_id_env: str) -> str:
    """Base slots: CLOUDFLARE_ACCOUNT_ID_N -> CLOUDFLARE_API_KEY_N.
    Reserve slots: CF_SCANNER_RESERVE_N_ACCOUNT_ID -> CF_SCANNER_RESERVE_N_API_TOKEN.
    Same two-family naming pattern (and same reasoning for not being a
    single blind substitution) as agents/security_scanner.py's own
    _token_env_for() -- mirrored here rather than imported, since this
    module is loaded very early (see the circular-import note at the
    top of this file) and importing agents/security_scanner here would
    risk reintroducing that same cycle.

    BUGFIX: this used to be a single `agent_key.replace("ACCOUNT_ID",
    "API_TOKEN")` call, which turns "CLOUDFLARE_ACCOUNT_ID_4" into
    "CLOUDFLARE_API_TOKEN_4" -- a plausible-looking but WRONG env var
    name. The actual configured name (env(example).txt,
    security_scanner.py's own pool) is "CLOUDFLARE_API_KEY_4". Every
    generic_worker role with no natural_roles match (fact_detector,
    flashcard_writer, quiz_writer, study_guide_writer,
    workflow_suggester) ranks the FULL account pool by quota, so it can
    easily land on one of these Cloudflare security-scanner slots even
    though it isn't tagged for them -- and with the old wrong name,
    utils/llm_client.py's generate_text() would ALWAYS report that slot
    as "not set" and skip it, no matter how it was actually configured,
    silently burning one of the chain's few fallback steps every time
    and pushing the real generation onto a worse-matched provider
    (a very plausible cause of quiz_writer output that drifts from the
    required '- [ ]' / '- [x]' checkbox format).
    """
    if account_id_env.startswith("CLOUDFLARE_ACCOUNT_ID_"):
        n = account_id_env.rsplit("_", 1)[-1]
        return f"CLOUDFLARE_API_KEY_{n}"
    if account_id_env.startswith("CF_SCANNER_RESERVE_") and account_id_env.endswith("_ACCOUNT_ID"):
        n = account_id_env[len("CF_SCANNER_RESERVE_"):-len("_ACCOUNT_ID")]
        return f"CF_SCANNER_RESERVE_{n}_API_TOKEN"
    raise ValueError(f"Don't know how to derive a token_env for account_id_env {account_id_env!r} "
                     f"— add its naming pattern to _cloudflare_token_env_for().")


def _chain_step_for(agent_key: str) -> dict:
    info = AGENT_CAPABILITIES[agent_key]
    provider = info["provider"]
    step = {"provider": provider, "model": PROVIDER_DEFAULT_MODEL.get(provider, ""), "key_env": agent_key}
    if provider == "cloudflare":
        # BUGFIX: this used to replace `step` with a dict that had no
        # "model" key at all. utils/llm_client.py's generate_text() reads
        # step["model"] unconditionally at the top of its per-step loop,
        # before it branches on provider == "cloudflare" -- so every chain
        # step routed to a Cloudflare account raised a bare KeyError('model')
        # (surfaced to the user as a red "model" error in Notebooks'
        # Mind Map / Workflows / any other generic_worker-backed panel).
        # Keep "model" (same PROVIDER_DEFAULT_MODEL default computed above)
        # alongside the cloudflare-specific account_id_env/token_env fields.
        account_id_env = info.get("key_id", agent_key)
        step = {"provider": provider, "model": PROVIDER_DEFAULT_MODEL.get(provider, ""),
                 "account_id_env": account_id_env,
                 "token_env": _cloudflare_token_env_for(account_id_env)}
    return step


# Fix A (reliability guide, §3 "Fix A"): how many accounts deep a single
# run() call's fallback chain goes. 3 is enough to survive one exhausted
# account plus one full provider-wide outage/quota event without needing a
# fourth hop; raise it later if that ever isn't enough in practice.
MAX_CHAIN_STEPS = 3

# Fix 4a (reliability audit, follow-up to Fix A): MAX_CHAIN_STEPS above
# was a flat constant -- always exactly 3 accounts tried, no matter how
# many working keys are actually configured across however many
# providers. If those first 3 picks happen to all be unavailable (rate-
# limited, quota-exhausted, or on a Fix-3 permanent cooldown) the whole
# call fails even when a 4th/5th/6th configured account would have
# worked fine.
#
# _dynamic_max_chain_steps() below computes the chain length at call
# time instead: count how many DISTINCT providers currently have at
# least one non-cooling-down account anywhere in AGENT_CAPABILITIES (a
# cheap proxy for "how many genuinely different fallback options exist
# right now"), and use that -- floored at the old MAX_CHAIN_STEPS so
# behavior never gets WORSE than before this fix, and capped at
# MAX_CHAIN_STEPS_CEILING so a large multi-provider roster doesn't turn
# one failed call into a dozen sequential HTTP round-trips before
# finally giving up.
MAX_CHAIN_STEPS_CEILING = 6


def _dynamic_max_chain_steps(quota_status: dict) -> int:
    from eo.panel import _is_cooling_down  # deferred — see module-level note above

    live_providers = {
        info.get("provider")
        for key, info in AGENT_CAPABILITIES.items()
        if not _is_cooling_down(key, quota_status)
    }
    return max(MAX_CHAIN_STEPS, min(MAX_CHAIN_STEPS_CEILING, len(live_providers)))


def _build_fallback_chain(role: str, quota_status: dict, max_steps: int = MAX_CHAIN_STEPS) -> list:
    """
    Fix A: replaces the old "pick exactly one account" behavior with a real
    multi-step fallback chain. Previously run() called _best_match() once
    and wrapped that single account in a length-1 chain, so the very first
    429/exhausted account was also the last -- generate_text()'s own
    fallback-chain walk (utils/llm_client.py) never got anything to fall
    through to.

    This calls eo.panel._best_match() up to `max_steps` times, growing an
    `exclude` set each round so no account is picked twice. It also prefers
    spreading the chain across DIFFERENT providers: each round first tries
    _best_match() with every account from an already-used provider excluded
    too, and only allows a repeat provider if that leaves no candidate at
    all. This means a provider-wide event (e.g. every Groq key hitting its
    daily TPD cap at once, as in the RuntimeError this fix addresses) can't
    take out the whole chain -- Cerebras/GitHub/Mistral/Cloudflare accounts
    are still tried.

    Returns a list of agent_key strings (0 to max_steps of them), in the
    order they should be attempted. An empty list means no account is
    available at all, same meaning as _best_match() returning None today.
    """
    from eo.panel import _best_match  # deferred — see module-level note above

    chain_keys = []
    used_providers = set()
    exclude = set()

    for _ in range(max_steps):
        provider_exclude = exclude | {
            key for key, info in AGENT_CAPABILITIES.items()
            if info.get("provider") in used_providers
        }
        candidate = _best_match(role, quota_status, exclude=provider_exclude)
        if candidate is None:
            # No fresh-provider candidate left this round -- allow a repeat
            # provider rather than leaving this chain slot empty, as long as
            # it's not an account already earlier in the chain.
            candidate = _best_match(role, quota_status, exclude=exclude)
        if candidate is None:
            break  # genuinely nothing left in the whole account pool
        chain_keys.append(candidate)
        exclude.add(candidate)
        used_providers.add(AGENT_CAPABILITIES[candidate].get("provider"))

    return chain_keys


def parse_next_tag(raw_text: str) -> tuple:
    """
    Migration Part 12 §5: renamed from _parse_next -- made public since
    Part 11 §2 imports it across a module boundary (agents/reviewer.py,
    agents/fixer_pool.py). No logic change from the original _parse_next,
    name only.
    """
    lines = raw_text.strip().splitlines()
    if lines and lines[-1].strip().upper().startswith("NEXT:"):
        tag = lines[-1].split(":", 1)[1].strip()
        body = "\n".join(lines[:-1]).strip()
        return body, (None if tag.upper() == "DONE" else tag)
    return raw_text.strip(), None   # no tag found — treat as done, don't crash on it


def run(role: str, task_text: str, input_keys: list = None, session_id: str = None,
        key_override=None, include_conversation_context: bool = True,
        domain: str = None, chain_override: list = None, owner_id: str = None) -> dict:
    """
    role: the exact role name the Panel/registry assigned (e.g.
        "brainstormer", "fact_checker") — also used as this call's own
        output key on the memory bus, so a later stage can read it.
    input_keys: the specific earlier stages' output this role should
        read, per this task's execution_order (eo/router.py's
        role_names[:idx] slice) — NOT the whole history, just what
        precedes this role in the resolved order.
    include_conversation_context: Part 2 §2.6. Defaults to True — today's
        exact behavior for every existing caller (the Part 23 prepend of
        conversation_memory.get_full_context()). Set False for a role
        that has no business seeing unrelated conversation history it
        wasn't scoped to (e.g. a narrow persona or single-purpose role in
        a workflow template) — input_keys is unaffected either way, since
        that's a separate, already-enforced scoping mechanism.
    domain: Part 2 §2.6, cost-tracking gap. Purely forwarded to
        generate_text() below so utils/llm_client.py's log_usage() can
        tag this call's usage for the per-project/per-section breakdown.
    chain_override: Bug fix (2026-08-12) — NEW. A ready-to-use, already
        multi-step generate_text() chain (list of step dicts), used
        AS-IS in place of this function's own key_override/
        _build_fallback_chain() logic below. Same shape and purpose as
        agents/part_price_finder.py's own chain_override parameter:
        for a caller that's about to dispatch several roles concurrently
        (eo/executor.py's _run_concurrent_group()) and has already
        reserved each of them a DISTINCT starting account (plus its own
        spread-across-providers fallback chain excluding every sibling's
        reservation) up front, via eo/dynamic_chain.py's
        build_fallback_chain_excluding() -- see that call site's own
        comment for why this is necessary: key_override below collapses
        to a length-1, no-fallback chain by design (an explicit single-
        account pin), which can't carry a multi-step reserved chain, and
        letting each concurrently-dispatched role independently call
        _build_fallback_chain() from the same quota snapshot means
        sibling roles firing at the same instant compute near-identical
        top-ranked chains and pile onto the same accounts in lockstep --
        exactly the failure mode multi-account fallback was supposed to
        prevent. None (default) leaves every other caller (the plain
        sequential path, key_override callers) completely unaffected.
        Defaults to None — no other effect on this function's behavior.
        eo/executor.py's dispatch (both the single-role and the
        concurrent-group branch) already passes this through.

    owner_id (Patch B5 — Output-Format Routing): if given, forwarded to
        eo/user_profile.py's default_format_hint(), which degrades to ""
        (today's exact system_prompt, unchanged) whenever there's no
        owner_id, no stored output_prefs, or the stored preference isn't
        confident yet. Appended after MARKDOWN_INSTRUCTION/
        NEXT_TAG_INSTRUCTION below, same "general defaults only" caveat
        MARKDOWN_INSTRUCTION's own text already carries — a role's own
        brief (an exact required structure, JSON, checkbox options, ...)
        still wins over this hint exactly the same way it already wins
        over MARKDOWN_INSTRUCTION's table/bullet suggestions. Never
        surfaced to the user; only ever nudges which of the frontend's
        already-existing renderers (ArtifactRenderer/MermaidDiagram/
        Markdown) the model's own output ends up picked up by.
    """
    brief = get_role_prompt(role)
    input_keys = input_keys or []

    context_parts = [f"TASK: {task_text}"]

    # Patch C3 — ToC lookup done once per call, not once per input_keys
    # entry: list_sections() already returns everything this session's
    # data_store artifact document has, so there's no reason to re-fetch
    # it per key. {} for a session with no artifact document at all yet
    # (list_sections()'s own "don't persist on read, don't error on
    # absence" contract) — every plain-role lookup below then falls
    # straight through to the legacy path, exactly the behavior a
    # session that predates this patch already had.
    toc_by_section = (
        {s["section_id"]: s for s in _ds_list_sections(session_id)}
        if session_id else {}
    )
    used_data_handler = False

    for k in input_keys:
        role_part, sep, section_part = k.partition(":")

        if sep:
            # Patch C3b — qualified entry ("role:section_id"): resolve
            # the named section directly, full text, no ToC step. The
            # role that declared this input_key already decided at
            # definition time it needs exactly this slice, so there's
            # no ambiguity here for a ToC pointer to save tokens on.
            try:
                section_text = _ds_read_section(session_id, section_part)
            except (KeyError, ValueError) as exc:
                context_parts.append(
                    f"--- '{k}' (requested section not available: {exc}) ---"
                )
                continue
            context_parts.append(f"--- Output from '{k}' ---\n{section_text}")
            continue

        toc_entry = toc_by_section.get(role_part)
        if toc_entry is not None:
            # Patch C3 — this role's output is in data_store (it ran
            # through this C3-patched run() before, see the write side
            # below), so point at it instead of inlining it in full.
            context_parts.append(
                f"--- '{role_part}' available (not shown in full) --- "
                f"section_id={role_part} written_by={toc_entry['written_by']} "
                f"version={toc_entry['version']} bytes={toc_entry['byte_len']}"
            )
            used_data_handler = True
            continue

        # Legacy path — unchanged from pre-C3 behavior. Reached for any
        # role with no matching data_store section: a REAL_ACTION_ROLES
        # entry from LEGACY_BUS_KEY_MAP (academic_search/web_researcher/
        # handoff_packager/extraction_table_builder — none of these
        # call this module's run(), so none of them ever get a
        # data_store section written for them) or a role from before
        # this patch existed.
        prior = bus_read(f"stage_output:{session_id}:{role_part}", default=None)
        if prior is None and role_part in LEGACY_BUS_KEY_MAP:
            # Migration Part 12 §3.4: fall back to the original v5 bus key
            # if this earlier role never wrote a stage_output entry (i.e.
            # it's a real-action-adjacent role like idea_planner/
            # prompt_writer whose actual consumer is a real-action module,
            # not another generic_worker step). app_slug-namespaced, not
            # session-namespaced -- see module docstring's caveat.
            prior = bus_read(LEGACY_BUS_KEY_MAP[role_part], default=None)
        if prior:
            context_parts.append(f"--- Output from '{role_part}' ---\n{prior}")
            # BUGFIX (bug audit, patch 4): getting the real report into
            # context (patch 2's LEGACY_BUS_KEY_MAP bridge) fixed the
            # blind spot, but a raw {"papers": [], "summary": "0 paper(s)
            # found..."} dict buried in a wall of context is easy for the
            # model to skim past and fill the gap from its own
            # parametric knowledge anyway -- exactly the "confident
            # literature review, zero real sources" failure mode that
            # started this audit. Spell it out as an explicit, unmissable
            # instruction instead of trusting the model to notice a
            # count field on its own.
            zero_results_notice = _zero_results_notice(role_part, prior)
            if zero_results_notice:
                context_parts.append(zero_results_notice)
    context = "\n\n".join(context_parts)   # CHANGED — conv_context no longer in this join

    if include_conversation_context:   # CHANGED — moved from top of function to here
        conv_context = conversation_memory.get_full_context(session_id)   # Part 23
        if conv_context:
            # NEW — Patch 1b: conversation history goes AFTER the droppable
            # marker, at the very end, so a 413 shrink drops it FIRST (via
            # _shrink_prompt_for_retry()'s "drop everything after the last
            # marker occurrence" rule) before ever falling back to blind-
            # slicing task/input_keys content. Conversation context is
            # reconstructable next turn from eo/conversation_memory.py's own
            # store; a role's actual input_keys content is not recoverable
            # once truncated mid-sentence.
            context = (
                f"{context}{DROPPABLE_CONTEXT_MARKER}"
                f"--- Recent conversation ---\n{conv_context}"
            )

    if chain_override is not None:
        # Bug fix (2026-08-12): a caller-reserved, already multi-step
        # chain (see this parameter's own docstring above) — used
        # exactly as given, bypassing both key_override's collapse-to-
        # one-step behavior and this function's own _build_fallback_chain()
        # call, since the whole point is that the CALLER already did that
        # ranking itself, coordinated across a group of sibling roles.
        chain = chain_override
    elif key_override:
        # Explicit override — the caller picked this exact account on
        # purpose (e.g. a targeted retry), so it stays a single-step chain
        # rather than being expanded automatically.
        agent_key = key_override if isinstance(key_override, str) else key_override[0]
        chain = [_chain_step_for(agent_key)] if agent_key else []
    else:
        # Fix A: real multi-step, multi-provider fallback chain instead of
        # a single _best_match() pick wrapped in a length-1 chain.
        # Fix 4a: chain length is now computed from live account/provider
        # health at call time instead of the old flat MAX_CHAIN_STEPS.
        quota_status = get_quota_snapshot()
        chain_keys = _build_fallback_chain(role, quota_status,
                                            max_steps=_dynamic_max_chain_steps(quota_status))
        chain = [_chain_step_for(k) for k in chain_keys]

    # Patch C7 — deferred import, not a top-level one; see this
    # module's own docstring (Patch C7 paragraph) for why.
    from eo.capabilities import ensure_skill_for_task, get_relevant_skill

    # NEW — Part 6 §E2, task 14: "agents read [a skill doc] before
    # attempting an unfamiliar task type." get_relevant_skill() already
    # degrades to "" on any retrieval failure or genuine no-match (see
    # its own docstring) -- that empty case just means no addition
    # here, same as brief being None for a role that's never been
    # briefed. Deliberately placed BEFORE MARKDOWN_INSTRUCTION/
    # NEXT_TAG_INSTRUCTION (both fixed, role-agnostic formatting rules
    # that apply regardless of task type) and framed as guidance, not
    # as part of the role's own identity/brief, so a role with a skill
    # match doesn't read as if the skill doc IS its brief.
    skill_doc = "" if role in STRICT_FORMAT_ROLES else get_relevant_skill(task_text)
    if not skill_doc and role not in STRICT_FORMAT_ROLES:
        # NEW — Part 6 §E2, task 14, self-improvement loop: "" here is
        # the load-bearing "no skill matches this task type yet" signal
        # get_relevant_skill()'s own docstring describes, not just
        # "nothing to add" — worth researching and writing one for.
        # ensure_skill_for_task() already swallows its own failures
        # (network, LLM provider, embedding) and never raises, but this
        # try/except is kept anyway, same belt-and-suspenders posture
        # eo/routing_memory.py's own log_outcome() embed step earns just
        # by being a best-effort side-channel: the skill this writes (if
        # it writes one) benefits a FUTURE task of a similar kind, not
        # this one, so nothing about it may ever cost this request more
        # than the one research pass + one cheap condensation call it
        # takes on the way to a plain "no skill found" outcome.
        try:
            ensure_skill_for_task(task_text)
        except Exception as exc:
            print(f"  [Generic Worker] skill self-improvement loop skipped "
                  f"({exc.__class__.__name__}: {exc}).")
    skill_addition = (
        f"\n\nGuidance from a similar task type you've handled before:\n{skill_doc}"
        if skill_doc else ""
    )

    # Patch C3 — only appended when this call's context actually
    # contains a ToC pointer (see the input_keys loop above); a role
    # with none shouldn't be told about a mechanism it has nothing to
    # use it on.
    data_handler_addition = DATA_HANDLER_INSTRUCTION if used_data_handler else ""

    system_prompt = (
        (brief or "") + skill_addition + MARKDOWN_INSTRUCTION + NEXT_TAG_INSTRUCTION
        + data_handler_addition   # NEW — Patch C3
        + default_format_hint(owner_id)   # NEW — Patch B5
    )
    try:
        raw = generate_text(
            system_prompt=system_prompt,
            user_content=context,
            chain=chain,
            agent_name=f"generic:{role}",
            session_id=session_id,
            domain=domain,
        )
    except RuntimeError:
        # Fix 4b (additive, on top of 4a above): the chain we just tried
        # exhausted end to end. Every step's own failure already wrote a
        # fresh cooldown_until entry to the bus as it happened (Fix B /
        # utils/llm_client.py's _set_cooldown()), so a BRAND NEW quota
        # snapshot fetched right now already reflects those cooldowns --
        # rebuilding the chain once more here can reach candidates this
        # attempt's chain never tried (accounts the provider-spreading
        # order in _build_fallback_chain() skipped past this round, or
        # ones outside this attempt's dynamic chain length). One retry
        # only: an explicit key_override never retries (the caller picked
        # that exact account on purpose), and if this second attempt's
        # freshly-built chain is empty or also exhausts, the RuntimeError
        # propagates for real -- this is one more honest attempt, not a
        # way to paper over an actually-down provider indefinitely.
        #
        # Bug fix (2026-08-12): chain_override never retries here either,
        # for a different reason than key_override -- rebuilding via the
        # plain _build_fallback_chain() call below would be exactly the
        # uncoordinated, no-visibility-into-siblings chain this parameter
        # exists to avoid (see its docstring). A caller that reserved a
        # coordinated chain across a group of concurrent roles is
        # responsible for deciding what a real exhaustion means for that
        # group -- silently falling back to an every-role-for-itself
        # retry here would reintroduce the same lockstep-collision this
        # fix is for, just one level deeper.
        if key_override or chain_override is not None:
            raise
        retry_quota_status = get_quota_snapshot()
        retry_chain_keys = _build_fallback_chain(
            role, retry_quota_status, max_steps=_dynamic_max_chain_steps(retry_quota_status))
        retry_chain = [_chain_step_for(k) for k in retry_chain_keys]
        if not retry_chain:
            raise
        raw = generate_text(
            system_prompt=system_prompt,
            user_content=context,
            chain=retry_chain,
            agent_name=f"generic:{role}",
            session_id=session_id,
            domain=domain,
        )
    body, next_destination = parse_next_tag(raw)

    # Bug fix (audit follow-up): see STRUCTURAL_FORMAT_CHECKS above for
    # why this exists. One retry only, same "one more honest attempt, not
    # a way to paper over a real failure indefinitely" posture the
    # RuntimeError retry above already uses -- a SECOND consecutive
    # format miss almost certainly means the source material itself
    # doesn't support the requested format (or every reachable model in
    # the chain is struggling the same way), not one unlucky reroute, so
    # this raises a clear, specific error instead of silently persisting
    # bad content for a caller (podcast_scriptwriter.py, rehearsal_
    # scriptwriter.py, slide_deck_planner.py) that has no way to tell a
    # well-formed result from a broken one itself.
    format_check = STRUCTURAL_FORMAT_CHECKS.get(role)
    if format_check is not None:
        checker, correction_hint, what_missing = format_check
        if not checker(body):
            print(
                f"  [generic:{role}] output failed its structural format "
                f"check -- retrying once with an explicit format correction."
            )
            retry_system_prompt = (
                system_prompt + f"\n\nIMPORTANT: {correction_hint}"
            )
            try:
                retry_raw = generate_text(
                    system_prompt=retry_system_prompt,
                    user_content=context,
                    chain=chain,
                    agent_name=f"generic:{role}",
                    session_id=session_id,
                    domain=domain,
                )
            except RuntimeError:
                retry_raw = None
            if retry_raw is not None:
                retry_body, retry_next_destination = parse_next_tag(retry_raw)
                if checker(retry_body):
                    body, next_destination = retry_body, retry_next_destination
                else:
                    raise ValueError(
                        f"{role}: model output still had no {what_missing} "
                        "after one retry -- giving up rather than passing "
                        "unusable content downstream."
                    )
            else:
                raise ValueError(
                    f"{role}: model output had no {what_missing}, and the "
                    "retry chain was exhausted -- giving up rather than "
                    "passing unusable content downstream."
                )

    if session_id:
        bus_write(f"stage_output:{session_id}:{role}", body)
        # Patch C3 — additional write, not a replacement of the line
        # above; see this module's own docstring (Patch C3 paragraph)
        # for why both writes stay. section_id == role name: one
        # section per role's whole output, matching this write's own
        # existing granularity. author_role == role, since this IS
        # that role finishing.
        _ds_write_section(session_id, role, body, role)
    if role in LEGACY_BUS_KEY_MAP:
        # Migration Part 12 §3.4: also feed the original v5 bus key so
        # code_writers.py etc. keep reading real input, unmodified.
        bus_write(LEGACY_BUS_KEY_MAP[role], body)
    return {"role": role, "text": body, "next_destination": next_destination}


# Deliberately imported here, at the bottom of the file, not at the top:
# eo.registry.py's own bottom-of-file import block (`from agents import
# (..., generic_worker, ...)`) runs BEFORE this module has finished
# executing whenever *this* module -- not eo.registry -- is the first one
# actually imported in the process (confirmed via api/providers/
# role_provider.py's `from agents.generic_worker import run as
# generic_worker_run`, one real entry point). At that point eo.registry
# builds its REGISTRY dict, which references `generic_worker.run` --
# if the top-level `from eo.registry import AGENT_CAPABILITIES,
# get_role_prompt` line ran up here instead, Python would still be
# sitting on THIS line when eo.registry reaches that reference, and
# `generic_worker.run` (defined only below) wouldn't exist yet:
# AttributeError: partially initialized module 'agents.generic_worker'
# has no attribute 'run'. Placing the import down here, after run() and
# every other name this module defines, guarantees eo.registry always
# sees a fully-formed module by the time it needs one, regardless of
# which of the two modules a given process happens to import first.
from eo.registry import AGENT_CAPABILITIES, get_role_prompt  # noqa: E402