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
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import get_role_prompt, AGENT_CAPABILITIES
from eo.quota_sentinel import get_quota_snapshot
from eo import conversation_memory   # NEW — Part 23
from utils.llm_client import generate_text
from memory.bus import read as bus_read, write as bus_write, KEYS
# Part 6 §6.4 bridge — see LEGACY_BUS_KEY_MAP below. No circularity risk:
# agents/handoff_packager.py imports only memory.bus/relay.emitter/
# eo.errors/agents.architecture_diagrammer/agents.schema_diagrammer, none
# of which import this module or eo.registry.
from agents.handoff_packager import PLAN_HANDOFF_PACKAGE_KEY
# NOTE: `from eo.panel import _best_match` is deliberately NOT imported at
# module level here. eo.registry.py now imports this module (generic_worker)
# at load time so resolve("generic_worker") works, and eo.panel.py imports
# FROM eo.registry (AGENT_CAPABILITIES, get_role_prompt) -- importing
# eo.panel here too would close a circular loop:
#   eo.registry -> agents.generic_worker -> eo.panel -> eo.registry
# Deferring this one import to inside run() (below) breaks the cycle: by
# the time run() is actually CALLED, both modules have finished loading.

PROVIDER_DEFAULT_MODEL = {
    "groq": "llama-3.3-70b-versatile",
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
    "mistral": "mistral-large-latest",
    "github": "openai/gpt-4.1-mini",
    # BUGFIX: was missing entirely, so _chain_step_for()'s cloudflare
    # branch had no default to fall back on. Same model string already
    # used for cloudflare steps elsewhere (agents/dependency_mapper.py,
    # utils/llm_client.py's own docstring example).
    "cloudflare": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
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
# input_keys but find nothing there. Other Part 3 real-action roles don't
# need an entry: academic_search's output isn't read by name downstream,
# and contradiction_prefilter/source_quality_flagger already write their
# own stage_output entry directly.
LEGACY_BUS_KEY_MAP = {
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
    from eo.panel import _best_match   # deferred — see module-level note above

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
        domain: str = None) -> dict:
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
        Defaults to None — no other effect on this function's behavior.
        eo/executor.py's dispatch (both the single-role and the
        concurrent-group branch) already passes this through.
    """
    brief = get_role_prompt(role)
    input_keys = input_keys or []

    context_parts = [f"TASK: {task_text}"]
    if include_conversation_context:   # Part 2 §2.6 — opt-out gate
        conv_context = conversation_memory.get_full_context(session_id)   # Part 23
        if conv_context:
            context_parts.insert(0, f"--- Recent conversation ---\n{conv_context}")   # Part 23

    for k in input_keys:
        prior = bus_read(f"stage_output:{session_id}:{k}", default=None)
        if prior is None and k in LEGACY_BUS_KEY_MAP:
            # Migration Part 12 §3.4: fall back to the original v5 bus key
            # if this earlier role never wrote a stage_output entry (i.e.
            # it's a real-action-adjacent role like idea_planner/
            # prompt_writer whose actual consumer is a real-action module,
            # not another generic_worker step). app_slug-namespaced, not
            # session-namespaced -- see module docstring's caveat.
            prior = bus_read(LEGACY_BUS_KEY_MAP[k], default=None)
        if prior:
            context_parts.append(f"--- Output from '{k}' ---\n{prior}")
    context = "\n\n".join(context_parts)

    if key_override:
        # Explicit override — the caller picked this exact account on
        # purpose (e.g. a targeted retry), so it stays a single-step chain
        # rather than being expanded automatically.
        agent_key = key_override if isinstance(key_override, str) else key_override[0]
        chain = [_chain_step_for(agent_key)] if agent_key else []
    else:
        # Fix A: real multi-step, multi-provider fallback chain instead of
        # a single _best_match() pick wrapped in a length-1 chain.
        chain_keys = _build_fallback_chain(role, get_quota_snapshot())
        chain = [_chain_step_for(k) for k in chain_keys]
    raw = generate_text(
        system_prompt=(brief or "") + MARKDOWN_INSTRUCTION + NEXT_TAG_INSTRUCTION,
        user_content=context,
        chain=chain,
        agent_name=f"generic:{role}",
        session_id=session_id,
        domain=domain,
    )
    body, next_destination = parse_next_tag(raw)
    if session_id:
        bus_write(f"stage_output:{session_id}:{role}", body)
    if role in LEGACY_BUS_KEY_MAP:
        # Migration Part 12 §3.4: also feed the original v5 bus key so
        # code_writers.py etc. keep reading real input, unmodified.
        bus_write(LEGACY_BUS_KEY_MAP[role], body)
    return {"role": role, "text": body, "next_destination": next_destination}