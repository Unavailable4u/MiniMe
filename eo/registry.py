"""
eo/registry.py — Stage 4, step 1 of the v5 Master Blueprint's build roadmap
(Part 10).
Single source of truth mapping an agent *name* (string, the vocabulary the
Inspector/Router speak in) to the real, importable Python callable that
does the work, plus a couple of bits of metadata router.py and the future
executor need.
This module intentionally does NOT execute anything. It only resolves
names -> callables. That keeps it safe to import from tests, from
router.py, and eventually from an executor, without any side effects
(no LLM calls, no memory writes) just from `import eo.registry`.
The production 19-agent roster (tier 3, Part 4 of the blueprint), plus
Tier 0's Responder and Tier 1's lean pipeline (Part 2.3-2.4, added in
Stage 4 steps 2-4 of the roadmap), are all wired up here.
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write


AGENT_CAPABILITIES = {
    # --- Groq: sequential low-volume roles ---
    "GROQ_API_KEY": {
        "provider": "groq",
        "strengths": ["general reasoning", "fast, reliable for sequential low-volume roles"],
        # "researcher"/"writer"/"analyst"/"formatter" kept from the Part 5
        # fix -- Migration Part 9's guide table omits these, but removing
        # them would silently re-break staff_task()'s _best_match() for
        # those four abstract roles (they'd have zero candidates again).
        # Migration Part 12 §4: added "brainstormer", "outliner", "editor"
        # for the richer non-coding role tags. Not a blind += of the
        # guide's full list -- "writer"/"researcher"/"analyst"/
        # "formatter"/"gatekeeper" were already present (Part 5/9 fixes
        # above), so only the genuinely new roles were appended to avoid
        # duplicate entries in this list.
        "natural_roles": ["idea_planner", "prompt_writer", "test_writer", "report_writer", "gatekeeper",
                           "researcher", "writer", "analyst", "formatter",
                           "brainstormer", "outliner", "editor"],
    },

    # --- Groq: Reviewer Pool — base 3, reserve 2 (Part 3 §4.2) ---
    "GROQ_API_KEY_6": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor"]},
    "GROQ_API_KEY_7": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor"]},
    "GROQ_API_KEY_8": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor"]},
    "GROQ_RESERVE_1": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor"]},
    "GROQ_RESERVE_2": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor"]},

    # --- Groq: Structure Architect (isolated single account) ---
    "GROQ_API_KEY_9": {
        "provider": "groq", "strengths": ["file/folder planning"], "natural_roles": ["structure_architect"],
    },

    # --- Groq: Starter General Agents (not part of role hiring, tracked for quota) ---
    "SGA_GROQ_1": {"provider": "groq", "strengths": ["fast direct answers"], "natural_roles": ["sga"]},
    "SGA_GROQ_2": {"provider": "groq", "strengths": ["fast direct answers"], "natural_roles": ["sga"]},
    "SGA_GROQ_3": {"provider": "groq", "strengths": ["fast direct answers"], "natural_roles": ["sga"]},

    # --- Groq: Inspector (isolated, not part of role hiring, tracked for quota) ---
    "EO_INSPECTOR_GROQ_KEY_1": {"provider": "groq", "strengths": ["triage"], "natural_roles": ["inspector"]},
    "EO_INSPECTOR_GROQ_KEY_2": {"provider": "groq", "strengths": ["triage"], "natural_roles": ["inspector"]},

    # --- Cerebras: Code Writer Pool — base 5, reserve 3 (Part 3 §4.1) ---
    "CEREBRAS_API_KEY_1": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer"]},
    "CEREBRAS_API_KEY_2": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer"]},
    "CEREBRAS_API_KEY_3": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer"]},
    "CEREBRAS_API_KEY_4": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer"]},
    "CEREBRAS_API_KEY_5": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer"]},
    "CEREBRAS_RESERVE_1": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer"]},
    "CEREBRAS_RESERVE_2": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer"]},
    "CEREBRAS_RESERVE_3": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer"]},

    # --- Cerebras: Fixer Pool (fixed 3, no reserve tier defined anywhere in Parts 1-8) ---
    "CEREBRAS_API_KEY_6": {"provider": "cerebras", "strengths": ["bug fixing"], "natural_roles": ["fixer"]},
    "CEREBRAS_API_KEY_7": {"provider": "cerebras", "strengths": ["bug fixing"], "natural_roles": ["fixer"]},
    "CEREBRAS_API_KEY_8": {"provider": "cerebras", "strengths": ["bug fixing"], "natural_roles": ["fixer"]},
    # Note: "report_writer" is no longer tagged on CEREBRAS_API_KEY_1 as it
    # was pre-Part-9 -- GROQ_API_KEY above already covers that role, and
    # Part 9's real account table ties CEREBRAS_API_KEY_1-5/RESERVE_1-3
    # to "implementer" only, matching code_writers.py's actual pool.

    # --- Cerebras: EO Panel Member B (isolated, real key name confirmed in Part 2 §0.2) ---
    "EO_PANEL_CEREBRAS_KEY": {
        "provider": "cerebras", "strengths": ["distinct model lineage", "second opinion"],
        # Migration Part 12 §4: added "researcher", "fact_checker".
        "natural_roles": ["panel_member_b", "researcher", "fact_checker"],
    },

    # --- Cloudflare: Security Scanner Pool — base 5, reserve 3 (Part 3 §4.3) ---
    # Replaces the old single made-up CLOUDFLARE_API_KEY_1 entry, which
    # didn't correspond to any slot security_scanner.py actually rotates
    # through (its real CLOUDFLARE_KEY_SLOTS are 4-8, plus 3 reserve slots
    # with their own env-var naming pattern). Dict keys are account_id_env
    # strings, not token_env, per Part 8 §2's key_id convention -- that's
    # what log_usage() actually keys cloudflare usage under. "dependency_mapper"
    # kept on the first slot only, preserving the one existing candidate
    # for that role that Part 9's literal guide table dropped entirely.
    "CLOUDFLARE_ACCOUNT_ID_4": {
        "provider": "cloudflare", "key_id": "CLOUDFLARE_ACCOUNT_ID_4",
        "strengths": ["security scanning"], "natural_roles": ["security_reviewer", "dependency_mapper"],
    },
    "CLOUDFLARE_ACCOUNT_ID_5": {
        "provider": "cloudflare", "key_id": "CLOUDFLARE_ACCOUNT_ID_5",
        "strengths": ["security scanning"], "natural_roles": ["security_reviewer"],
    },
    "CLOUDFLARE_ACCOUNT_ID_6": {
        "provider": "cloudflare", "key_id": "CLOUDFLARE_ACCOUNT_ID_6",
        "strengths": ["security scanning"], "natural_roles": ["security_reviewer"],
    },
    "CLOUDFLARE_ACCOUNT_ID_7": {
        "provider": "cloudflare", "key_id": "CLOUDFLARE_ACCOUNT_ID_7",
        "strengths": ["security scanning"], "natural_roles": ["security_reviewer"],
    },
    "CLOUDFLARE_ACCOUNT_ID_8": {
        "provider": "cloudflare", "key_id": "CLOUDFLARE_ACCOUNT_ID_8",
        "strengths": ["security scanning"], "natural_roles": ["security_reviewer"],
    },
    "CF_SCANNER_RESERVE_1_ACCOUNT_ID": {
        "provider": "cloudflare", "key_id": "CF_SCANNER_RESERVE_1_ACCOUNT_ID",
        "strengths": ["security scanning"], "natural_roles": ["security_reviewer"],
    },
    "CF_SCANNER_RESERVE_2_ACCOUNT_ID": {
        "provider": "cloudflare", "key_id": "CF_SCANNER_RESERVE_2_ACCOUNT_ID",
        "strengths": ["security scanning"], "natural_roles": ["security_reviewer"],
    },
    "CF_SCANNER_RESERVE_3_ACCOUNT_ID": {
        "provider": "cloudflare", "key_id": "CF_SCANNER_RESERVE_3_ACCOUNT_ID",
        "strengths": ["security scanning"], "natural_roles": ["security_reviewer"],
    },

    # --- Mistral, GitHub Models, HuggingFace: single-account fixed-purpose roles ---
    "MISTRAL_API_KEY": {
        "provider": "mistral", "strengths": ["documentation", "long-form writing"],
        # "final_qa" kept -- Part 9's literal guide table dropped it,
        # which would have left that role with zero candidates.
        # Migration Part 12 §4: added "writer", "editor".
        "natural_roles": ["documentation_writer", "final_qa", "writer", "editor"],
    },
    "GITHUB_MODELS_PAT": {"provider": "github", "strengths": ["general fallback"], "natural_roles": ["fallback"]},
    "EO_PANEL_GITHUB_PAT": {"provider": "github", "strengths": ["panel voting", "fallback"], "natural_roles": ["panel_member_c"]},
    # "HUGGINGFACE_API_KEY" kept -- Part 9's literal guide table omits it
    # entirely, but it's the only registered candidate for "memory_search"
    # and "duplication_checker"; dropping it would silently break hiring
    # for both roles again, the same failure mode Part 5 originally fixed.
    "HUGGINGFACE_API_KEY": {
        "provider": "huggingface", "strengths": ["semantic search", "duplication detection"],
        "natural_roles": ["memory_search", "duplication_checker"],
    },
}



ROLE_PROMPTS_KEY = "registry:role_prompts"

# What used to be the live ROLE_PROMPTS dict is now only a SEED — the
# starting contents on a totally fresh system, before the Panel has ever
# written anything of its own. After the first run, the memory bus (via
# the registry:-prefixed, non-namespaced key added in Part 7 §0) is the
# real source of truth; this dict is never read again except to bootstrap.
ROLE_PROMPTS_SEED = {
    "implementer": "You are a focused implementer. Write clean, working code for exactly the scope you were briefed on — do not expand scope on your own.",
    "verifier": "You are a verifier. Check the given output against its stated goal and report pass/fail with specific reasons — do not fix issues yourself, only report them.",
    "researcher": "You gather and synthesize information on a topic from provided sources or general knowledge. Flag anything you're unsure of rather than stating it as fact.",
    "writer": "You draft prose to a specified tone, length, and format from a brief or outline.",
    "fact_checker": "You review a draft against source material or general knowledge and flag unsupported claims. You do not rewrite — only annotate.",

    # Part 1 §1.3 — hand-written up front rather than left to the
    # cold-start brief writer, since a bad first-draft persona brief
    # becomes the permanent version once add_role_prompt() saves it (see
    # this module's docstring for get_role_prompt()'s bootstrap
    # behavior). Each brief describes how the persona thinks and reacts
    # in general, never anything about a specific product — the same
    # generalization rule every seed brief above already follows, since
    # these get reused verbatim across every future task that hires the
    # role.
    "persona_customer": (
        "You react to a product, feature, or pricing decision the way an "
        "enthusiastic-but-realistic everyday customer would — voicing "
        "genuine excitement, hesitation, or confusion in your own words. "
        "Stay in character as a customer, not an analyst; do not reference "
        "internal reasoning or business strategy the customer wouldn't know."
    ),
    "persona_skeptic": (
        "You react to a product, feature, or pricing decision the way a "
        "skeptical, hard-to-convince customer would — assuming the pitch "
        "is exaggerated until proven otherwise and voicing the doubts most "
        "reviews leave unsaid. Stay in character as a skeptical customer, "
        "not a hostile critic; your skepticism should feel earned, not "
        "performative."
    ),
    "critic_reviewer": (
        "You evaluate the given work the way an experienced, opinionated "
        "professional critic in its field would — praising real strengths "
        "specifically and calling out weaknesses just as specifically, in "
        "a confident published-review voice. Give an overall verdict, not "
        "just scattered observations."
    ),
    "usability_walkthrough": (
        "You simulate a first-time user attempting to complete a specific "
        "task with the given product or flow, narrating each step, where "
        "you hesitate, misclick, or get confused, and where the "
        "experience feels smooth. Report friction points as they'd "
        "actually happen in the moment, not as a retrospective list of "
        "design principles."
    ),
    "red_team": (
        "You actively try to find ways the given product, plan, or system "
        "could fail, be misused, or be exploited — thinking like an "
        "adversary or worst-case user, not a well-intentioned one. Be "
        "specific about the failure mode and how it would actually "
        "happen, not just that a risk 'exists.'"
    ),
    "pricing_sensitivity": (
        "You react to a specific price or pricing change the way a real "
        "prospective buyer weighing it against alternatives and their own "
        "budget would — including whether it feels fair, cheap, or "
        "expensive relative to perceived value. Give a specific reaction "
        "(e.g. would/wouldn't pay, or at what price you'd reconsider), "
        "not a generic pricing lecture."
    ),
    "support_ticket_predictor": (
        "You predict the concrete support tickets, complaints, and "
        "confused questions real users would submit after encountering "
        "the given product or feature, written the way an actual user "
        "would phrase them — not as a QA test-case list. Predict the "
        "volume and tone (frustrated, confused, urgent) as well as the "
        "content."
    ),
    "competitor_response": (
        "You predict how a rational competitor would actually respond to "
        "the given product, feature, or pricing move — matching, "
        "ignoring, undercutting, or repositioning — reasoning the way a "
        "competitor's own strategy team would. Ground your prediction in "
        "plausible competitive incentives, not speculation about what "
        "would be dramatic."
    ),
    # Part 1 §1.4, track 2 — a batch-generation role, not a persona. Used
    # ALONE for "spread of reviews"-style requests (e.g. "15 App Store
    # reviews"), never combined with the individual personas above --
    # hiring N separate roles is the wrong shape for "a realistic
    # distribution of the SAME kind of reaction." One call, one role
    # slot, no mode-ceiling pressure (see eo/router.py's MODE_CEILINGS).
    # The fenced-```json instruction is deliberate: agents/
    # generic_worker.py appends MARKDOWN_INSTRUCTION to every role's
    # system prompt unconditionally, and that instruction already tells
    # the model to use fenced code blocks for any code -- leaning into
    # that (rather than trying to suppress markdown for this one role)
    # keeps this a zero-code-change addition like every other role here.
    "marketplace_review_batch": (
        "You generate a realistic distribution of N marketplace-style "
        "reviews (e.g. App Store, Amazon) for the given product, "
        "feature, or update — a genuine mix of positive, neutral, and "
        "negative reactions, each in a different, plausible reviewer's "
        "own voice, not N variations of the same opinion. Default to 10 "
        "reviews if the task doesn't specify a count. Output your answer "
        "as a single fenced ```json code block containing one JSON array "
        "of objects, each with \"rating\" (1-5), \"sentiment\" "
        "(\"positive\"/\"neutral\"/\"negative\"), and \"text\" (the "
        "review itself) — nothing else outside that code block."
    ),
    # Deliberately NOT a dedup/aggregation pass like
    # agents/review_aggregator.py — personas are SUPPOSED to disagree
    # (see §1.5), so this brief explicitly instructs against flattening
    # that disagreement away.
    "simulation_synthesizer": (
        "You read every persona's reaction to the same product or "
        "decision and synthesize them into one summary: what most "
        "personas agreed on, where they genuinely disagreed and why, and "
        "an overall read. Preserve real disagreement between personas "
        "explicitly — do not average conflicting reactions into a single "
        "flattened conclusion."
    ),
}


import datetime as _datetime


def _utcnow_iso() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


def _wrap_legacy_entry(role_name: str, brief: str) -> dict:
    """Part 2 §2.2 schema widening. A pre-migration store held bare
    {role_name: brief_string}. Wrap a legacy bare string into the new
    {brief, source, updated_at, times_hired} shape. There's no way to
    recover real history for these, so tag honestly rather than guess
    favorably: if the string is byte-for-byte still the current seed
    value, "seed" is actually correct (that's how it got there);
    anything else is tagged "panel_brief_writer" — the only other way a
    bare string could have ended up in the pre-migration store — so the
    UI correctly flags it as an unreviewed cold-start brief instead of
    silently implying a human wrote it."""
    source = "seed" if ROLE_PROMPTS_SEED.get(role_name) == brief else "panel_brief_writer"
    return {"brief": brief, "source": source, "updated_at": None, "times_hired": 0}


def _load_prompts() -> dict:
    """Single read path for every function below. Bootstraps from
    ROLE_PROMPTS_SEED on the very first call if the memory bus has
    nothing yet (unchanged behavior), and migrates any bare-string
    legacy entries into the new object shape in the same pass — no
    separate migration script needed, per Part 2 §2.2's design. Writes
    back to the bus only when bootstrap or migration actually changed
    something, so a store that's already fully migrated costs one read
    and zero writes."""
    prompts = read(ROLE_PROMPTS_KEY, default=None)
    if prompts is None:
        prompts = {
            name: _wrap_legacy_entry(name, brief)
            for name, brief in ROLE_PROMPTS_SEED.items()
        }
        write(ROLE_PROMPTS_KEY, prompts)
        return prompts

    changed = False
    for role_name, value in list(prompts.items()):
        if not isinstance(value, dict):
            prompts[role_name] = _wrap_legacy_entry(role_name, value)
            changed = True
    if changed:
        write(ROLE_PROMPTS_KEY, prompts)
    return prompts


def get_role_prompt(role_name: str) -> str | None:
    """Returns the stored brief for this role as a plain string, or
    None if it's never been written — exactly today's return contract.
    Every existing caller (agents/generic_worker.py's run(),
    eo/panel.py's _get_or_write_role_prompt()) keeps working
    unmodified even though the underlying storage shape widened."""
    entry = _load_prompts().get(role_name)
    return entry["brief"] if entry else None


def get_role_metadata(role_name: str) -> dict | None:
    """New in Part 2 §2.2 — returns the full {brief, source,
    updated_at, times_hired} object for the Role Library UI, or None
    if this role has never been briefed. get_role_prompt() above stays
    the string-only contract every non-UI caller already depends on;
    this is the richer read path for the new frontend panel only."""
    return _load_prompts().get(role_name)


def add_role_prompt(role_name: str, brief: str, source: str = "panel_brief_writer") -> None:
    """Writes a newly-generated brief back into the persistent store.
    This is what makes the registry actually grow instead of writing
    the same role's brief on every single task that needs it.

    Defaults to source="panel_brief_writer" — unchanged call shape for
    eo/panel.py's _get_or_write_role_prompt(), which calls this every
    time it writes a role's cold-start brief for the first time; only
    the stored value's shape widened, not this function's default
    behavior. Pass source="user_edited" (or just call
    update_role_prompt() below) when a human wrote or edited the
    brief instead. Preserves any existing times_hired count rather
    than resetting it, since re-briefing a role isn't the same event
    as it being hired."""
    prompts = _load_prompts()
    prompts[role_name] = {
        "brief": brief,
        "source": source,
        "updated_at": _utcnow_iso(),
        "times_hired": prompts.get(role_name, {}).get("times_hired", 0),
    }
    write(ROLE_PROMPTS_KEY, prompts)


def update_role_prompt(role_name: str, new_brief: str, source: str = "user_edited") -> None:
    """New in Part 2 §2.2 — thin wrapper over add_role_prompt(), just
    setting source explicitly so the Role Library UI can visually
    distinguish "you wrote this" from "the system generated this and
    nobody's reviewed it yet." This is what the UI's inline-edit save
    action calls; it directly surfaces the exact risk Part 1 §1.3
    flagged (an unreviewed cold-start brief silently becoming
    permanent)."""
    add_role_prompt(role_name, new_brief, source=source)


def record_role_hire(role_name: str) -> None:
    """New in Part 2 §2.2 — increments times_hired for a role that was
    just staffed. Not yet called from eo/panel.py in this pass (that's
    a one-line addition inside staff_task() once panel.py is in scope
    for §2.3/§2.5); exposed here now so that follow-up has something to
    call. Creates a bare counter entry rather than raising if the role
    somehow isn't in the store yet (a hire can in principle race a
    first-ever brief write)."""
    prompts = _load_prompts()
    entry = prompts.get(role_name) or {
        "brief": None, "source": "panel_brief_writer",
        "updated_at": None, "times_hired": 0,
    }
    entry["times_hired"] = entry.get("times_hired", 0) + 1
    prompts[role_name] = entry
    write(ROLE_PROMPTS_KEY, prompts)


def list_known_roles() -> list:
    """Every role the system has ever written a brief for — unchanged
    return contract (sorted list of role-name strings) even though the
    underlying store now holds richer objects per role."""
    return sorted(_load_prompts().keys())

# Migration Part 10 §2.1 — replaces Part 5's ROLE_TO_AGENT-based
# resolve_role(). Only roles that perform a real action (write files to
# disk, call a scanning API) get an explicit mapping to their dedicated
# module name. Every reasoning-only role — brainstorming, writing,
# editing, research, fact-checking, formatting, and coding
# review/verify/fix — resolves to the literal string "generic_worker"
# instead. eo/executor.py's dispatch (Part 10 §4) is what actually
# routes that string to agents.generic_worker.run(role=role_name, ...).
#
# A brand-new role name the Panel invents next month needs zero code
# changes here to run — it just falls through to generic_worker.
REAL_ACTION_ROLES = {
    "implementer": "code_writers",
    "verifier": "reviewer",
    "fixer": "fixer_pool",
    "security_reviewer": "security_scanner",
    "file_manager": "file_manager",
    "idea_planner": "idea_planner",      # ADD
    "prompt_writer": "prompt_writer",    # ADD
    "test_writer": "test_writer",        # ADD
    "dependency_mapper": "dependency_mapper",
    "duplication_checker": "duplication_checker",
    "structure_architect": "structure_architect",
    "memory_search": "memory_search",
}


def resolve_role(role_name: str) -> str:
    """Real-action roles resolve to their dedicated module name, exactly
    as before. Everything else resolves to the literal string
    'generic_worker' — execute_graph's dispatch (Part 10 §4) is what
    actually routes that to agents.generic_worker.run(role=role_name,
    ...)."""
    return REAL_ACTION_ROLES.get(role_name, "generic_worker")

from agents import (
    memory_search,
    idea_planner,
    prompt_writer,
    code_writers,
    dependency_mapper,
    test_writer,
    reviewer,
    duplication_checker,
    fixer_pool,
    sandbox_tester,
    structure_architect,
    security_scanner,
    security_aggregator,
    file_manager,
    documentation_agent,
    report_writer,
    responder,
    prompt_writer_lean,
    code_writer_lean,
    reviewer_fixer_lean,
    generic_worker,
)

# name -> {"callable": fn, "needs_cycle_num": bool}
#
# "needs_cycle_num" flags the one agent (Gatekeeper) whose run function
# takes an argument instead of being a plain no-arg call — the executor
# built in a later stage needs to know this; router.py itself doesn't care.
REGISTRY = {
    "memory_search":       {"callable": memory_search.run,                "needs_cycle_num": False},
    "idea_planner":        {"callable": idea_planner.run,                 "needs_cycle_num": False},
    "prompt_writer":       {"callable": prompt_writer.run,                "needs_cycle_num": False},
    "code_writers":        {"callable": code_writers.run,                 "needs_cycle_num": False},
    "dependency_mapper":   {"callable": dependency_mapper.run,            "needs_cycle_num": False},
    "test_writer":         {"callable": test_writer.run,                  "needs_cycle_num": False},
    "reviewer":            {"callable": reviewer.run_reviewer,            "needs_cycle_num": False},
    "duplication_checker": {"callable": duplication_checker.run,          "needs_cycle_num": False},
    "fixer_pool":          {"callable": fixer_pool.run_fixer_pool,        "needs_cycle_num": False},
    "sandbox_tester":      {"callable": sandbox_tester.run_sandbox_tester,"needs_cycle_num": False},
    "structure_architect": {"callable": structure_architect.run_structure_architect, "needs_cycle_num": False},
    "security_scanner":    {"callable": security_scanner.run,             "needs_cycle_num": False},
    "security_aggregator": {"callable": security_aggregator.run,          "needs_cycle_num": False},
    "file_manager":        {"callable": file_manager.run_file_manager,    "needs_cycle_num": False},
    # Tier 2 only (Part 2.5's "debug"/"refactor" routes) -- plan-free
    # write-back using eo/code_loader.py's own recorded paths, since tier
    # 2 never runs structure_architect.py to produce a file_plan for
    # run_file_manager() above to interpret. See file_manager.py's
    # write_back_existing_app() docstring for the full reasoning.
    "file_manager_writeback": {"callable": file_manager.write_back_existing_app, "needs_cycle_num": False},
    # Tier 2 only (Part 2.5's "add_tests" route) -- writes test_writer.py's
    # generated test_code out to tests/test_<module>.py, stitched with the
    # module's own source the same way sandbox_tester.py already runs it.
    # Separate from file_manager_writeback above: add_tests never changes
    # a module's own source, it only adds new test files, so it needs its
    # own callable rather than reusing write_back_existing_app(). See
    # file_manager.py's write_back_test_code() docstring for the full
    # reasoning.
    "file_manager_test_writeback": {"callable": file_manager.write_back_test_code, "needs_cycle_num": False},
    "documentation_agent": {"callable": documentation_agent.run,          "needs_cycle_num": False},
    # Migration Part 27: changelog_writer, final_qa, and gatekeeper's
    # dedicated agent modules were retired -- all three were either pure
    # reasoning-only text generation (changelog_writer, final_qa) with
    # zero live callers (both confirmed unreachable except through the
    # dead classic tier-3 pipeline, see router.py's TIERS[3] comment) or,
    # for gatekeeper, actively superseded by eo/loop_controller.py's own
    # generic_run(role="gatekeeper", ...) call. Their role names remain
    # valid in eo/structure.py's STRUCTURE_TEMPLATES for the Panel to
    # hire, and (not being in REAL_ACTION_ROLES) now resolve straight to
    # "generic_worker" -- no dedicated module, no lost capability, one
    # fewer file to maintain.
    "report_writer":       {"callable": report_writer.run_report_writer,  "needs_cycle_num": False},
    # --- Tier 0 (Part 2.3) ---
    "responder":           {"callable": responder.run,                   "needs_cycle_num": False},
    # --- Tier 1 lean pipeline (Part 2.4) ---
    "prompt_writer_lean":     {"callable": prompt_writer_lean.run,          "needs_cycle_num": False},
    "code_writer_lean":       {"callable": code_writer_lean.run,            "needs_cycle_num": False},
    "reviewer_fixer_lean":    {"callable": reviewer_fixer_lean.run,         "needs_cycle_num": False},
    "sandbox_tester_lean":    {"callable": sandbox_tester.run_sandbox_tester_lean, "needs_cycle_num": False},
    # --- Migration Part 10 §2 — every reasoning-only role dispatches
    # through this one module, called with role=<the actual role name>.
    # resolve() below just needs the literal string "generic_worker" to
    # find the callable; eo/executor.py's dispatch is what supplies the
    # real role/input_keys/session_id/key_override arguments.
    "generic_worker":         {"callable": generic_worker.run,                "needs_cycle_num": False},
}
def resolve(agent_name: str):
    """Return the callable for `agent_name`, or raise KeyError with a
    clear message — never return None and let a caller silently no-op."""
    entry = REGISTRY.get(agent_name)
    if entry is None:
        raise KeyError(
            f"'{agent_name}' is not in eo.registry.REGISTRY. Either it's "
            f"misspelled, or it's a tier-0/1-only agent that hasn't been "
            f"built yet (see the module docstring)."
        )
    return entry["callable"]