"""
eo/product_tier_map.py — MiniMe Roadmap Part 3.2's agent_tier_map skeleton.

NAMING NOTE — read this before touching `tier` anywhere near this file:
This codebase already has a real, load-bearing `tier` (int: 0/1/2/3 —
see eo/structure.py's PATH_TO_TIER/TIER_TO_PATH, and every `tier=`
kwarg threaded through eo/executor.py and utils/llm_client.log_usage()).
That `tier` is an EXECUTION-COMPLEXITY routing tier: which of the 74
agents actually get invoked for a given request (tier 0 = instant
Responder, tier 1 = lean pipeline, tier 2 = fixed, tier 3 = the full
adaptive/committee path), resolved from a `path` string via
PATH_TO_TIER.

This module's tier is a completely different, orthogonal axis: which
PAID PRODUCT the requesting user is on (Basic / Moderate / Ultimate —
Roadmap Part 3). A single request can simultaneously be an execution
tier-3 ("adaptive") request from a Basic product-tier user. Neither
axis implies the other. To keep that straight in code, not just in
comments, everything in this module is spelled `product_tier`, never
`tier` — do not shorten it back to `tier` in any code that touches
both this module and eo/executor.py's / eo/structure.py's `tier`.

WHAT THIS IS
One row per agent *role* — the same vocabulary eo/registry.py's
AGENT_CAPABILITIES natural_roles tags and REAL_ACTION_ROLES keys
already speak — with one column per product tier (PRODUCT_TIERS
below), each cell describing the model class that role is allowed to
route to and its ceiling/decomposition posture at that product tier.

WHAT THIS IS NOT (yet)
1. Not wired into routing. eo/dynamic_chain.py's _rank_accounts() and
   agents/generic_worker.py's own fallback-chain builder don't read
   this yet, deliberately — see Roadmap Part 3.5/3.6's gates.
   AGENT_PRODUCT_TIER_MAP is read by nothing at runtime right now.
   get_agent_product_tier_profile() below is the intended future read
   path once Moderate's build actually starts (Roadmap Phase 3a).
2. Not a source of real numbers for Moderate/Ultimate. Their columns
   are `None` placeholders on purpose (Roadmap Part 3.3: ceilings get
   loosened per agent only after a real empirical A/B test against an
   actual mid/frontier-tier model candidate — never assumed).
3. Not a source of real MEASURED numbers for Basic's cells either.
   Basic's column exists to assert "zero behavior change from what's
   live today" (Roadmap Part 3.2), but this codebase doesn't
   currently store per-role ceiling/decomposition constants in one
   reusable place to read them from — they're embedded piecemeal
   inside each agent module's own calls into
   utils.llm_client.generate_text() and its _max_tokens_for()
   defaults. Basic's cells therefore use the INHERIT sentinel below
   rather than a guessed number: "whatever the live code path already
   does for this role, unchanged." Replacing INHERIT with real
   measured figures is exactly what Roadmap Checklist item 1
   (cost-per-task logging, live from Phase 0) is for — do that first,
   then backfill this table from real data, not the other way around.

Basic's own account/key provisioning lives in eo/registry.py's
AGENT_CAPABILITIES, untouched by this file. This module answers a
different question than that dict does — not "which key can serve
this role" but "which product tier is this role allowed to behave
differently under, and how."
"""
from __future__ import annotations

# The three paid product tiers, Roadmap Part 3. Order matters where
# code iterates this tuple (e.g. a future admin-panel tier selector) --
# keep it Basic -> Moderate -> Ultimate, matching the roadmap's own
# cascade direction (Part 3.7).
PRODUCT_TIERS = ("basic", "moderate", "ultimate")

# Sentinel for "this cell intentionally defers to whatever the current
# code path already does for this role -- not yet measured, not a
# real number, not something to branch routing logic on." See module
# docstring point 3. Deliberately a distinct sentinel from None: None
# means "this product tier has no profile for this role yet at all"
# (Moderate/Ultimate, pre-3.3-testing); INHERIT means "Basic's profile
# exists, and it's identical to today's live, already-shipped behavior."
INHERIT = "inherit"


