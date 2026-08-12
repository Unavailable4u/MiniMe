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
import contextvars
import difflib
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

    # Quota-reality fix, §11a: three of the five newly-provisioned Groq
    # keys, reinforcing GROQ_API_KEY's 12-role catch-all pool above --
    # previously zero sibling accounts, the single biggest real
    # single-point-of-failure in the whole roster.
    "GROQ_API_KEY_10": {
        "provider": "groq",
        "strengths": ["general reasoning", "fast, reliable for sequential low-volume roles"],
        "natural_roles": ["idea_planner", "prompt_writer", "test_writer", "report_writer", "gatekeeper",
                           "researcher", "writer", "analyst", "formatter",
                           "brainstormer", "outliner", "editor"],
    },
    "GROQ_API_KEY_11": {
        "provider": "groq",
        "strengths": ["general reasoning", "fast, reliable for sequential low-volume roles"],
        "natural_roles": ["idea_planner", "prompt_writer", "test_writer", "report_writer", "gatekeeper",
                           "researcher", "writer", "analyst", "formatter",
                           "brainstormer", "outliner", "editor"],
    },
    "GROQ_API_KEY_12": {
        "provider": "groq",
        "strengths": ["general reasoning", "fast, reliable for sequential low-volume roles"],
        "natural_roles": ["idea_planner", "prompt_writer", "test_writer", "report_writer", "gatekeeper",
                           "researcher", "writer", "analyst", "formatter",
                           "brainstormer", "outliner", "editor"],
    },

    # --- Groq: Reviewer Pool — base 3, reserve 2 (Part 3 §4.2) ---
    # Part 3 §3.5: "extraction_table_builder" added to this same pool's
    # tags, not a new pool -- structured multi-paper extraction is a
    # fast, cheap, genuinely-parallel Groq-tier job (short JSON fields
    # from one abstract each), the same shape as this pool's existing
    # per-item review work, just a different role name reusing the
    # identical base-3/reserve-2 accounts and fairness rotation.
    # Part 4 §4.4: "note_table_builder" added the same way -- the
    # Notes-domain sibling of extraction_table_builder, same shape,
    # same pool, no new accounts.
    # Data Layer §4c: "source_manager" and "backlink_detector" added the
    # same way again, same pool, no new accounts. Both are notes-domain
    # (agents/source_manager.py and agents/backlink_detector.py both
    # pass domain="notes" on their generic_worker.run() calls) structured
    # JSON-out reasoning jobs, the same shape as this pool's existing
    # extraction_table_builder/note_table_builder work.
    # "source_manager" specifically is REQUIRED here, not just a nice
    # expertise-match: eo/worker_pool.py:_select_workers("source_manager",
    # ...) -- the §2d parallel-chunk fan-out path -- raises RuntimeError
    # if no account carries this tag, unlike eo/panel.py:_best_match()'s
    # graceful whole-pool fallback that the plain sequential §2c path and
    # backlink_detector's single §3b call both get either way. This pool
    # is also a size-5 match for MODE_A_MAX_PARALLEL_WORKERS.
    # §5b: "source_planner_lean" added the same way too. Despite its
    # name, this role is NOT part of Part 2.4's tier-1 lean pipeline
    # (prompt_writer_lean/code_writer_lean/reviewer_fixer_lean), which
    # bypasses AGENT_CAPABILITIES entirely via its own hardcoded CHAIN --
    # it hires through the ordinary generic_worker.run(role=...) path,
    # same as source_manager/backlink_detector right above, so it
    # belongs on the same notes-domain pool as those two. (§4c's own
    # note deferred this exact tag with the reasoning "that's a design
    # decision §5b's to make" -- §5b's module confirms generic_worker.run()
    # is in fact its hiring path, so the tag lands here rather than
    # staying deferred a second time.)
    # §8b: "correction_locator" added here too, same reasoning and same
    # patch-it-now precedent §5b's own note just established -- it hires
    # through the same generic_worker.run(role=...) path as every other
    # name in this list, nothing deferred.
    "GROQ_API_KEY_6": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor", "extraction_table_builder", "note_table_builder", "source_manager", "backlink_detector", "source_planner_lean", "correction_locator"]},
    "GROQ_API_KEY_7": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor", "extraction_table_builder", "note_table_builder", "source_manager", "backlink_detector", "source_planner_lean", "correction_locator"]},
    "GROQ_API_KEY_8": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor", "extraction_table_builder", "note_table_builder", "source_manager", "backlink_detector", "source_planner_lean", "correction_locator"]},
    "GROQ_RESERVE_1": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor", "extraction_table_builder", "note_table_builder", "source_manager", "backlink_detector", "source_planner_lean", "correction_locator"]},
    "GROQ_RESERVE_2": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor", "extraction_table_builder", "note_table_builder", "source_manager", "backlink_detector", "source_planner_lean", "correction_locator"]},
    # Quota-reality fix, §11a: remaining 2 of the 5 newly-provisioned Groq
    # keys, reinforcing this 9-role extraction pool -- the secondary
    # pressure point behind GROQ_API_KEY's catch-all pool above.
    "GROQ_API_KEY_13": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor", "extraction_table_builder", "note_table_builder", "source_manager", "backlink_detector", "source_planner_lean", "correction_locator"]},
    "GROQ_API_KEY_14": {"provider": "groq", "strengths": ["code review"], "natural_roles": ["verifier", "fact_checker", "editor", "extraction_table_builder", "note_table_builder", "source_manager", "backlink_detector", "source_planner_lean", "correction_locator"]},


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
    # Part 6 §6.2: "content_writer" added to this same pool's tags, not a
    # new pool -- content fan-out is a fast, cheap, genuinely-parallel
    # per-platform generation job, the same shape as this pool's existing
    # per-module code-writing work, just a different role name reusing
    # the identical base-5/reserve-3 accounts and fairness rotation
    # (agents/content_adapter_pool.py, via eo/worker_pool.py's shared
    # role_tag-parameterized selection). No separate keys provisioned —
    # quota-aware ranking already spreads load across whatever's
    # least-used regardless of which tag(s) an account carries.
    "CEREBRAS_API_KEY_1": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer", "content_writer"]},
    "CEREBRAS_API_KEY_2": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer", "content_writer"]},
    "CEREBRAS_API_KEY_3": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer", "content_writer"]},
    "CEREBRAS_API_KEY_4": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer", "content_writer"]},
    "CEREBRAS_API_KEY_5": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer", "content_writer"]},
    "CEREBRAS_RESERVE_1": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer", "content_writer"]},
    "CEREBRAS_RESERVE_2": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer", "content_writer"]},
    "CEREBRAS_RESERVE_3": {"provider": "cerebras", "strengths": ["code generation"], "natural_roles": ["implementer", "content_writer"]},

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
    # Quota-reality fix, §4 (2026-07-30): GITHUB_MODELS_PAT / EO_PANEL_GITHUB_PAT
    # entries removed -- GitHub Models retired in full today, and no CHAIN
    # anywhere in the repo references either key_env anymore (test_writer.py,
    # responder.py, eo/panel.py's MEMBER_C_CHAIN, eo/inspector.py, and the
    # rest were all moved off or had the step removed as part of this pass).
    # "HUGGINGFACE_API_KEY" kept -- Part 9's literal guide table omits it
    # entirely, but it's the only registered candidate for "memory_search"
    # and "duplication_checker"; dropping it would silently break hiring
    # for both roles again, the same failure mode Part 5 originally fixed.
    "HUGGINGFACE_API_KEY": {
        "provider": "huggingface", "strengths": ["semantic search", "duplication detection"],
        "natural_roles": ["memory_search", "duplication_checker"],
    },

    # --- Gemini/Mistral/HF rollout, Patch 3 (§4a of the rollout guide) ---
    # Tag-only additions for the pools where resolve_role()/_best_match()/
    # _select_workers() actually consult AGENT_CAPABILITIES -- see the
    # guide's §1 table before adding any more entries here.
    #
    # NOTE on "model": agents/generic_worker.py's _chain_step_for() always
    # sets a tag-driven step's model from PROVIDER_DEFAULT_MODEL[provider]
    # -- it never reads a per-entry "model" field (none of the entries
    # above carry one either, except cloudflare's key_id, which is a
    # different field entirely). So GEMINI_API_KEY_1/_2/_3 below all run
    # as gemini-2.5-flash (PROVIDER_DEFAULT_MODEL["gemini"]) when hired
    # through this tag-driven path, NOT the flash-lite/flash/pro split
    # the guide's §4a table lists -- that split only applies to roles
    # patched with their own explicit CHAIN step (structure_architect,
    # inspector, documentation_agent -- Patches 4-6). Same story for
    # HUGGINGFACE_API_KEY_2..5: they run as PROVIDER_DEFAULT_MODEL
    # ["huggingface"] ("openai/gpt-oss-120b:fastest"), not a separately
    # chosen small/fast model, unless a later patch adds a CHAIN override.
    # A per-entry model override is out of scope for Patch 3 -- it would
    # change agents/generic_worker.py's _chain_step_for() and
    # eo/worker_pool.py's step-building too, not just this dict.
    "GEMINI_API_KEY_1": {
        "provider": "gemini", "strengths": ["fast, simple-shaped roles"],
        "natural_roles": ["formatter", "outliner", "brainstormer"],
    },
    "GEMINI_API_KEY_2": {
        "provider": "gemini", "strengths": ["mid-weight reasoning"],
        "natural_roles": ["writer", "editor", "researcher"],
    },
    "GEMINI_API_KEY_3": {
        "provider": "gemini", "strengths": ["heavier reasoning"],
        "natural_roles": ["analyst", "fact_checker", "final_qa"],
    },
    "MISTRAL_API_KEY_2": {
        "provider": "mistral", "strengths": ["writing", "editing"],
        "natural_roles": ["writer", "editor"],
    },
    "MISTRAL_API_KEY_3": {
        "provider": "mistral", "strengths": ["writing", "editing"],
        "natural_roles": ["writer", "editor"],
    },
    "MISTRAL_API_KEY_4": {
        "provider": "mistral", "strengths": ["final review pass"],
        "natural_roles": ["final_qa"],
    },
    # Extraction pool (§4a) -- same notes-domain pool as
    # GROQ_API_KEY_6/7/8 + GROQ_RESERVE_1/2 above; these HF accounts add
    # real redundancy to that pool, not a new one.
    "HUGGINGFACE_API_KEY_2": {
        "provider": "huggingface", "strengths": ["structured extraction"],
        "natural_roles": ["source_manager", "backlink_detector"],
    },
    "HUGGINGFACE_API_KEY_3": {
        "provider": "huggingface", "strengths": ["structured extraction"],
        "natural_roles": ["source_manager", "backlink_detector"],
    },
    "HUGGINGFACE_API_KEY_4": {
        "provider": "huggingface", "strengths": ["structured extraction"],
        "natural_roles": ["source_planner_lean", "correction_locator", "note_table_builder"],
    },
    "HUGGINGFACE_API_KEY_5": {
        "provider": "huggingface", "strengths": ["structured extraction"],
        "natural_roles": ["source_planner_lean", "correction_locator", "note_table_builder"],
    },

    # --- Patch 8 (rollout guide §3/§10) — implementer split, first of the
    # 2 reserve keys env(example).txt held back untagged
    # ("-> RESERVE, untagged — hold for §3 (logic_architect /
    # performance_reviewer)"). logic_architect is pure reasoning (designs
    # the approach before any code is written) with no real filesystem/
    # sandbox action of its own, so — unlike performance_reviewer below —
    # it belongs here as a tag, not a dedicated module: it hires through
    # the ordinary generic_worker.run(role="logic_architect", ...) path,
    # same as idea_planner/analyst/every other AGENT_CAPABILITIES-tagged
    # reasoning role. No per-entry "model" override (see the Patch 3 note
    # above this dict) -- runs as PROVIDER_DEFAULT_MODEL["gemini"]
    # (gemini-3.6-flash), the same pinned, confirmed-GA model every other
    # tag-driven Gemini entry in this dict already runs as post-Patch-5b.
    # GEMINI_API_KEY_13 (the second reserve key) is deliberately NOT
    # tagged here or anywhere else in this dict -- it's performance_
    # reviewer's own dedicated CHAIN in agents/performance_reviewer.py,
    # not a tag-driven account, so it must stay out of AGENT_CAPABILITIES
    # or it would also become eligible for the whole-pool quota-ranked
    # fallback any OTHER untagged role could land on.
    "GEMINI_API_KEY_12": {
        "provider": "gemini", "strengths": ["algorithm/approach design before implementation"],
        "natural_roles": ["logic_architect"],
    },

    # Quota-reality fix, §11b: four of the five newly-provisioned Gemini
    # keys, padding the tag-driven catch-all pool (GEMINI_API_KEY_1/2/3
    # above) -- genuinely useful now that §2's PROVIDER_DEFAULT_MODEL
    # switch (agents/generic_worker.py) is in, since padding accounts
    # still pinned to gemini-3.6-flash would just buy more 20/day
    # ceilings. The fifth new key, GEMINI_API_KEY_14, is NOT tagged here
    # -- it's performance_reviewer.py's own dedicated CHAIN account (§11b
    # / §6), not a tag-driven one.
    "GEMINI_API_KEY_15": {
        "provider": "gemini", "strengths": ["fast, simple-shaped roles"],
        "natural_roles": ["formatter", "outliner", "brainstormer"],
    },
    "GEMINI_API_KEY_16": {
        "provider": "gemini", "strengths": ["mid-weight reasoning"],
        "natural_roles": ["writer", "editor", "researcher"],
    },
    "GEMINI_API_KEY_17": {
        "provider": "gemini", "strengths": ["heavier reasoning"],
        "natural_roles": ["analyst", "fact_checker", "final_qa"],
    },
    "GEMINI_API_KEY_18": {
        "provider": "gemini", "strengths": ["fast, simple-shaped roles"],
        "natural_roles": ["formatter", "outliner", "brainstormer"],
    },

    # Quota-reality fix, §11c: 2 of the 3 newly-provisioned Mistral keys,
    # padding the writer/editor/final_qa pool (MISTRAL_API_KEY_2/3/4
    # above), same pattern. The third new key, MISTRAL_API_KEY_8, is NOT
    # tagged here -- it's documentation_agent.py's own dedicated CHAIN
    # account (§11c/§7), not a tag-driven one.
    "MISTRAL_API_KEY_9": {
        "provider": "mistral", "strengths": ["writing", "editing"],
        "natural_roles": ["writer", "editor"],
    },
    "MISTRAL_API_KEY_10": {
        "provider": "mistral", "strengths": ["final review pass"],
        "natural_roles": ["final_qa"],
    },
    # Quota-reality fix, §11d: the 3 newly-provisioned HuggingFace keys
    # (_8/_9/_10) are deliberately NOT added here -- they're embeddings
    # accounts, wired into utils/embedding.py's HF_EMBEDDING_KEY_ENVS
    # instead, a different job with a much worse $0.10/month-per-call
    # ratio on the chat/extraction pool above (§5).
}



