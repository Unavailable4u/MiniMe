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
    changelog_writer,
    report_writer,
    final_qa,
    gatekeeper,
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
    "changelog_writer":    {"callable": changelog_writer.run,             "needs_cycle_num": False},
    "report_writer":       {"callable": report_writer.run_report_writer,  "needs_cycle_num": False},
    "final_qa":            {"callable": final_qa.run,                    "needs_cycle_num": False},
    "gatekeeper":          {"callable": gatekeeper.run_gatekeeper,        "needs_cycle_num": True},
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
# NEW — add below resolve(), don't touch REGISTRY or resolve() above

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
}


def get_role_prompt(role_name: str) -> str | None:
    """Returns the stored brief for this role, or None if it's never
    been written. Bootstraps from ROLE_PROMPTS_SEED on the very first
    call if the memory bus has nothing yet — after that, the bus is
    authoritative and the seed is never consulted again."""
    prompts = read(ROLE_PROMPTS_KEY, default=None)
    if prompts is None:
        prompts = dict(ROLE_PROMPTS_SEED)
        write(ROLE_PROMPTS_KEY, prompts)
    return prompts.get(role_name)


def add_role_prompt(role_name: str, brief: str) -> None:
    """Writes a newly-generated brief back into the persistent store.
    This is what makes the registry actually grow instead of writing
    the same role's brief on every single task that needs it."""
    prompts = read(ROLE_PROMPTS_KEY, default=dict(ROLE_PROMPTS_SEED))
    prompts[role_name] = brief
    write(ROLE_PROMPTS_KEY, prompts)


def list_known_roles() -> list:
    """Every role the system has ever written a brief for — useful for
    the frontend (a future 'known roles' panel) and for debugging."""
    prompts = read(ROLE_PROMPTS_KEY, default=dict(ROLE_PROMPTS_SEED))
    return sorted(prompts.keys())

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
}


def resolve_role(role_name: str) -> str:
    """Real-action roles resolve to their dedicated module name, exactly
    as before. Everything else resolves to the literal string
    'generic_worker' — execute_graph's dispatch (Part 10 §4) is what
    actually routes that to agents.generic_worker.run(role=role_name,
    ...)."""
    return REAL_ACTION_ROLES.get(role_name, "generic_worker")