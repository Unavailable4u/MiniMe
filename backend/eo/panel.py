"""
eo/panel.py — EO Panel, Part 2.2 of the v5 Master Blueprint. Escalation
only: called by eo/loop_v4.py when the Inspector's own classification is
below the confidence threshold or already tier >= 2 (Part 3's decision
flow).

Three members:
  A — the Inspector's own draft classification, already computed. No
      extra call (Part 2.2: "carried over").
  B — a second model lineage, genuinely different from the Inspector's.
      Provider substitution note: the blueprint specifies OpenRouter free
      tier here, but per utils/llm_client.py's own docstring, OpenRouter
      (like Gemini) isn't used anywhere in this codebase. Substituting
      Cerebras instead keeps the actual property Part 2.2 cares about --
      "deliberately different model lineages ... a panel of three calls
      to variants of the same base model isn't a panel" -- since the
      Inspector runs on Groq and member C (below) runs on Mistral/Gemini,
      Cerebras is the one remaining distinct lineage/account.
  C — Quota-reality fix, §4 (2026-07-30): was GitHub Models gpt-4.1-mini,
      fallback gpt-4.1-nano, via EO_PANEL_GITHUB_PAT. GitHub Models
      retired in full today; now Mistral mistral-medium-latest, fallback
      Gemini gemini-3.1-flash-lite, via the reserve keys
      MISTRAL_API_KEY_7 / GEMINI_API_KEY_9 (confirmed unused elsewhere).

Synthesis rule (Part 2.2, restated exactly):
  - tier: the HIGHEST tier across all three opinions. Never under-route
    on disagreement.
  - suggested_agents: the UNION of all three.
  - confidence: the AVERAGE of all three.
  - directed_task_type: kept only if unanimous or null across all three;
    genuine disagreement bumps to tier 3 scoping (this module forces tier
    to 3 in that specific case, per the blueprint's own wording, even if
    the max-tier rule above would have landed lower).
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.inspector import SYSTEM_PROMPT, _strip_fences, _validate
from eo.structure import build_reference_structure_addition, PATH_TO_TIER, TIER_TO_PATH
from utils.llm_client import generate_text
from eo.registry import AGENT_CAPABILITIES, get_role_prompt, add_role_prompt, record_role_hire
from eo.quota_sentinel import get_quota_snapshot
from relay.emitter import emit_event

MEMBER_B_CHAIN = [
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "EO_PANEL_CEREBRAS_KEY"},
    # Env-audit follow-up: EO_PANEL_CLOUDFLARE_ACCOUNT_ID/API_TOKEN were
    # provisioned back when the blueprint's original Member B plan was
    # Cloudflare Workers AI (@cf/meta/llama-3.1-8b-instruct — see this
    # module's own docstring for why Cerebras was substituted in as the
    # primary instead). The Cloudflare account was never decommissioned
    # and the credentials are real, so rather than let them sit idle
    # they're wired in here as Member B's fallback -- this also closes a
    # real single-point-of-failure gap the chain had (one step, one
    # account, no fallback at all for an escalation-only vote that's
    # supposed to represent a genuinely distinct model lineage).
    {"provider": "cloudflare", "model": "@cf/meta/llama-3.1-8b-instruct",
     "account_id_env": "EO_PANEL_CLOUDFLARE_ACCOUNT_ID", "token_env": "EO_PANEL_CLOUDFLARE_API_TOKEN"},
    # GEMINI_API_KEY_7 was reserved in the .env.example Part 2 migration
    # comments for exactly this slot ("-> EO_PANEL_CEREBRAS_KEY
    # (panel_member_b): distinct model lineage, Gemini Pro") but never
    # actually got wired into any chain -- confirmed zero other
    # references in the codebase. Added here as the second fallback, per
    # that original intent, EXCEPT for the model: utils/llm_client.py's
    # own QUOTA_CONFIG deliberately omits gemini-3.1-pro/gemini-2.5-pro
    # ("0/0 on the free tier (paid-only) ... will fail every time, not
    # just on exhaustion") -- so "Gemini Pro" as originally planned would
    # have been a guaranteed-broken fallback step. gemini-3.6-flash is
    # the free-tier model every other Gemini chain in this codebase
    # actually uses.
    {"provider": "gemini", "model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY_7"},
]
# Quota-reality fix, §4: GitHub Models retired in full (2026-07-30) --
# this chain was two GitHub steps on the SAME key (EO_PANEL_GITHUB_PAT),
# so it was already single-point-of-failure even before retirement made
# it a hard outage. MISTRAL_API_KEY_7 / GEMINI_API_KEY_9 were confirmed
# (0 occurrences anywhere else in the repo) as the reserve keys
# multi-provider-scaling-guide.md §3 held back for exactly this "distinct
# model lineage" panel voice -- no new key needed. gemini-3.1-flash-lite's
# real RPD is 500, comfortably enough for a once-per-task classification
# vote.
MEMBER_C_CHAIN = [
    {"provider": "mistral", "model": "mistral-medium-latest", "key_env": "MISTRAL_API_KEY_7"},
    # MISTRAL_API_KEY_6 was reserved by the same .env.example Part 2
    # comment block as MISTRAL_API_KEY_7 above ("-> panel sibling:
    # researcher / fact_checker (distinct lineage, alongside Gemini)")
    # but MISTRAL_API_KEY_7 was the only one of the pair actually wired
    # in -- confirmed zero other references to _6 anywhere in the repo.
    # Added here as a same-provider fallback before dropping to the
    # cross-provider Gemini step below, closing the gap flagged in the
    # env audit.
    {"provider": "mistral", "model": "mistral-medium-latest", "key_env": "MISTRAL_API_KEY_6"},
    {"provider": "gemini", "model": "gemini-3.1-flash-lite", "key_env": "GEMINI_API_KEY_9"},
]
# Migration Part 26 §4c: PATH_TO_TIER / TIER_TO_PATH now come from
# eo/structure.py (one shared definition) instead of being redefined here.
# eo.inspector.classify()/_validate() emit "path" (Part 12), but every vote
# in this module (_UNREACHABLE_VOTE, and the draft passed in from
# loop_v4.py) is keyed by "tier" — a real member B/C vote needs the same
# translation applied, or _synthesize()'s `v["tier"]` lookup raises
# KeyError the moment a panel member actually succeeds instead of falling
# back to _UNREACHABLE_VOTE. TIER_TO_PATH is what lets _synthesize()'s
# return dict carry a real "path" again (Part 26 §6) so
# eo/routing_memory.py's log_outcome() stops recording "path": None for
# every escalated task.

# A conservative stand-in vote used when a panel member's own chain is
# fully exhausted. Per the "never under-route on disagreement" rule, a
# member that couldn't be reached votes tier 3 with low confidence rather
# than being silently dropped from the union/average — a missing vote is
# not the same thing as an "everything's fine" vote.
_UNREACHABLE_VOTE = {
    "tier": 3,
    "directed_task_type": None,
    "confidence": 0.0,
    "suggested_agents": [],
    "reasoning": "member unreachable — all providers in its chain failed",
    "domain": None,
    "execution_order": [],
    "parallel_groups": [],
}


def _get_member_vote(label: str, task_text: str, chain: list) -> dict:
    try:
        user_content = f"Task: {task_text}" + build_reference_structure_addition(task_text)
        raw = generate_text(
            system_prompt=SYSTEM_PROMPT,
            user_content=user_content,
            chain=chain,
            agent_name=f"EO Panel ({label})",
        )
        parsed = json.loads(_strip_fences(raw))
        validated = _validate(parsed)
        validated["tier"] = PATH_TO_TIER[validated["path"]]   # NEW — same fix as loop_v4.py's draft
        validated["_reachable"] = True
        return validated
    except Exception as exc:
        print(f"  [EO Panel] member {label} unreachable ({exc.__class__.__name__}: {exc}), "
              f"voting conservative (tier 3, confidence 0.0).")
        vote = dict(_UNREACHABLE_VOTE)
        vote["_reachable"] = False
        return vote


def _merge_execution_order(votes: list, all_agents_sorted: list) -> list:
    """Migration Part 10 §3 — the guide folds execution_order into the
    Panel's schema but doesn't specify how to synthesize THREE members'
    orders into one. Each member's own execution_order only covers ITS
    OWN suggested_agents, not the union all three voted for, so we can't
    just pick one member's list wholesale.

    Chosen rule: stable-merge in priority order A, B, C (A is the
    Inspector's own already-more-trusted draft, matching how member A is
    already treated as "carried over" rather than an equal blind vote
    elsewhere in this module) — first-seen role wins its position, and
    any unioned role none of the three explicitly ordered (e.g. only
    mentioned via suggested_agents union, not in anyone's own
    execution_order) is appended at the end rather than dropped, per the
    same "never drop a hired role" principle Part 10 §3.1 uses in
    build_execution_graph_from_hires()."""
    merged, seen = [], set()
    for v in votes:
        for role in v.get("execution_order", []):
            if role in all_agents_sorted and role not in seen:
                merged.append(role)
                seen.add(role)
    for role in all_agents_sorted:
        if role not in seen:
            merged.append(role)
            seen.add(role)
    return merged


def _merge_parallel_groups(votes: list, all_agents_sorted: list) -> list:
    """Step 2 of the parallel-execution work (see eo/inspector.py Step 1
    for where `parallel_groups` first enters a vote's schema, and
    eo/router.py Step 3 for the hard sanitizer that actually gates
    dispatch — this function only decides what the PANEL collectively
    believes is safe to run concurrently; it is not itself the
    gatekeeper).

    Synthesis rule: same "never trust a lone voice on something
    safety-relevant" posture as the rest of this module (compare the
    max-tier and directed_task_type rules in _synthesize() below). A
    candidate group only survives if at least 2 of the 3 members
    proposed something compatible with it — an exact match, a superset,
    or a subset — since two members drawing the boundary slightly
    differently around the same core parallel set is still agreement on
    the underlying claim "these can run together," not disagreement.
    A single member's unconfirmed guess is dropped, exactly like a
    single member's unconfirmed directed_task_type is dropped below.

    Once a candidate has >=2 supporting votes, the surviving group is
    the INTERSECTION of the supporting members' own proposals (not the
    raw candidate) — this keeps one member's overly-broad group from
    smuggling in a role the other supporting member never actually
    named alongside it. Groups that end up nested inside another
    surviving group after this are dropped in favor of the larger one.

    Every group returned here still has to clear eo/router.py's hard
    sanitizer (hired check, approval_roles exclusion, overlap/dedup,
    singleton/size-cap) before anything downstream is allowed to act on
    it — this function only reflects panel consensus, nothing more."""
    all_agents_set = set(all_agents_sorted)

    # Normalize each vote's own groups: drop anything malformed, any
    # member not in the union of hired-candidate roles, and collapse to
    # size>=2 (a "group" of one isn't a parallelism claim at all — that's
    # eo/router.py's singleton rule, restated here so a lone-role
    # "group" can't even enter the candidate pool).
    normalized = []
    for v in votes:
        groups, seen = [], set()
        for g in v.get("parallel_groups", []) or []:
            if not isinstance(g, list):
                continue
            fs = frozenset(r for r in g if isinstance(r, str) and r in all_agents_set)
            if len(fs) < 2 or fs in seen:
                continue
            seen.add(fs)
            groups.append(fs)
        normalized.append(groups)

    # Candidate pool: every distinct group any member proposed at all.
    candidates = set()
    for groups in normalized:
        candidates.update(groups)

    survivors = set()
    for cand in candidates:
        supporting = [
            i for i, groups in enumerate(normalized)
            if any(cand <= g or g <= cand for g in groups)
        ]
        if len(supporting) < 2:
            continue
        # Tightest compatible proposal per supporting vote (closest to
        # `cand` by symmetric difference), then intersect across those —
        # the conservative merge described above.
        tightest = []
        for i in supporting:
            match = min(
                (g for g in normalized[i] if cand <= g or g <= cand),
                key=lambda g: len(g.symmetric_difference(cand)),
            )
            tightest.append(match)
        merged = frozenset.intersection(*tightest)
        if len(merged) >= 2:
            survivors.add(merged)

    # Drop any survivor strictly contained in another surviving group —
    # the larger, still-2-of-3-agreed group is strictly more informative.
    final = [g for g in survivors if not any(g < other for other in survivors)]

    # Deterministic, JSON-stable output: sorted role names, groups
    # ordered by (size, role names) rather than set-iteration order.
    return [sorted(g) for g in sorted(final, key=lambda g: (len(g), sorted(g)))]


def _synthesize(votes: list, draft: dict) -> dict:
    tiers = [v["tier"] for v in votes]
    max_tier = max(tiers)
    all_agents = set()
    for v in votes:
        all_agents.update(v.get("suggested_agents", []))
    avg_confidence = sum(v["confidence"] for v in votes) / len(votes)
    directed_types = {v.get("directed_task_type") for v in votes}
    if len(directed_types) == 1:
        directed_task_type = directed_types.pop()
    else:
        # Genuine disagreement on task type — bump to tier 3 scoping
        # rather than guessing which member was right (Part 2.2).
        directed_task_type = None
        max_tier = max(max_tier, 3)

    # Migration Part 10 §3 — domain and execution_order. Domain
    # disagreement does NOT bump tier the way directed_task_type
    # disagreement does — domain only biases execution_order (a
    # convenience), it isn't load-bearing for routing correctness the
    # way directed_task_type is, so there's no need to force tier 3 over
    # three members picking different reference structures.
    domains = {v.get("domain") for v in votes}
    domain = domains.pop() if len(domains) == 1 else None
    all_agents_sorted = sorted(all_agents)
    execution_order = _merge_execution_order(votes, all_agents_sorted)
    parallel_groups = _merge_parallel_groups(votes, all_agents_sorted)

    reasoning = " | ".join(
        f"member {label}: {v.get('reasoning', '')}"
        for label, v in zip("ABC", votes)
    )
    # Raw per-member votes, kept alongside the flattened `reasoning`
    # string (unchanged, nothing downstream that reads `reasoning`
    # breaks). This is what lets a frontend trace card show "all 3 panel
    # opinions" (Part 6.6) as structured data instead of re-parsing the
    # joined string.
    # `_reachable` (set by _get_member_vote, defaulting True for member A
    # since the draft is always a real Inspector classification, never a
    # fallback) is bookkeeping only -- popped off each vote before it
    # goes into panel_votes so it doesn't leak into the API/frontend
    # payload as if it were part of the panel's own schema.
    reachable = [v.pop("_reachable", True) for v in votes]
    panel_votes = [
        {"member": label, **v}
        for label, v in zip("ABC", votes)
    ]
    # Bug fix: this used to be hardcoded True unconditionally, so a
    # member that crashed and fell back to _UNREACHABLE_VOTE (e.g. the
    # Cloudflare dict/str crash above, or any other chain exhaustion)
    # still produced a "Panel reviewed" header on the frontend even
    # though that member never actually reviewed anything. Only claim a
    # real review happened when every member's own chain produced a
    # genuine vote.
    panel_reviewed = all(reachable)
    return {
        "tier": max_tier,
        "path": TIER_TO_PATH.get(max_tier),
        "directed_task_type": directed_task_type,
        "confidence": round(avg_confidence, 4),
        "suggested_agents": all_agents_sorted,
        "reasoning": reasoning,
        "panel_reviewed": panel_reviewed,
        "panel_votes": panel_votes,
        "domain": domain,
        "execution_order": execution_order,
        # Present on the return dict as of Step 2 of the parallel-execution
        # work; not yet consumed by any caller. eo/router.py Step 3 is the
        # actual gatekeeper that decides whether any of this is safe to
        # act on — nothing downstream should dispatch off this list alone.
        "parallel_groups": parallel_groups,
    }


def run_panel(task_text: str, draft: dict) -> dict:
    """
    `draft` is the Inspector's own already-computed classification dict
    (member A, per Part 2.2 — no extra call). Runs members B and C, then
    synthesizes all three per the rule above.
    """
    member_b = _get_member_vote("B", task_text, MEMBER_B_CHAIN)
    member_c = _get_member_vote("C", task_text, MEMBER_C_CHAIN)
    result = _synthesize([draft, member_b, member_c], draft)
    return result

# NEW — add to eo/panel.py, below run_panel()

QUOTA_CUTOFF = 0.8   # matches the blueprint's 80% figure — the same threshold
                     # quota_sentinel.py already uses to fire quota_alert

# Reuses the EXISTING EO_PANEL_CEREBRAS_KEY account (Part 2's Panel
# Member B) — no new account provisioning needed for this. Writing a new
# brief is an occasional single call, not parallel worker traffic.
BRIEF_WRITER_CHAIN = [
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "EO_PANEL_CEREBRAS_KEY"},
    # Fallback fix: this chain used to be a single step on the SAME
    # EO_PANEL_CEREBRAS_KEY account MEMBER_B_CHAIN above hammers on every
    # escalation. When Member B's own traffic rate-limits that account,
    # _set_cooldown() puts cerebras:EO_PANEL_CEREBRAS_KEY into cooldown --
    # and because this chain had no other step, generate_text() below hit
    # _is_cooling_down() straight away, `continue`d past the only entry,
    # and fell out of the loop having never actually attempted a call. That
    # left last_exc unset (it's only assigned inside the `except
    # _TRANSIENT_ERRORS` branch, which never ran) and produced the
    # confusing "All providers in fallback chain exhausted or unavailable.
    # Last error: None" crash -- a single shared-account outage taking down
    # role-brief writing for every in-flight task.
    #
    # Rather than provision new credentials for what the original comment
    # already correctly notes is "an occasional single call, not parallel
    # worker traffic", this reuses MEMBER_B_CHAIN's own already-wired
    # fallback slots immediately above -- distinct provider, distinct
    # accounts, so a Member-B-triggered cooldown on the Cerebras key no
    # longer strands this chain too.
    {"provider": "cloudflare", "model": "@cf/meta/llama-3.1-8b-instruct",
     "account_id_env": "EO_PANEL_CLOUDFLARE_ACCOUNT_ID", "token_env": "EO_PANEL_CLOUDFLARE_API_TOKEN"},
    {"provider": "gemini", "model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY_7"},
]

BRIEF_WRITER_SYSTEM_PROMPT = """You write reusable role briefs for a multi-agent task-execution system. \
Given a role name and the task that currently needs it, write a concise brief (2-4 sentences) describing \
what an agent filling this role should do. Write it to generalize beyond this one task — it will be reused \
verbatim for every future task that needs this same role, so avoid referencing specifics of the current \
task itself. Respond with ONLY the brief text — no preamble, no markdown, no quotation marks."""


def _get_or_write_role_prompt(role_name: str, task_text: str, session_id: str = None,
                                tier: int = None) -> str:
    """Fast path: the role's already in the registry (Part 1's global,
    non-namespaced store) — return it, zero extra LLM calls. Slow path
    (only happens once per genuinely new role, ever): write a new brief
    and save it so this slow path never runs again for this role."""
    existing = get_role_prompt(role_name)
    if existing:
        return existing

    emit_event("agent_start", session_id, agent="panel_brief_writer",
               payload={"label": f"Writing a new role brief: {role_name}"})
    try:
        brief = generate_text(
            system_prompt=BRIEF_WRITER_SYSTEM_PROMPT,
            user_content=f"Role: {role_name}\nTask that currently needs it: {task_text}",
            chain=BRIEF_WRITER_CHAIN,
            agent_name="Panel (brief writer)",
            session_id=session_id,   # Part 8 §9 — without this, this call's usage
            tier=tier,                # never got logged or shown live, silently.
        ).strip()
    except RuntimeError as exc:
        # Fallback-chain fix, follow-up: even with BRIEF_WRITER_CHAIN now
        # three providers deep, a total outage is still theoretically
        # possible -- and this call sits directly in the request path of
        # staff_task()/_resolve_decision_and_hires(), so letting it raise
        # here takes down the entire /tasks request over what's genuinely
        # a "nice to have" (a well-written, reusable role brief), not a
        # hard requirement. Same fire-and-forget posture emit_event() and
        # _set_cooldown() already follow elsewhere in this file: log it,
        # don't let it take down the real work riding alongside it.
        #
        # Deliberately NOT calling add_role_prompt() here -- that would
        # permanently cache this generic text as role_name's brief
        # (get_role_prompt()'s fast path above means it would never be
        # retried), silently degrading every future task that hires this
        # role. Returning it unpersisted means this one task proceeds with
        # a generic-but-workable brief, and the next task that needs this
        # role gets a fresh attempt at a real one.
        print(f"  [Panel (brief writer)] role brief generation failed for "
              f"'{role_name}' ({exc}); using a generic, non-persisted "
              f"fallback brief for this task only.")
        emit_event("agent_done", session_id, agent="panel_brief_writer",
                   payload={"summary": f"Role brief generation failed for "
                                       f"'{role_name}'; used a generic fallback"})
        return (f"Act as the '{role_name}' role for this task. Focus on "
                f"exactly the work you were hired to do, use the task "
                f"context and any prior agents' output you're given, and "
                f"hand back a complete, direct result.")

    add_role_prompt(role_name, brief)
    emit_event("agent_done", session_id, agent="panel_brief_writer",
               payload={"summary": f"New role '{role_name}' added to the registry"})
    return brief


def _usage_fraction(key_env: str, quota_status: dict) -> float:
    """0.0-1.0+ fraction of this account's estimated daily quota already
    used, read directly from get_quota_snapshot()'s own {"pct": ...}
    field. Missing usage data, an unknown key, or a provider with no
    verified QUOTA_CONFIG entry (cloudflare, mistral) all read as 0.0 —
    fail toward "treat it as available" rather than toward excluding an
    account we simply have no data on yet."""
    if not quota_status:
        return 0.0
    return quota_status.get(key_env, {}).get("pct") or 0.0


def _is_cooling_down(key_env: str, quota_status: dict) -> bool:
    """Fix B (reliability guide, §3 "Fix B"): quota_status now carries a
    per-account "cooling_down" bool (see
    eo.quota_sentinel.get_quota_snapshot()'s docstring) — True only when
    that account's own cooldown_until timestamp is still in the future.
    This is checked SEPARATELY from _usage_fraction()'s 80%-cutoff
    logic below: an account can be well under its daily token quota and
    still be mid-cooldown from a 429 a moment ago (the exact case this
    fix addresses — Groq's "try again in 8m5.568s" being treated as
    "out of tokens until midnight"). Missing quota_status, an unknown
    key, or no recorded cooldown all read as False — fail toward
    "treat it as available," same posture _usage_fraction() already
    takes for missing data."""
    if not quota_status:
        return False
    return bool(quota_status.get(key_env, {}).get("cooling_down"))


def _sorted_by_quota(candidates: list, quota_status: dict) -> list:
    return sorted(candidates, key=lambda k: _usage_fraction(k, quota_status))


def staff_task(classification: dict, quota_status: dict = None,
                task_text: str = None, session_id: str = None) -> list:
    """
    Takes the synthesized classification from run_panel() (or the
    Inspector's own draft, if no escalation was needed) and returns a
    list of hiring decisions:
        [{"role": "implementer", "agent_key": "CEREBRAS_CODE_1",
          "brief": "..."}, ...]

    Migration Part 6 §2: quota_status now auto-fetches the live snapshot
    from eo/quota_sentinel.py when the caller doesn't supply one — every
    existing call site (loop_v4.py, task_runner.py) calls staff_task(draft)
    with quota data omitted, so this makes that omission mean "use live
    quota data" instead of "run with no quota awareness at all."

    Migration Part 7 §2.1: task_text and session_id (2 NEW params) —
    task_text lets _get_or_write_role_prompt() write a real, on-topic
    brief the first time a genuinely new role shows up; session_id lets
    that brief-writing call emit agent_start/agent_done events on the
    caller's own live channel instead of nowhere.

    Part 2 §2.3 note: this function needs ZERO changes to support
    workflow-template-driven hiring — a template's saved roles list is
    handed in as classification["suggested_agents"] (via
    eo.structure.classification_from_template()) exactly like a normal
    Inspector/Panel classification would be. Which roles to hire and how
    to pick an account for each are orthogonal concerns.
    """
    if quota_status is None:
        quota_status = get_quota_snapshot()
    suggested_agents = classification.get("suggested_agents", [])
    hires = []
    for role_name in suggested_agents:
        candidate = _best_match(role_name, quota_status)
        if candidate is None:
            continue
        brief = _get_or_write_role_prompt(
            role_name, task_text or classification.get("reasoning", ""),
            session_id=session_id, tier=classification.get("tier"),
        )
        # Part 2 §2.2 follow-through: this was flagged as a one-line
        # addition to make once staff_task() was back in scope --
        # counts every real hire so the Role Library UI's times_hired
        # reflects actual usage, not just brief-writing events.
        record_role_hire(role_name)
        hires.append({"role": role_name, "agent_key": candidate, "brief": brief})
    return hires


def _best_match(role_name: str, quota_status: dict = None, exclude: set = None) -> str:
    """
    Migration Part 6 §1: prefer the best expertise (natural_roles) match
    and KEEP using it as long as it's under 80% of its daily quota. Only
    switch away once it crosses that line. If NO account's natural_roles
    lists this role at all, that's not a failure — fall through to the
    full account pool and pick purely by quota, since any capable
    account can attempt a role it just isn't specially tagged for.

    Migration Part 2 §2.6: exclude (optional) — a set of account keys to
    skip entirely, regardless of quota headroom. Added for
    eo/executor.py's escalation-retry path: when a role gets sent back
    for a "recheck" (its own output looked weak), the retry should land
    on a different account than the one that just produced that weak
    output, not bounce back to the identical account for an identical
    answer. None (default) excludes nothing, so every existing caller
    (staff_task(), which never passes this) is completely unaffected.
    """
    exclude = exclude or set()
    # Fix B: cooling-down accounts are filtered out up front, alongside
    # `exclude` — a 429'd account is unusable for the same practical
    # reason an explicitly-excluded one is (this call would just fail
    # again), it just recovers on its own once cooldown_until passes
    # instead of needing a new caller decision each time.
    natural_candidates = [
        key for key, info in AGENT_CAPABILITIES.items()
        if role_name in info.get("natural_roles", []) and key not in exclude
        and not _is_cooling_down(key, quota_status)
    ]

    if natural_candidates:
        under_cutoff = [c for c in natural_candidates if _usage_fraction(c, quota_status) < QUOTA_CUTOFF]
        if under_cutoff:
            # One or more natural matches still have headroom — use the
            # least-used one among THEM as a simple tiebreaker (not a
            # bouncing mechanism, since all of them are still "good
            # enough" on expertise grounds; this only matters when a
            # role has more than one natural-match account).
            return _sorted_by_quota(under_cutoff, quota_status)[0]
        # every natural match is at/over 80% — deliberately fall through
        # to the full pool below rather than returning None or forcing
        # an over-quota account.

    # No natural match at all, OR every natural match is maxed out:
    # choose from every provisioned account (minus exclude and any
    # currently cooling down), ranked purely by quota.
    all_candidates = [
        k for k in AGENT_CAPABILITIES.keys()
        if k not in exclude and not _is_cooling_down(k, quota_status)
    ]
    if not all_candidates:
        # Fix B: every non-excluded account is currently cooling down —
        # a temporary state, unlike being excluded or the pool being
        # genuinely empty. Prefer falling back to "excluded accounts
        # allowed, cooldown still respected" over ignoring cooldown
        # outright, since a cooldown account excluded here is still
        # about to become usable again on its own.
        all_candidates = [k for k in AGENT_CAPABILITIES.keys() if not _is_cooling_down(k, quota_status)]
    if not all_candidates:
        # Every account is either excluded, cooling down, or
        # AGENT_CAPABILITIES is itself empty. If it's one of the first
        # two (e.g. only one account is provisioned at all, and it just
        # 429'd), fall back to considering it anyway: a repeated,
        # still-cooling account is still better than failing the retry
        # outright. If AGENT_CAPABILITIES is genuinely empty, that's a
        # real configuration problem.
        all_candidates = list(AGENT_CAPABILITIES.keys())
        if not all_candidates:
            return None
    ranked = _sorted_by_quota(all_candidates, quota_status)
    under_cutoff = [c for c in ranked if _usage_fraction(c, quota_status) < QUOTA_CUTOFF]
    return under_cutoff[0] if under_cutoff else ranked[0]
    # ^ if literally every account is over 80%, still return the least-bad
    #   option rather than failing the hire entirely — a degraded account
    #   is better than no agent at all for a task that needs one.

if __name__ == "__main__":
    fake_draft = {
        "tier": 2, "directed_task_type": "debug", "confidence": 0.6,
        "suggested_agents": ["reviewer", "fixer_pool"], "reasoning": "looked like a bug report",
    }
    print(json.dumps(run_panel("something ambiguous", fake_draft), indent=2))