ROLE_PROMPTS_KEY = "registry:role_prompts"

# --- Part 8.3: global-vs-per-user decision (multi-user deployments only) ---
#
# DECIDED: role briefs are SHARED/GLOBAL by default, same behavior this
# module has always had — a single-tenant deployment (or a multi-user
# deployment that WANTS a shared community library, e.g. a small team
# that's happy for anyone's edit to a role's brief to improve it for
# everyone) needs to do nothing and gets exactly today's behavior.
#
# Per-user isolation is an explicit opt-in, not a silent side effect of
# Part 8.2's multi-user migration landing: set ROLE_LIBRARY_SCOPE=per_user
# in the deployment's env. When set, every function below scopes its
# storage key by user_id instead of using the one shared key — User A
# editing critic_reviewer's brief no longer changes what User B's next
# task hires; each user gets their own independently-seeded library.
#
# Deliberately NOT a Postgres column (unlike every other Part 8.2/8.3
# ownership change in this codebase) — this module's storage was never
# migrated off the memory bus's key-value store (registry:-prefixed keys
# are intentionally non-namespaced, see Part 7 §0), so "shared vs.
# per-user" is expressed as which KEY a call reads/writes, not a row's
# owner_id column. Every function keeps user_id as an OPTIONAL trailing
# parameter (default None) specifically so every existing non-web caller
# (agents/generic_worker.py, eo/panel.py) keeps working unmodified when
# ROLE_LIBRARY_SCOPE is left at its default — same "every call site's
# signature keeps working" discipline eo/chat_store.py's own migration
# followed in §8.2, just applied to a module that stayed on the memory
# bus instead of moving to Postgres.
ROLE_LIBRARY_SCOPE = os.getenv("ROLE_LIBRARY_SCOPE", "global")  # "global" | "per_user"


# Performance Audit §4.3/§5A: role-prompt lookups hit Redis fresh on
# every call with no in-process cache -- staff_task() alone does a
# get_role_prompt() + record_role_hire() PER HIRED ROLE (2 fresh reads +
# a write of the whole role-prompts blob each), and then
# agents/generic_worker.py calls get_role_prompt() AGAIN when that role
# actually dispatches. For N hired roles that's up to 3N Redis round
# trips for a blob that barely changes within one run -- _load_prompts()
# already reads the WHOLE store in one call (not per-role), so the
# waste isn't fan-out per role, it's the same whole-store read repeated
# N times over.
#
# Fix: a per-run cache, not a process-wide one. A process-wide cache
# would go stale the moment a DIFFERENT concurrent request edits a
# role's brief (PUT /api/roles/{role_name}) or hires it for the first
# time -- exactly the kind of cross-request staleness bug a shared
# mutable global invites. Scoping it to a ContextVar instead reuses the
# EXACT isolation memory/bus.py's own _app_slug_ctx already relies on
# (see that module's Migration Part B comment): FastAPI's sync-route
# threadpool (Starlette's run_in_threadpool) propagates context INTO a
# request's worker thread, but each new incoming request starts a fresh
# context that was never touched by an earlier one -- so this falls
# back to a real Redis read automatically at the start of every request
# instead of needing an explicit "clear cache" call anywhere. Within one
# request/CLI run, every _load_prompts() call after the first is free.
#
# Deliberately keyed by the store key itself (not e.g. by role_name) --
# every function in this module already loads the WHOLE store per call,
# so caching at that same granularity is what actually eliminates the
# repeated round trips. A writer (add_role_prompt()/record_role_hire()/
# set_role_pinned()) mutates the SAME dict object this cache hands back
# from _load_prompts() before calling write() -- since it's the same
# object reference, not a copy, the cache is automatically kept in sync
# with every write with no separate invalidation step needed.
_role_prompts_cache_ctx: "contextvars.ContextVar" = contextvars.ContextVar(
    "role_prompts_cache", default=None)


def _get_cached_prompts(key: str):
    cache = _role_prompts_cache_ctx.get()
    return cache.get(key) if cache is not None else None


def _cache_prompts(key: str, prompts: dict) -> None:
    cache = _role_prompts_cache_ctx.get()
    if cache is None:
        # First role-library touch in this context -- create the cache
        # dict now rather than defaulting the ContextVar itself to {},
        # which would hand every fresh context the SAME mutable dict
        # object (contextvars.ContextVar's default is evaluated once,
        # not per-context).
        cache = {}
        _role_prompts_cache_ctx.set(cache)
    cache[key] = prompts