def _basic_row() -> dict:
    """One Basic-column cell, matching module docstring point 3: every
    field is INHERIT until Checklist item 1's real cost-per-task
    logging gives an actual number to put here.

    Returns a fresh dict per call -- callers should never mutate a
    cell in place. A cell is meant to be replaced wholesale once real
    data exists for it (Part 3.3's empirical testing replaces a whole
    cell at once, not individual fields patched piecemeal).
    """
    return {
        # Today's AGENT_CAPABILITIES / dynamic_chain.PROVIDER_DEFAULT_MODEL
        # resolution for this role, unchanged by this table.
        "model_class": INHERIT,
        # Today's utils.llm_client per-call max_tokens for this role's
        # calls, unchanged by this table.
        "max_output_tokens": INHERIT,
        # Today's decomposition depth (how many sub-steps this role's
        # task is split into), unchanged by this table.
        "max_substeps": INHERIT,
        # Today's retry/continuation behavior for this role, unchanged
        # by this table.
        "retry_continuation": INHERIT,
    }


# One row per agent role. Rows are the union of eo/registry.py's
# AGENT_CAPABILITIES natural_roles tags (generic_worker-dispatched
# reasoning roles) and REAL_ACTION_ROLES keys (roles with a dedicated
# action module), as of this patch. That module remains the
# authoritative live vocabulary; this table is a snapshot of it, not a
# live view -- tests/unit/test_eo_product_tier_map.py's coverage check
# is what catches this table drifting out of sync after a role is
# added to (or retired from) either of those, and should be re-run
# (it will fail loudly) any time that happens.
#
# Every role's moderate/ultimate cell is `None`: no ceiling has been
# loosened for anything yet, for any role. Part 3.3's per-agent,
# per-product-tier empirical testing is what populates a role's
# moderate or ultimate cell, one at a time, as testing for that
# specific role actually completes. An all-None column is the correct,
# honest state until that testing exists -- it is not a bug in this
# file, and should not be "filled in" with guessed values to make the
# table look more finished than the underlying testing actually is.
AGENT_PRODUCT_TIER_MAP: dict[str, dict[str, dict | None]] = {
    role: {"basic": _basic_row(), "moderate": None, "ultimate": None}
    for role in (
        "academic_search",
        "analyst",
        "architecture_diagrammer",
        "backlink_detector",
        "brainstormer",
        "citation_graph_builder",
        "code_editor",
        "content_adapter_pool",
        "content_writer",
        "contradiction_prefilter",
        "correction_locator",
        "dataset_analyst",
        "dependency_mapper",
        "deploy_config_writer",
        "documentation_writer",
        "duplication_checker",
        "editor",
        "extraction_table_builder",
        "fact_checker",
        "file_manager",
        "final_qa",
        "fixer",
        "formatter",
        "gatekeeper",
        "handoff_packager",
        "hardware_speccer",
        "idea_planner",
        "implementer",
        "inspector",
        "logic_architect",
        "mech_primitive",
        "mech_section",
        "mech_subsection",
        "memory_search",
        "note_table_builder",
        "outliner",
        "panel_member_b",
        "part_price_finder",
        "performance_reviewer",
        "prompt_writer",
        "report_writer",
        "researcher",
        "sandbox_tester",
        "schema_diagrammer",
        "security_reviewer",
        "sga",
        "source_manager",
        "source_planner_lean",
        "source_quality_flagger",
        "structure_architect",
        "test_writer",
        "verifier",
        "web_researcher",
        "writer",
    )
}


def get_agent_product_tier_profile(role: str, product_tier: str = "basic") -> dict | None:
    """Look up `role`'s profile at `product_tier`.

    Not called from any live routing code yet -- this is the read
    path Roadmap Part 3.2 describes eo/dynamic_chain.py's
    _rank_accounts() eventually using, once Phase 3a's Moderate build
    starts populating real cells (see module docstring point 1).
    Wiring this into actual routing is deliberately out of scope for
    this patch.

    Returns None if `product_tier` has no profile yet for `role`
    (expected and normal for moderate/ultimate pre-3.3-testing --
    a future caller on that path should treat None as "fall back to
    today's untiered behavior for this role", not as an error).

    Raises:
        KeyError: `role` isn't in AGENT_PRODUCT_TIER_MAP at all --
            an unregistered role, a real bug to fix by adding a row,
            not something to silently default around.
        ValueError: `product_tier` isn't one of PRODUCT_TIERS -- a
            typoed tier name, also a real bug.
    """
    if product_tier not in PRODUCT_TIERS:
        raise ValueError(
            f"unknown product_tier {product_tier!r} -- expected one of {PRODUCT_TIERS}"
        )
    return AGENT_PRODUCT_TIER_MAP[role][product_tier]