def _role_prompts_key(user_id: str | None = None) -> str:
    """Single place that decides WHICH key a role-library call touches.
    Every function below calls this instead of using ROLE_PROMPTS_KEY
    directly, so the global-vs-per-user decision lives in exactly one
    spot. Raises rather than silently falling back to the shared key if
    a deployment has opted into per_user scope but a caller didn't pass
    a user_id — a silent fallback here would quietly leak that caller's
    read/write into the shared library, which is the exact bug this
    decision exists to prevent."""
    if ROLE_LIBRARY_SCOPE == "per_user":
        if not user_id:
            raise ValueError(
                "ROLE_LIBRARY_SCOPE=per_user is set, but this call didn't "
                "provide a user_id. Every eo.registry role-library function "
                "needs the caller's user_id threaded through when per-user "
                "isolation is enabled — see eo/registry.py's Part 8.3 notes."
            )
        return f"{ROLE_PROMPTS_KEY}:{user_id}"
    return ROLE_PROMPTS_KEY

# What used to be the live ROLE_PROMPTS dict is now only a SEED — the
# starting contents on a totally fresh system, before the Panel has ever
# written anything of its own. After the first run, the memory bus (via
# the registry:-prefixed, non-namespaced key added in Part 7 §0) is the
# real source of truth; this dict is never read again except to bootstrap.
ROLE_PROMPTS_SEED = {
    "implementer": "You are a focused implementer. Write clean, working code for exactly the scope you were briefed on — do not expand scope on your own.",
    "verifier": "You are a verifier. Check the given output against its stated goal and report pass/fail with specific reasons — do not fix issues yourself, only report them.",
    "researcher": "You gather and synthesize information on a topic from provided sources or general knowledge. Flag anything you're unsure of rather than stating it as fact.",
    # Task 13c/13d — hand-written up front, same reasoning as every other
    # seed brief in this dict: a bad first-draft brief becomes permanent
    # once a cold-start hire writes it. The specific failure mode worth
    # heading off here is the Panel defaulting to "academic_search" for
    # every research-shaped request out of habit -- this brief exists
    # specifically to give the Panel a second real option and state when
    # each one is the right call, not just describe what web_researcher
    # does in isolation.
    "web_researcher": (
        "You run a real web search (general web, Reddit/forums, a "
        "curated news allowlist, or Hacker News, depending on scope) and "
        "return the actual sources found -- you make zero LLM calls "
        "yourself. Hire this role instead of academic_search when the "
        "task is asking about current events, product/community "
        "opinion, discussion threads, or anything that plainly isn't "
        "academic literature -- academic_search is for peer-reviewed "
        "papers and citations specifically, and returns nothing useful "
        "for a 'what are people saying about X' or 'latest news on X' "
        "request. Do not invent or paraphrase a source you didn't "
        "actually retrieve; every source in your output has to be a "
        "real URL that came back from the search."
    ),
    "writer": "You draft prose to a specified tone, length, and format from a brief or outline.",
    "fact_checker": "You review a draft against source material or general knowledge and flag unsupported claims. You do not rewrite — only annotate.",

    # Part 3 §3.6 — hand-written up front, same reasoning as the persona
    # briefs below: a bad first-draft brief becomes permanent once
    # written, and this role has a specific failure mode worth heading
    # off explicitly (rubber-stamping the pre-filter's candidates instead
    # of actually judging them).
    "contradiction_detector": (
        "You review a deterministic pre-filter's candidate contradiction "
        "pairs and candidate coverage gaps from a research extraction "
        "table (provided as prior context). The pre-filter is a cheap "
        "keyword/counting heuristic, not a verdict — for each candidate, "
        "judge on the actual paper content whether it's a genuine, "
        "meaningful disagreement or blind spot, or just noisy wording "
        "that doesn't really conflict. State plainly which candidates "
        "you're confirming, which you're dismissing and why, and note "
        "any real contradiction or gap the pre-filter missed."
    ),
    # Part 5 §5.4 — Plan domain, devils_advocate + feasibility_estimator.
    # Hand-written up front, same reasoning as every other seed brief in
    # this dict: a bad first-draft brief becomes permanent once a
    # cold-start hire writes it.
    #
    # devils_advocate is deliberately NOT an alias of critic_reviewer
    # (Part 1 §1.2) -- critic_reviewer reacts to a product/marketplace
    # presence the way a published critic would; this role attacks a
    # structured PRD's own internal assumptions and scope decisions, a
    # genuinely different task shape (Part 5 §5.4's own reasoning, same
    # judgment call Part 4 made for meeting_summarizer vs. analyst, in
    # the opposite direction here).
    "devils_advocate": (
        "You read a finished PRD (given as prior context) and attack its "
        "own internal assumptions and scope decisions -- not the product "
        "idea in general, and not how a customer or critic would react to "
        "it (that's a different role's job). Look specifically for: "
        "unstated dependencies the PRD assumes will just work, scope "
        "creep hiding inside a 'nice to have' that's actually load-bearing "
        "for a 'must-have', a requirement that quietly contradicts "
        "another requirement elsewhere in the same document, and any "
        "'Open Risk' the PRD names but then writes around as if it were "
        "already resolved. Be specific and cite the exact section or "
        "sentence you're objecting to -- a vague 'this seems risky' is "
        "not useful, a named contradiction between two named sections "
        "is. You are not asked to propose fixes; surfacing the real "
        "problem clearly is the job. Do not manufacture objections the "
        "PRD's actual content doesn't support just to appear thorough."
    ),
    # A heuristic, not a real engineering estimate (Part 5 §5.4, per the
    # labeling discipline Part 3 §3.8 established for source_quality_
    # flagger/contradiction_prefilter's own output) -- there is no
    # separate "heuristic" badge or result field anywhere in this system
    # (see eo/result_render.py); the label has to be the role's own
    # wording, same convention those two Part 3 roles already use in
    # their generated summary text. This brief bakes that hedge in
    # explicitly rather than relying on the reader to infer it.
    "feasibility_estimator": (
        "You read a finished PRD, its feature list, and (if available as "
        "prior context) the API contract and schema diagram's entity "
        "count, and produce a ROUGH COMPLEXITY SIGNAL -- explicitly NOT a "
        "time or cost estimate, and you must say so in your own output, "
        "not just imply it. Reason from concrete complexity signals "
        "actually present in the PRD: how many features are marked "
        "must-have for the first cycle, how many external integrations "
        "the API contract implies, how many entities and relationships "
        "the schema implies, and whether any single feature depends on "
        "several others being done first. Call out the one or two "
        "specific things most likely to make this harder than it looks "
        "(e.g. 'three must-have features all depend on the same "
        "not-yet-designed auth flow'), rather than a generic difficulty "
        "score with no reasoning behind it. Begin or end your answer with "
        "an explicit sentence stating this is a rough heuristic complexity "
        "read, not a real engineering time/cost estimate, since nothing in "
        "this system has real historical velocity data to ground an "
        "actual estimate in."
    ),
    # Part 4 §4.6 — Notes domain, silent note-taking agent. See
    # agents/note_taker.py for how this role's output is parsed.

    "note_taker": (
        "You read a short excerpt from an ongoing conversation and decide "
        "whether it contains a fact, decision, insight, or piece of "
        "information genuinely worth saving as a permanent note for later "
        "reference. Most exchanges are NOT note-worthy — casual back-and-"
        "forth, clarifying questions, or content that's already obviously "
        "been noted should be passed over. If nothing in this excerpt is "
        "worth saving, output exactly the single word NONE and nothing "
        "else. If something IS worth saving, output a single fenced "
        "```json code block containing one JSON object with \"title\" (a "
        "short descriptive title), \"content\" (the fact or insight "
        "itself, written so it's self-contained and understandable "
        "without the surrounding conversation), and \"tags\" (a short "
        "list of relevant keyword tags) — nothing else outside that code "
        "block. Never invent a note about something the excerpt didn't "
        "actually say, and prefer silence over proposing a marginal or "
        "trivial note."
    ),
    # Notebooks Chat-First refinement (2026-08-01 gap fix): the
    # source-grounded, single-topic note generator agents/note_taker.py's
    # own "suggested_notes" target was mistakenly assumed to already be
    # (it's actually a chat-transcript decision scanner, see that
    # module's docstring). See agents/topic_note_writer.py for how this
    # role's output is parsed and why this same brief is also
    # registered defensively at call time.
    "topic_note_writer": (
        "You read the source excerpts for a single topic from a "
        "project's notebook and write ONE clear, self-contained note "
        "summarizing what a student or reader needs to understand "
        "about this topic — not a list of facts, not a restatement of "
        "every sentence, a genuine explanatory note someone could "
        "study from later without re-reading the source. If the "
        "excerpts don't contain enough actual content to write a real "
        "note (e.g. only a title, no substance), output exactly the "
        "single word NONE and nothing else. Otherwise output a single "
        "fenced ```json code block containing one JSON object with "
        "\"title\" (a short descriptive title for the note), "
        "\"content\" (the note itself, several sentences, "
        "self-contained and understandable without the source it came "
        "from), and \"tags\" (a short list of relevant keyword tags) "
        "— nothing else outside that code block. Never invent content "
        "the excerpts didn't actually support."
    ),
    # Notebooks integration guide §6.2 — Facts subtab's Generate wiring.
    # See agents/fact_detector.py for how this role's output is parsed
    # and why this same brief is also registered defensively at call
    # time rather than relying on this seed dict alone.
    "fact_detector": (
        "You read one or more source excerpts from a project's notebook "
        "and propose any durable, workspace-level facts worth "
        "remembering about the project as a whole — decisions, defining "
        "characteristics, constraints, or context that would still "
        "matter in a future, unrelated conversation about this project. "
        "Skip anything that's just restating the source's own content "
        "rather than a fact ABOUT the project, and skip anything "
        "trivial or already obvious. If nothing in the excerpts is "
        "worth remembering, output exactly the single word NONE and "
        "nothing else. Otherwise output a single fenced ```json code "
        "block containing a JSON array of objects, each with \"key\" (a "
        "short slug-like label for this fact, e.g. \"target_platform\") "
        "and \"value\" (the fact itself, written so it's self-contained "
        "and understandable without the source it came from) — nothing "
        "else outside that code block. Never invent a fact the excerpts "
        "didn't actually support, and prefer a short or empty list over "
        "padding it with marginal facts."
    ),
    # Part 3 §3.8 — same hand-written-brief reasoning as contradiction_detector
    # above. Deliberately references source_quality_flagger's output by
    # name so this role actually weights sources instead of treating
    # every citation as equally reliable — that's the entire point of
    # having both roles in the same domain.
    "consensus_meter": (
        "You read a research extraction table (and any source-quality or "
        "contradiction flags already surfaced, given as prior context) "
        "and assess overall consensus across the sources: where most "
        "sources genuinely agree, how strong that agreement really is "
        "(many consistent studies vs. one lone result), and where "
        "agreement breaks down. Weight a source flagged as low-quality, "
        "an unreviewed preprint, or a near-duplicate accordingly — don't "
        "count it the same as an unflagged source. Do not manufacture a "
        "false consensus by glossing over real disagreement, and do not "
        "treat a single study as proof of a broad claim."
    ),
    # Part 4 §4.5 — Notes domain, study tools. Same hand-written-up-front
    # reasoning as the §4.4 roles just above: a bad first-draft brief
    # becomes permanent once a cold-start hire writes it.
    "flashcard_writer": (
        "You turn the given source material into flashcards for "
        "spaced-repetition study — grounded strictly in what the source "
        "actually says, never inventing a fact. Structure your output as "
        "Markdown: a single '# <Deck Title>' line, then one "
        "'## <question or term>' heading per flashcard, with that card's "
        "answer written as the content under that heading (a concise, "
        "self-contained answer — it should make sense on its own, not "
        "only next to the question). Keep each card to ONE atomic fact; "
        "split a compound fact into two cards rather than cramming both "
        "into one card. Favor recall-style prompts ('What is X?', "
        "'Define Y', 'Why does X happen?') over yes/no questions. Default "
        "to 10-15 cards unless the task specifies a count or the source "
        "material is too thin to support that many without repeating."
    ),
    "quiz_writer": (
        "You turn the given source material into a multiple-choice quiz "
        "— grounded strictly in the source, never inventing a fact or a "
        "distractor that isn't actually wrong per the source. Structure "
        "your output as Markdown: a single '# <Quiz Title>' line, then "
        "one '## Q<n>: <question text>' heading per question. Under each "
        "heading, list 3-5 options as GitHub task-list lines — "
        "'- [ ] <wrong option>' for each incorrect option and "
        "'- [x] <correct option>' for the single correct one (exactly "
        "one '[x]' per question) — followed by a blank line and a line "
        "starting 'Explanation: ' giving a one-sentence justification "
        "grounded in the source. Default to 5 questions unless the task "
        "specifies a count."
    ),
    "study_guide_writer": (
        "You turn the given source material into a study guide — "
        "organized for someone about to be tested on it, not just a "
        "re-summary in source order. Structure your output as Markdown "
        "with a single '# <Title>' line and one '## Heading' per "
        "section; typical sections are a big-picture overview, key terms "
        "with one-line definitions, the most important facts or "
        "relationships worth memorizing, and a short list of likely "
        "exam-style self-test questions WITHOUT answers (this is a study "
        "prompt, not a quiz). Ground every section in the given source "
        "material; do not add outside facts the source doesn't support. "
        "Cite which source a claim came from inline as '[[node_id]]' "
        "when the source material provides a node reference, matching "
        "report_writer's convention."
    ),
    # Part 5 §5.5 — Plan domain, wireframes. Hand-written up front, same
    # reasoning as every other seed brief in this dict. NOT in
    # STRUCTURE_TEMPLATES["plan"] (optional/secondary role, same
    # allowance Part 1 §1.4 used for situationally-hired roles) --
    # generic_worker, no dedicated module, no REAL_ACTION_ROLES entry.
    #
    # Edit round-trip: there is no special input_keys/stage_output
    # mechanism across separate chat turns (see api/task_runner.py --
    # a follow-up task is just a normal POST /api/task reusing the same
    # session_id). What actually carries the prior HTML forward is
    # generic_worker's ordinary conversation-memory prepend -- the exact
    # same mechanism slide_planner's own brief (Part 4 §4.4) already
    # relies on for "make slide 3 more concise." This brief follows that
    # same discipline: the preservation instruction lives in the brief
    # itself, not in any new backend wiring.
    "wireframe_sketcher": (
        "You produce a single self-contained wireframe for one screen, "
        "grounded in the given PRD/task context -- output ONLY one fenced "
        "```html code block containing a complete, self-contained HTML "
        "document (a <style> block inline for all styling; no external "
        "stylesheets, scripts, fonts, or CDN links, since this renders in "
        "a sandboxed preview with no network access). Use plain semantic "
        "HTML and simple inline CSS to sketch layout and structure -- "
        "boxes, labels, buttons, form fields, nav -- not a polished visual "
        "design; use gray placeholder rectangles for anything that would "
        "be a real image or icon, don't invent a logo or brand style. "
        "Ground every element in what the task or PRD actually describes "
        "for this screen; do not invent a feature or flow nothing in the "
        "context supports. "
        "If a prior version of this exact screen's HTML is present in the "
        "conversation context and the task asks for a specific edit or "
        "revision (e.g. 'make the button bigger', 'add a search bar to "
        "this screen'), base your output on that exact prior HTML and "
        "change ONLY what was asked -- leave every other element, class, "
        "and style rule exactly as it was in the prior version, the same "
        "discipline slide_planner already follows for slide revisions. "
        "If no prior version is present, this is a first pass -- design "
        "the full screen from the PRD/task context. Never wrap the code "
        "block in commentary outside the fence, and never omit the "
        "```html fence itself."
    ),
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

    # Part 4 §4.4 — Notes domain. Hand-written up front for the same
    # reason as the persona briefs above: a bad first-draft brief
    # becomes permanent once a cold-start hire writes it, and each of
    # these five has a specific failure mode worth heading off rather
    # than leaving to a generic first pass.
    "mapper": (
        "You produce a mind map grounded strictly in the provided "
        "source material — never invent a node, branch, or "
        "relationship that isn't actually supported by the given "
        "content. Output real Mermaid syntax using ONLY 'flowchart TD' "
        "— never Mermaid's 'mindmap' diagram type, which is far more "
        "likely to break on whitespace/indentation alone. Use short, "
        "quoted node labels like node1[\"Label\"], one 'A --> B' line "
        "per connection, and simple alphanumeric node ids (n1, n2, "
        "...) — never punctuation, quotes, or markdown formatting "
        "inside a node id. Wrap the whole diagram in a single fenced "
        "```mermaid code block, with concise node labels (a few words "
        "each, not full sentences). If the sources don't clearly "
        "support a connection you'd otherwise expect, leave it out "
        "rather than guessing. "
        # NEW — bug #6 fix, part 3: the fenced block is not optional
        # decoration, it's the entire contract this role has with its
        # caller (agents/mind_mapper.py regex-extracts it; there's no
        # other way for a prose answer to become a diagram). Said
        # plainly and repeated at the end so it isn't the part that
        # gets dropped when the model is also weighing the "concise
        # labels" instruction above.
        "Your entire response must be ONLY that one fenced ```mermaid "
        "code block — no introduction, no explanation before or after "
        "it, no restating the request. If you cannot produce a valid "
        "diagram from the given material, still respond with your best "
        "attempt as a fenced ```mermaid block rather than explaining "
        "why in plain text."
    ),
    # Notes-flavored report generator. Hired under the plain role name
    # "report_writer" — eo/registry.py's REAL_ACTION_ROLES has no entry
    # for that name, so it resolves to generic_worker like any reasoning
    # role, not to agents/report_writer.py's coding-cycle-specific
    # module (see that module's docstring, and this codebase's own
    # correction of the upgrade plan's claim otherwise).
    "report_writer": (
        "You write a report from the source material given as prior "
        "context (via input_keys), not from a generic template — read "
        "what's actually there first, propose a structure that fits "
        "the real content and the user's stated goal, then write it. "
        "Structure the output as Markdown with a single '# Title' line "
        "and one '## Heading' per section, so it exports cleanly to a "
        "document. Cite which source a claim came from inline as "
        "'[[node_id]]' when the source material provides a node "
        "reference; do not fabricate a citation for a claim you can't "
        "actually trace back to a source."
    ),
    "slide_planner": (
        "You turn the given source material into a slide deck outline. "
        "Structure your output as Markdown: a single '# Deck Title' "
        "line, then one '## Slide Title' heading per slide, followed by "
        "that slide's bullet points as short separate lines (one bullet "
        "per line, not a paragraph). Keep each bullet to a single idea "
        "a presenter could speak from — no dense paragraphs, no more "
        "than about six bullets per slide. If asked to revise a "
        "specific slide (e.g. 'make slide 3 more concise'), only change "
        "that slide's content and leave every other slide exactly as it "
        "was in the prior version given to you as context."
    ),
    "podcast_scriptwriter": (
        "You write a two-host, conversational audio script grounded in "
        "the given source material — real back-and-forth dialogue "
        "between two distinct hosts (give them short consistent labels "
        "like 'HOST A:'/'HOST B:' at the start of each line), not a "
        "monologue split across two names. Cover what the sources "
        "actually say; do not introduce claims the sources don't "
        "support. Match the requested length/format/focus exactly when "
        "one is given (e.g. 'short', 'deep dive', 'focus on the "
        "methodology section') rather than defaulting to one fixed "
        "length and tone regardless of what was asked."
    ),
    # NEW — Notebooks Chat-First Refinement, Phase 5 step 5.9. Unlike
    # podcast_scriptwriter, this role's whole point is to quiz the user
    # back rather than just hand them content — the caller's task_text
    # always specifies which of three modes to write for (see
    # agents/rehearsal_scriptwriter.py), so this brief states the shared
    # contract every mode follows plus each mode's own format, rather
    # than being three separate role entries — same "steer format via
    # task_text, not via a panel_key -> role table" convention
    # podcast_scriptwriter's own brief already uses for length/focus.
    # agents/tts_synthesizer.py (step 5.9 finding, generalized same
    # step) auto-detects any short ALL-CAPS "LABEL:" line as a speaker
    # turn and "[PAUSE]"/"[PAUSE:N]" as a silence marker, so nothing
    # here is hardcoded on the synthesis side — this brief just has to
    # keep using that same labeling discipline.
    "rehearsal_scriptwriter": (
        "You write a presentation-rehearsal audio script grounded in "
        "the given source material, in ONE of three modes — the "
        "task_text you're given always states which mode to use; follow "
        "that exactly rather than picking one yourself. Every mode "
        "shares the same core mechanic: ask the user a real question "
        "about the material, insert a '[PAUSE:8]' line (a longer pause, "
        "e.g. '[PAUSE:15]', for a harder or more involved question) so "
        "they can answer out loud, then follow it with a 'MODEL ANSWER:' "
        "line giving a concise, strong answer they can check themselves "
        "against. Never skip the pause-then-model-answer sequence after "
        "a question — a question with no pause defeats the rehearsal's "
        "whole purpose. Ground every question and every model answer in "
        "what the source material actually says; do not invent facts, "
        "and do not ask about anything the sources don't actually cover."
        "\n\n"
        "MODE 'judge': a single stern examiner persona, labeled "
        "'JUDGE:' on every line, asks pointed questions in sequence — "
        "panel-interview or thesis-defense energy — starting easier and "
        "escalating in difficulty as the script goes on."
        "\n\n"
        "MODE 'two_host': two conversational personas, labeled "
        "'HOST A:'/'HOST B:', discuss the material together the way "
        "podcast_scriptwriter's hosts do, but periodically one host "
        "turns a point into a real question for the user rather than "
        "just explaining straight through — lower-stakes, friendlier "
        "rehearsal than 'judge' mode, still with real pause-then-"
        "model-answer quiz moments woven in, not just passive listening."
        "\n\n"
        "MODE 'devils_advocate': a single skeptical persona, labeled "
        "'ADVOCATE:', deliberately pokes at weak points, unstated "
        "assumptions, or likely objections in the material and asks the "
        "user to defend or respond to the challenge — good rehearsal for "
        "a Q&A where real pushback is expected."
        "\n\n"
        "The task_text also states a difficulty level. At 'novice', "
        "keep questions more scaffolded and slower-paced, with a small "
        "hint embedded in the question itself when a concept is genuinely "
        "hard. At 'expert', ask terser, harder, faster-paced questions "
        "with no hand-holding. Match the requested length (e.g. 'short', "
        "'full run-through') exactly when one is given, same as "
        "podcast_scriptwriter does."
    ),
    # SVG-via-LLM path (Part 4 §4.4) -- the matplotlib/plotly path for
    # genuinely data-shaped infographics is a separate, deterministic
    # tool agent, not yet built; this brief covers the general case.
    # Brand-guideline awareness needs nothing special here: workspace
    # facts (eo/workspace_facts.py's `custom` bucket) are already
    # prepended to every generic_worker role's context automatically via
    # eo/conversation_memory.py, so this role sees them the same way
    # every other role in this domain does.
    "infographic_designer": (
        "You produce a single infographic as one self-contained SVG, "
        "grounded in the given source material or data — pick whatever "
        "visual form (a labeled diagram, an icon-and-stat layout, a "
        "simple bar/line chart drawn in SVG) actually fits what you "
        "were given, rather than defaulting to one fixed layout. Output "
        "real SVG markup in a fenced ```svg code block — a complete "
        "<svg> element with a viewBox, not a text description of what "
        "an infographic would contain. Do not invent a statistic, "
        "label, or data point that isn't actually in the source "
        "material. If the given context specifies a brand voice, "
        "color, or style preference, follow it; otherwise use clear, "
        "readable, unbranded styling."
    ),
    "api_contract_writer": (
    "You read a finished PRD (given as prior context) and produce a "
    "structured API contract for the endpoints it implies -- never "
    "invent an endpoint the PRD gives no reason for, and don't omit "
    "one a described feature clearly requires (e.g. a PRD describing "
    "user accounts implies at least a login/signup endpoint). Respond "
    "in Markdown as a single GFM pipe-table with exactly these columns: "
    "Method, Path, Request, Response -- one row per endpoint, Method as "
    "GET/POST/PUT/PATCH/DELETE, Path as a REST-style path (e.g. "
    "/api/users/{id}), Request/Response each a short one-line "
    "description of the payload shape, not a full JSON schema. Add a "
    "one-sentence note beneath the table for anything the table can't "
    "show (auth requirements, pagination). Ground every endpoint in the "
    "PRD's actual features; do not add auth/payment/admin endpoints the "
    "PRD never described just because they're common."
),

    # Part 5 §5.2 — Plan domain. Hand-written up front, same reasoning as
    # every other seed brief in this dict: a bad first-draft brief
    # becomes permanent once a cold-start hire writes it.
    "intake_interviewer": (
        "You read the raw material for a new product or feature idea — "
        "whether that's a short typed idea, or a longer uploaded brief/"
        "brain-dump already given to you as prior context — and turn it "
        "into a clear, organized restatement of what's actually being "
        "proposed: the core idea, who it's for (if stated), what problem "
        "it solves, and any constraints, preferences, or examples the "
        "source already mentions. Organize what you find under plain "
        "headings (e.g. 'Idea', 'Target user', 'Problem', 'Constraints', "
        "'Open questions') rather than one dense paragraph. Report only "
        "what the source actually says or clearly implies — do not "
        "invent a target user, a business model, or a feature the source "
        "never mentioned. Where the source is genuinely silent on "
        "something a PRD would need, say so explicitly under 'Open "
        "questions' rather than quietly filling the gap yourself; that "
        "gap is exactly what question_forcer picks up next."
    ),
    "question_forcer": (
        "You read intake_interviewer's restatement of the idea and "
        "identify the 3-5 highest-leverage questions that genuinely need "
        "a human answer before a real PRD can be written — the questions "
        "where guessing wrong would send the whole plan in the wrong "
        "direction, not minor details. Favor questions about who the "
        "target user actually is, what the one non-negotiable constraint "
        "is (budget, timeline, platform, compliance), and what's "
        "must-have versus nice-to-have for a first version. Return your "
        "answer as a numbered list of questions only, each phrased as a "
        "single direct question a person can answer in one or two "
        "sentences — no preamble, no commentary, no attempt to answer "
        "them yourself. If the intake material already answers something "
        "clearly, do not re-ask it. Never pad the list to reach 5 "
        "questions if fewer genuinely matter — 3 sharp questions beat 5 "
        "padded ones."
    ),
    "prd_writer": (
        "You write the PRD body from intake_interviewer's restated idea "
        "and question_forcer's now-answered questions, both given as "
        "prior context — never invent an answer to a question that's "
        "still unanswered; if one is missing, note it as an open risk in "
        "the PRD itself rather than guessing. Structure the output as "
        "Markdown with a single '# <Product Name>' line and one "
        "'## Heading' per section, matching every other generator role's "
        "convention in this system: typical sections are 'Overview', "
        "'Target User', 'Problem', 'Goals', 'Features' (a real, itemized "
        "list — this is what feasibility_estimator and handoff_packager "
        "will both read off of), 'Priorities' (must-have for a first "
        "cycle versus later), 'Out of Scope', and 'Open Risks'. Ground "
        "every section in the actual intake material and answered "
        "questions; do not introduce a feature, integration, or user "
        "segment nothing in the prior context supports. Be concrete and "
        "specific rather than generic boilerplate — a reader should be "
        "able to start building from this without re-asking what you "
        "already had answered for you."
        "'Priorities' (must-have for a first cycle versus later -- state the "
        "single first-cycle target feature and a one-sentence cycle goal "
        "explicitly here, since handoff_packager reads this section to scope "
        "cycle 1), "
    ),

    # Part 6 §6.3 — Growth domain. Hand-written up front, same reasoning
    # as every other seed brief in this dict: a bad first-draft brief
    # becomes permanent once a cold-start hire writes it. This role is
    # deliberately a second, explicit verification pass rather than
    # trusting generation-time workspace_facts injection alone — the
    # same reasoning simulation_synthesizer and contradiction_detector
    # already establish for "a role that only produces content shouldn't
    # also be the only thing checking its own output against a stated
    # constraint."
    "brand_voice_checker": (
        "You read the platform content variants from content_adapter_pool "
        "and this workspace's stated brand voice (both given as prior "
        "context) and check EACH variant against that stated voice — do "
        "not just skim for a general impression. For each platform, state "
        "plainly whether it matches the brand voice or drifts from it, and "
        "if it drifts, say exactly how: wrong tone (e.g. too casual/formal "
        "relative to what's stated), a claim or framing the brand voice "
        "explicitly avoids, or terminology inconsistent with what the "
        "workspace facts establish (e.g. a product name, category, or "
        "phrase used differently than specified). If no brand voice is "
        "on file for this workspace, say so explicitly rather than "
        "inventing a voice to check against, and note that this check "
        "could not be meaningfully performed. Do not rewrite the "
        "variants yourself — flagging clearly is the job."
    ),

    # Part 6 §6.4 — Growth domain. content_calendar_builder reads Part 5's
    # finished PRD/handoff bundle for anything date- or milestone-shaped.
    # Structured-not-prose discipline, same as Part 3's extraction table
    # and Part 5's API contract table.
    "content_calendar_builder": (
        "You read the platform content variants from content_adapter_pool "
        "and, if given as prior context, a finished PRD or handoff "
        "package from the Plan domain — and sequence each platform "
        "variant against any launch date, phased-rollout section, or "
        "feature-by-feature ship dates the PRD/handoff states. Output a "
        "structured list, one row per platform variant, each row exactly "
        "'- date: <date or relative label> | platform: <platform> | "
        "content_ref: <short description of which variant this is>' — "
        "not prose. If a real launch date or milestone is available in "
        "prior context, use it. If NO Plan-domain handoff or launch date "
        "is available in prior context, do not invent one — fall back to "
        "relative sequencing instead ('day of launch', 'day 3', 'week 2', "
        "etc.) and say explicitly in a closing note that no real dates "
        "were available, so relative sequencing was used instead."
    ),

    # Part 6 §6.5 — Growth domain. Scoped honestly to what's actually
    # checkable without a paid data provider (Ahrefs/SEMrush and similar)
    # — same "label it as a structural check, not a ranking signal"
    # discipline Part 3 §3.8 already applied to source-quality flagging.
    #
    # Expanded past the original heading/keyword/meta/readability set to
    # also cover depth-vs-intent, E-E-A-T trust signals, linking quality,
    # image alt coverage, schema.org suggestions (with a deliberate
    # FAQPage/HowTo exclusion — both lost their rich-result eligibility,
    # so recommending them is now outdated advice), and AI/answer-engine
    # quotability — domain knowledge pulled from a broader third-party
    # SEO-audit skill's category list, filtered down to whatever's
    # judgeable from pasted text alone with no live crawl or paid API.
    # The name stays "seo_structure_auditor" rather than renaming to
    # something like "content_auditor" since nothing outside this dict
    # calls it by name yet — no reason to churn the identifier.
    "seo_structure_auditor": (
        "You are a content-quality auditor for a single piece of web "
        "content (a landing page, blog post, or launch page draft) "
        "pasted in as text -- you have no live crawler and no paid SEO "
        "API, so never claim to check rankings, backlinks, indexing "
        "status, or site performance; those live elsewhere. Audit only "
        "what's visible in the text itself, against these categories:\n\n"
        "STRUCTURE: exactly one H1, logical H2/H3 nesting (no skipped "
        "levels), scannable section headings, lists/tables used where "
        "they'd help over a wall of prose.\n\n"
        "METADATA: is there a clear title tag and meta description in "
        "the draft; if present, flag if the title runs long enough to "
        "truncate in search results (roughly 60 characters) or the "
        "description runs past the typical display limit (roughly "
        "150-160 characters) or doesn't summarize the page's actual "
        "value.\n\n"
        "DEPTH VS. INTENT: judge whether the content actually covers "
        "what someone searching for this would need -- not a word-count "
        "target, a coverage judgment. A short page that fully answers "
        "the query beats a long one that pads around it; say so "
        "explicitly if that's the case.\n\n"
        "KEYWORD USAGE: is the apparent primary topic/keyword present in "
        "the title, H1, and early in the body; is usage natural or does "
        "it read stuffed; are related terms/synonyms present.\n\n"
        "READABILITY: sentence and paragraph length, jargon level "
        "against the likely audience, whether dense sections would "
        "benefit from breaking up.\n\n"
        "TRUST AND EXPERIENCE SIGNALS (E-E-A-T, applied to what's in the "
        "text): is there an author or brand voice with visible "
        "credibility, does it cite sources or real data rather than "
        "vague claims, does it show any first-hand experience "
        "(specifics, screenshots described, concrete examples) instead "
        "of generic marketing language, is a date or 'last updated' "
        "signal present.\n\n"
        "LINKING: internal links described with descriptive (not 'click "
        "here') anchor text; any external links point at credible "
        "sources rather than none at all.\n\n"
        "IMAGES: if image references or alt text are present in the "
        "draft, flag missing or non-descriptive alt text; note where an "
        "image or chart would clarify something currently only stated "
        "in prose.\n\n"
        "SCHEMA OPPORTUNITIES: suggest which schema.org type would fit "
        "if this were published (Article/BlogPosting, Product, "
        "LocalBusiness, Organization, etc.) -- but do NOT suggest "
        "FAQPage or HowTo markup, both lost their rich-result benefit "
        "and recommending them is now outdated advice.\n\n"
        "AI/ANSWER-ENGINE READINESS: does the content give a clear, "
        "quotable answer near the top rather than burying the point; "
        "would an AI answer engine or search snippet be able to lift a "
        "clean sentence out of this.\n\n"
        "For every finding, say what's missing or weak, why it matters, "
        "and a concrete fix -- not a vague 'improve this.' Never phrase "
        "a finding as if it reflects actual search ranking or "
        "performance ('this will rank well', 'this hurts your SEO') -- "
        "phrase every finding as a structural or content-quality "
        "observation only. Do not report on rankings, backlinks, crawl/"
        "indexing, site security, or page-load performance -- those are "
        "out of scope for this role and covered elsewhere in this "
        "product. Begin or end your answer with an explicit sentence "
        "stating this is a content-quality audit, not an SEO or GEO "
        "ranking audit, since no ranking-signal data was available to "
        "check against."
    ),

    # Part 6 §6.6 — Growth domain. Same honesty discipline as
    # seo_structure_auditor just above: there is no free "find me real "
    # journalists/influencers" API, so this role suggests categories,
    # never named contacts (which would be fabricated or stale the
    # moment they're generated).
    "outreach_categorizer": (
        "You suggest outlet/creator TYPES to target for outreach — never "
        "named contacts, publications, or individual people, since a "
        "generated name would either be fabricated or stale the moment "
        "it's written. Base your suggestions on the target user described "
        "in any PRD/handoff given as prior context, and on the actual "
        "subject matter of the content itself. Phrase each suggestion as "
        "a category (e.g. 'developer-focused technical newsletters', "
        "'B2B SaaS-focused podcasts', 'regional tech press covering this "
        "market', 'micro-influencers in this specific niche') with a "
        "one-sentence reason it fits this launch. Return 4-8 categories, "
        "not a padded longer list. Begin or end your answer with an "
        "explicit sentence stating these are category suggestions, not a "
        "contact list, since no real outreach-contact database is "
        "available to this system."
    ),
    # Part 7 §7.3 — Coding domain, Integration checklist. Hand-written up
    # front, same reasoning as every other seed brief in this dict.
    # Deliberately mirrors "note_taker" above's fenced-```json convention
    # (a generic_worker role can still produce real structured output —
    # it just has to be enforced by the brief itself, since
    # agents/generic_worker.py applies the same MARKDOWN_INSTRUCTION/
    # NEXT_TAG_INSTRUCTION wrapper and stage_output:* text storage to
    # every role, structured or not). Fixed six-category vocabulary
    # (rather than letting the model invent categories) so the checklist
    # UI (frontend TasksTab.jsx) never has to render an unknown tag —
    # "monitoring" is included even though Part 7 §7.3's own integration
    # list only names five, because §7.5 depends on this role being able
    # to flag it too.
    "integration_flagger": (
        "You read the task text (and, if this run started from a "
        "Plan→Build handoff, the richer PRD/architecture/API-contract "
        "content given as prior context) and tag which common "
        "integrations the spec implies. Check specifically for exactly "
        "these six categories: \"auth\" (user accounts, login, sessions, "
        "permissions), \"payments\" (billing, checkout, subscriptions), "
        "\"email_notifications\" (transactional email, push, SMS), "
        "\"analytics\" (usage tracking, event logging, dashboards), "
        "\"file_storage\" (uploads, attachments, media/blob storage), "
        "and \"monitoring\" (error tracking, uptime checks, logging/"
        "observability). Output ONLY a single fenced ```json code block "
        "containing one JSON object with an \"integrations\" key: a list "
        "of objects, each with \"type\" (one of the six category names "
        "above, spelled exactly as given) and \"evidence\" (the specific "
        "phrase or requirement in the spec that implies it) — nothing "
        "else outside that code block. Only include a category the spec "
        "genuinely implies; do not tag one 'just in case', and do not "
        "invent a category outside this fixed list of six. If nothing in "
        "the spec implies any of these, output a fenced ```json block "
        "containing {\"integrations\": []}."
    ),
    # Patch 8 (rollout guide §3) — Coding domain, implementer split.
    # Hand-written up front, same reasoning as every other seed brief in
    # this dict. Deliberately scoped to APPROACH, not code: this role's
    # whole reason for existing is to separate "what should this module
    # do and how" from "write the actual code" (agents/code_writers.py),
    # so a brief that let it drift into writing snippets would collapse
    # the distinction Part §3 wanted.
    "logic_architect": (
        "You read the module spec(s) for a coding task (given as prior "
        "context) and design the APPROACH before any code is written: "
        "the algorithm or strategy, the key data structures, how the "
        "pieces fit together, and the main edge cases an implementer "
        "needs to watch for. Do NOT write actual code — no function "
        "bodies, no syntax-specific snippets. Think in terms an "
        "implementer in any language could follow: 'use a hash map "
        "keyed on X for O(1) lookups', 'process the list in a single "
        "pass, tracking a running total', 'validate the input shape "
        "before touching the main logic', not 'def foo(x): ...'. If a "
        "module's spec already fully implies a trivial, obvious "
        "approach with no real design decision to make, say so briefly "
        "rather than inventing complexity to sound thorough. Flag "
        "clearly if two modules' specs seem to require an approach that "
        "conflicts (e.g. one assumes data shaped a way the other "
        "produces it differently)."
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
    return {"brief": brief, "source": source, "updated_at": None, "times_hired": 0,
            "pinned": False, "pinned_at": None}


def _load_prompts(user_id: str | None = None) -> dict:
    """Single read path for every function below. Bootstraps from
    ROLE_PROMPTS_SEED on the very first call if the memory bus has
    nothing yet (unchanged behavior), and migrates any bare-string
    legacy entries into the new object shape in the same pass — no
    separate migration script needed, per Part 2 §2.2's design. Writes
    back to the bus only when bootstrap or migration actually changed
    something, so a store that's already fully migrated costs one read
    and zero writes.

    user_id (Part 8.3): forwarded to _role_prompts_key() to pick which
    store this reads/bootstraps — the shared one (default) or this
    user's own, isolated one (ROLE_LIBRARY_SCOPE=per_user). A per-user
    store bootstraps from the SAME ROLE_PROMPTS_SEED as the shared one
    the first time that specific user is ever seen, so nobody's library
    starts empty.

    Performance Audit §5A: checks the per-run cache (see
    _get_cached_prompts()/_cache_prompts() above) before touching Redis
    at all. Every return path below still populates/refreshes the cache
    on its way out, so a cache miss costs exactly the one read() this
    function already did before caching existed — this only removes the
    REPEATED reads across multiple calls within the same run, not the
    first one."""
    key = _role_prompts_key(user_id)
    cached = _get_cached_prompts(key)
    if cached is not None:
        return cached

    prompts = read(key, default=None)
    if prompts is None:
        prompts = {
            name: _wrap_legacy_entry(name, brief)
            for name, brief in ROLE_PROMPTS_SEED.items()
        }
        write(key, prompts)
        _cache_prompts(key, prompts)
        return prompts

    changed = False
    for role_name, value in list(prompts.items()):
        if not isinstance(value, dict):
            prompts[role_name] = _wrap_legacy_entry(role_name, value)
            changed = True
    if changed:
        write(key, prompts)
    _cache_prompts(key, prompts)
    return prompts


def get_role_prompt(role_name: str, user_id: str | None = None) -> str | None:
    """Returns the stored brief for this role as a plain string, or
    None if it's never been written — exactly today's return contract.
    Every existing caller (agents/generic_worker.py's run(),
    eo/panel.py's _get_or_write_role_prompt()) keeps working
    unmodified even though the underlying storage shape widened, as
    long as ROLE_LIBRARY_SCOPE stays at its default "global" — those
    callers don't have a user_id to pass yet, so per-user isolation
    isn't usable from agent code until they're updated to thread one
    through (see Part 8.3 notes above ROLE_PROMPTS_KEY)."""
    entry = _load_prompts(user_id).get(role_name)
    return entry["brief"] if entry else None


def get_role_metadata(role_name: str, user_id: str | None = None) -> dict | None:
    """New in Part 2 §2.2 — returns the full {brief, source,
    updated_at, times_hired} object for the Role Library UI, or None
    if this role has never been briefed. get_role_prompt() above stays
    the string-only contract every non-UI caller already depends on;
    this is the richer read path for the new frontend panel only."""
    return _load_prompts(user_id).get(role_name)


def add_role_prompt(role_name: str, brief: str, source: str = "panel_brief_writer",
                     user_id: str | None = None) -> None:
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
    key = _role_prompts_key(user_id)
    prompts = _load_prompts(user_id)
    existing = prompts.get(role_name, {})
    prompts[role_name] = {
        "brief": brief,
        "source": source,
        "updated_at": _utcnow_iso(),
        "times_hired": existing.get("times_hired", 0),
        # Pin state is a display preference, not part of what a brief
        # edit/rewrite is about — preserved across brief updates the
        # same way times_hired already is, rather than reset to
        # unpinned every time a role's brief changes.
        "pinned": existing.get("pinned", False),
        "pinned_at": existing.get("pinned_at"),
    }
    write(key, prompts)


def update_role_prompt(role_name: str, new_brief: str, source: str = "user_edited",
                        user_id: str | None = None) -> None:
    """New in Part 2 §2.2 — thin wrapper over add_role_prompt(), just
    setting source explicitly so the Role Library UI can visually
    distinguish "you wrote this" from "the system generated this and
    nobody's reviewed it yet." This is what the UI's inline-edit save
    action calls; it directly surfaces the exact risk Part 1 §1.3
    flagged (an unreviewed cold-start brief silently becoming
    permanent). user_id (Part 8.3): the editing user — api/server.py's
    PUT /api/roles/{role_name} passes the caller's own owner_id here,
    which matters only if ROLE_LIBRARY_SCOPE=per_user."""
    add_role_prompt(role_name, new_brief, source=source, user_id=user_id)


def record_role_hire(role_name: str, user_id: str | None = None) -> None:
    """New in Part 2 §2.2 — increments times_hired for a role that was
    just staffed. Not yet called from eo/panel.py in this pass (that's
    a one-line addition inside staff_task() once panel.py is in scope
    for §2.3/§2.5); exposed here now so that follow-up has something to
    call. Creates a bare counter entry rather than raising if the role
    somehow isn't in the store yet (a hire can in principle race a
    first-ever brief write)."""
    key = _role_prompts_key(user_id)
    prompts = _load_prompts(user_id)
    entry = prompts.get(role_name) or {
        "brief": None, "source": "panel_brief_writer",
        "updated_at": None, "times_hired": 0,
        "pinned": False, "pinned_at": None,
    }
    entry["times_hired"] = entry.get("times_hired", 0) + 1
    prompts[role_name] = entry
    write(key, prompts)


def set_role_pinned(role_name: str, pinned: bool, user_id: str | None = None) -> dict:
    """New — Role Library pinned-roles feature. Toggles a role's pinned
    state, persisted server-side alongside its brief/metadata so it
    syncs across devices (same store, same key, as everything else in
    this module — no new storage mechanism). Creates a bare entry (like
    record_role_hire() above) if the role has no brief yet — a role can
    in principle be pinned from a picker before it's ever actually been
    hired/briefed. Returns the updated entry so the API layer can hand
    the fresh {role, brief, source, ..., pinned, pinned_at} straight
    back to the frontend without a second read."""
    key = _role_prompts_key(user_id)
    prompts = _load_prompts(user_id)
    entry = prompts.get(role_name) or {
        "brief": None, "source": "panel_brief_writer",
        "updated_at": None, "times_hired": 0,
        "pinned": False, "pinned_at": None,
    }
    entry["pinned"] = bool(pinned)
    entry["pinned_at"] = _utcnow_iso() if pinned else None
    prompts[role_name] = entry
    write(key, prompts)
    return entry


def list_known_roles(user_id: str | None = None) -> list:
    """Every role the system has ever written a brief for — unchanged
    return contract (sorted list of role-name strings) even though the
    underlying store now holds richer objects per role."""
    return sorted(_load_prompts(user_id).keys())


def list_role_metadata(user_id: str | None = None) -> list:
    """Bulk counterpart to get_role_metadata() — one single read of the
    store instead of N. list_known_roles()+get_role_metadata() per role
    was doing an N+1 read against the memory bus; this returns the same
    data (sorted by role name) in exactly one read() call."""
    prompts = _load_prompts(user_id)
    return sorted(
        ({"role": name, **meta} for name, meta in prompts.items()),
        key=lambda r: r["role"],
    )

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
    "architecture_diagrammer": "architecture_diagrammer",
    "schema_diagrammer": "schema_diagrammer",
    "hardware_speccer": "hardware_speccer",    # MiniMe Blueprint — real action (writes
    # workspace_facts.custom["parts"/"wiring"/"mech"]) via run_hardware_speccer(),
    # same category as architecture_diagrammer/schema_diagrammer just above. Was
    # missing entirely, so Panel could never hire it -- every suggested_agents
    # label for a hardware task fell through resolve_role()'s "generic_worker"
    # default instead, and the spec was never written no matter how the task
    # was phrased or routed.
    "handoff_packager": "handoff_packager",    # Part 3 — research domain's real-action roles. Each performs a real
    # action (external HTTP calls, or writes structured data a
    # downstream role consumes as JSON, not free text) rather than pure
    # reasoning, same category as idea_planner/prompt_writer/test_writer
    # above. "contradiction_detector" and "consensus_meter" are
    # deliberately absent from this map — both require genuine judgment
    # and resolve to "generic_worker" like any reasoning role.
    "academic_search": "academic_search",
    # Task 13 (E1 research role) — same real-action category as
    # academic_search directly above: zero LLM calls, writes each result
    # straight to the graph via eo/knowledge_graph.write_node (see
    # agents/web_researcher.py's own docstring). Distinct from
    # academic_search: scoped to general/forum/news/hackernews web
    # search (utils/web_search.py, task 13a) rather than paper indexes.
    "web_researcher": "web_researcher",
    "source_quality_flagger": "source_quality_flagger",
    "citation_graph_builder": "citation_graph_builder",
    "extraction_table_builder": "extraction_table_builder",
    "contradiction_prefilter": "contradiction_prefilter",
    "dataset_analyst": "dataset_analyst",  # Part 3 §3.7
    # Part 6 §6.1/§6.2 — growth domain. content_adapter_pool performs
    # real, self-contained fan-out work with its own internal
    # concurrency, same category as code_writers/reviewer/fixer_pool —
    # not a generic_worker reasoning role. brand_voice_checker,
    # content_calendar_builder, seo_structure_auditor, and
    # outreach_categorizer are deliberately ABSENT from this map: all
    # four are reasoning-with-structured-input roles that resolve to
    # "generic_worker" like any other reasoning role (Part 6 §6.1).
    "content_adapter_pool": "content_adapter_pool",
    # Part 7 §7.4 — same category as structure_architect above: pure
    # reasoning with no real filesystem action of its own, but it needs
    # real on-disk file-tree access generic_worker.py's run() can't give
    # it, so it's a dedicated module rather than a generic_worker role.
    # deploy_agent (the module that actually writes the config file /
    # gates the live-deploy confirmation) is deliberately NOT in this map
    # — see agents/deploy_agent.py's own docstring for why it's dispatched
    # directly from a UI-button API endpoint instead of through the
    # Panel-hire/executor.py path this map feeds.
    "deploy_config_writer": "deploy_config_writer",
    # Patch 8 (rollout guide §3) — implementer split's other new role.
    # Unlike logic_architect above, this one needs REAL execution to do
    # its job at all (time/memory measurements, not an LLM's guess at
    # them) — same category as dataset_analyst: wraps
    # agents/sandbox_tester.py's _run_one_module() for a genuinely
    # computed result. See agents/performance_reviewer.py's own
    # docstring.
    "performance_reviewer": "performance_reviewer",
}

# Robustness fix (2026-08): Inspector/Panel classifiers freely invent
# suggested_agents labels in natural English (that's by design — see
# inspector.py's own SYSTEM_PROMPT). For MOST roles that's fine, because
# everything not in REAL_ACTION_ROLES safely falls through to
# generic_worker and still gets the task done via plain reasoning.
#
# But a handful of REAL_ACTION_ROLES entries perform an action
# generic_worker categorically CANNOT do on its own — hardware_speccer
# writes a structured BOM to workspace_facts.custom, architecture/schema
# diagrammer render real Mermaid to a dedicated bus key, etc. For exactly
# these, resolve_role()'s old plain dict lookup meant a classifier that
# said "hardware_designer" instead of "hardware_speccer" silently got
# routed to generic_worker's free-text reasoning instead — no error, no
# log, just a task that quietly never produces the real artifact it was
# supposed to. That's the bug this section closes.
#
# Two layers, cheapest/safest first:
#   1. ROLE_ALIASES — a curated, hand-reviewed list of synonyms per
#      canonical role. Exact match only (after normalization). This is
#      the trusted path — add new synonyms here as they're observed in
#      real routing_decision logs.
#   2. A conservative fuzzy fallback (difflib) over the canonical names
#      + all aliases, for a synonym nobody's added yet. High cutoff on
#      purpose — a false POSITIVE here silently sends a task to the
#      wrong dedicated module, which is worse than the false NEGATIVE of
#      just falling through to generic_worker like before. When in
#      doubt, this stays conservative and lets generic_worker take it.
#
# Scope is intentionally narrow: only roles that need a real, non-text
# action belong in ROLE_ALIASES. Plain reasoning roles (reviewer,
# researcher, etc.) should keep falling through to generic_worker — do
# NOT add aliases for those here, that's not what this table is for.
ROLE_ALIASES = {
    "hardware_speccer": [
        "hardware_designer", "hardware_engineer", "hardware_specifier",
        "electronics_designer", "electronics_engineer", "electronics_speccer",
        "component_selector", "parts_speccer", "bom_writer",
        "bill_of_materials_writer", "device_speccer",
    ],
    "architecture_diagrammer": [
        "architecture_designer", "system_architect", "system_diagrammer",
        "architecture_diagram_writer", "architecture_designer_agent",
    ],
    "schema_diagrammer": [
        "database_designer", "db_schema_designer", "schema_designer",
        "entity_diagrammer", "erd_designer", "database_schema_designer",
    ],
}

# Any real-action role NOT listed as a key above (e.g. code_writers,
# fixer_pool, file_manager) keeps its old exact-match-only behavior —
# those roles are hired directly by their own canonical name in
# practice, so they don't need synonym coverage, and giving every
# real-action role a wide alias net would raise the odds of an
# unrelated reasoning label accidentally matching one of them.

_FUZZY_MATCH_CUTOFF = 0.82   # conservative on purpose — see docstring above


def _normalize_role_label(name: str) -> str:
    return name.strip().lower().replace("-", "_").replace(" ", "_")


def _build_alias_lookup() -> dict:
    """Flattens ROLE_ALIASES into {normalized_alias: canonical_name},
    including each canonical name mapping to itself, so alias lookup and
    fuzzy-match candidate generation share one source of truth."""
    lookup = {}
    for canonical, synonyms in ROLE_ALIASES.items():
        lookup[_normalize_role_label(canonical)] = canonical
        for synonym in synonyms:
            lookup[_normalize_role_label(synonym)] = canonical
    return lookup


_ALIAS_LOOKUP = _build_alias_lookup()


def _fuzzy_resolve_specialized_role(role_name: str) -> str | None:
    """Conservative last resort for a synonym not yet in ROLE_ALIASES.
    Only ever matches against the specialized-role alias pool built
    above (never the full REAL_ACTION_ROLES set) — a role like
    "code_writers" has no synonym list here on purpose, so it's never a
    fuzzy-match candidate and can't be accidentally hijacked."""
    normalized = _normalize_role_label(role_name)
    close = difflib.get_close_matches(
        normalized, _ALIAS_LOOKUP.keys(), n=1, cutoff=_FUZZY_MATCH_CUTOFF,
    )
    if not close:
        return None
    canonical = _ALIAS_LOOKUP[close[0]]
    print(f"  [registry] resolve_role: fuzzy-matched '{role_name}' -> "
          f"'{canonical}' (nearest known label '{close[0]}', "
          f"cutoff={_FUZZY_MATCH_CUTOFF}). Consider adding '{role_name}' "
          f"to ROLE_ALIASES['{canonical}'] if this keeps recurring.")
    return canonical


def resolve_role(role_name: str) -> str:
    """Real-action roles resolve to their dedicated module name.
    Resolution order:
      1. Exact match in REAL_ACTION_ROLES (unchanged original behavior).
      2. Exact match (post-normalization) against ROLE_ALIASES for the
         handful of roles where a generic_worker fallback would mean the
         task's real output never gets produced at all.
      3. A conservative fuzzy match against that same alias pool, for a
         synonym nobody's curated yet.
    Everything else resolves to the literal string 'generic_worker' —
    execute_graph's dispatch (Part 10 §4) is what actually routes that to
    agents.generic_worker.run(role=role_name, ...)."""
    if role_name in REAL_ACTION_ROLES:
        return REAL_ACTION_ROLES[role_name]

    normalized = _normalize_role_label(role_name)
    aliased = _ALIAS_LOOKUP.get(normalized)
    if aliased and aliased in REAL_ACTION_ROLES:
        return REAL_ACTION_ROLES[aliased]

    fuzzy = _fuzzy_resolve_specialized_role(role_name)
    if fuzzy and fuzzy in REAL_ACTION_ROLES:
        return REAL_ACTION_ROLES[fuzzy]

    return "generic_worker"

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
    architecture_diagrammer,
    schema_diagrammer,
    architecture_diagrammer,
    schema_diagrammer,
    handoff_packager,  # Part 5 §5.6
    prompt_writer_lean,
    code_writer_lean,
    reviewer_fixer_lean,
    generic_worker,
    academic_search,  # Part 3 §3.3 — was missing from this block, see fix note
    web_researcher,  # Task 13c — E1 research role, general/forum/news/hackernews scopes
    extraction_table_builder,  # Part 3 §3.5
    contradiction_prefilter,  # Part 3 §3.6
    dataset_analyst,  # Part 3 §3.7
    source_quality_flagger,  # Part 3 §3.8
    citation_graph_builder,  # Part 3
    content_adapter_pool,  # Part 6 §6.2
    deploy_config_writer,  # Part 7 §7.4
    performance_reviewer,  # Patch 8 (rollout guide §3)
    hardware_speccer,  # MiniMe Blueprint — was never imported, so REGISTRY
    # below couldn't reference it either. See REAL_ACTION_ROLES entry above.
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
    "architecture_diagrammer": {"callable": architecture_diagrammer.run_architecture_diagrammer, "needs_cycle_num": False},
    "schema_diagrammer": {"callable": schema_diagrammer.run_schema_diagrammer, "needs_cycle_num": False},
    "hardware_speccer": {"callable": hardware_speccer.run_hardware_speccer, "needs_cycle_num": False},
    "handoff_packager": {"callable": handoff_packager.run_handoff_packager, "needs_cycle_num": False},
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
    # --- Part 3 research-domain real-action roles ---
    # academic_search (§3.3) was resolvable via REAL_ACTION_ROLES and
    # dispatched by name in executor.py, but had no REGISTRY entry until
    # this line — resolve("academic_search") was a guaranteed KeyError.
    "academic_search":        {"callable": academic_search.run,               "needs_cycle_num": False},
    # Task 13d/13e — web_researcher.run() takes an extra scope= kwarg
    # ("general"/"forum"/"news"/"hackernews"). Threaded explicitly end
    # to end from api/routes/tasks.py's TaskRequest.scope, through
    # run_task()/_dispatch_resolved()/_run_tier3_hires()/
    # run_with_looping()/execute_graph(), down to eo/executor.py's own
    # web_researcher dispatch branch -- not inferred from task_text, so
    # a UI scope selector is authoritative rather than best-effort. This
    # is a deliberately DIFFERENT choice than academic_search's own
    # `sources` param one line above, which genuinely is never threaded
    # (always None/all four) -- picking the wrong academic index is
    # mostly-harmless breadth, picking the wrong web_researcher scope
    # (e.g. silently returning general web results instead of the
    # Hacker News the person selected) is a real, visible bug.
    "web_researcher":         {"callable": web_researcher.run,                "needs_cycle_num": False},
    "extraction_table_builder": {"callable": extraction_table_builder.run,    "needs_cycle_num": False},
    # contradiction_prefilter (§3.6) is the deterministic pre-filter half
    # of the contradiction/gap detector; "contradiction_detector" itself
    # has no entry here — it's a genuine generic_worker judgment role.
    "contradiction_prefilter": {"callable": contradiction_prefilter.run,      "needs_cycle_num": False},
    # dataset_analyst (§3.7) wraps sandbox_tester.py's _run_one_module()
    # for real, computed dataset analysis rather than an LLM's guess.
    "dataset_analyst": {"callable": dataset_analyst.run,                      "needs_cycle_num": False},
    # source_quality_flagger (§3.8): deterministic quality flags + reused
    # (agents/duplication_checker.py) near-duplicate detection.
    "source_quality_flagger": {"callable": source_quality_flagger.run,        "needs_cycle_num": False},
    # citation_graph_builder: read-only view over the "cites" edges
    # academic_search.py already writes — no new nodes/edges of its own.
    "citation_graph_builder": {"callable": citation_graph_builder.run,        "needs_cycle_num": False},
    # Part 6 §6.2 — growth domain's dedicated parallel content fan-out
    # pool. See REAL_ACTION_ROLES above for why this has a dedicated
    # module instead of resolving to "generic_worker".
    "content_adapter_pool": {"callable": content_adapter_pool.run,            "needs_cycle_num": False},
    # Part 7 §7.4 — see REAL_ACTION_ROLES above for why this is a
    # dedicated module rather than a generic_worker role.
    "deploy_config_writer": {"callable": deploy_config_writer.run_deploy_config_writer, "needs_cycle_num": False},
    # Patch 8 (rollout guide §3) — see REAL_ACTION_ROLES above for why
    # this is a dedicated module rather than a generic_worker role.
    "performance_reviewer": {"callable": performance_reviewer.run, "needs_cycle_num": False},
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