"""
evals/promptfoo/providers/role_provider.py — D2 patch 2.

A promptfoo Python provider (https://www.promptfoo.dev/docs/providers/python/)
that hires a role's brief from the SAME store the app hires from
(eo.registry.get_role_prompt -> Upstash Redis, key "registry:role_prompts",
seeded from ROLE_PROMPTS_SEED) and replays it through the SAME
utils/llm_client.generate_text() chain agents/generic_worker.py's run()
uses -- not a reimplementation, not a hardcoded prompts: list. See this
package's README ("Why this isn't just YAML pointing at static prompt
files") for the reasoning.

SCOPE OF THIS PROVIDER (read before adding a role's test file):
Patch 2b traced every eo.registry.REAL_ACTION_ROLES module (reviewer.py,
fixer_pool.py, code_writers.py, structure_architect.py, the tier-1 lean
trio, all 20 dedicated modules as of this writing) rather than guessing
from the guide. Finding: NONE of them call get_role_prompt() at all --
each has its own hardcoded SYSTEM_PROMPT/CHAIN module constant (the lean
trio's is the most explicit example, with a comment noting they
"bypass eo/registry.py's build_fallback_chain() by design"). So this
isn't a case of "same live brief, different chain" the way patch 2's
docstring assumed -- REAL_ACTION_ROLES roles simply aren't Role-Library
-driven at all, and D2's whole "briefs live in Redis, not files" premise
(this package's README) doesn't apply to them. There is no live chain
for this provider to replay for these roles, so _reject_real_action_role()
below turns that into a clear, actionable error instead of silently
evaluating the wrong brief/chain against them. Point tests/*.yaml at
generic_worker-routed roles (anything in ROLE_PROMPTS_SEED and NOT in
REAL_ACTION_ROLES) -- that's every role this provider can meaningfully
test.

promptfoo Python provider contract: this module must expose a top-level
call_api(prompt, options, context) -> dict with an "output" (success) or
"error" (failure) key. See:
https://www.promptfoo.dev/docs/providers/python/
"""
import logging
import os
import sys
import uuid

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))  # .../backend, so `eo`/`agents`/`utils`/`memory` import cleanly

logger = logging.getLogger("promptfoo.role_provider")
logging.basicConfig(level=logging.INFO, format="[role_provider] %(levelname)s: %(message)s")

# Patch 9, resolving the TODO(patch 2b) below: confirmed against
# upstash_redis==1.7.0's own installed source (http.py) rather than
# guessed. The two failure modes the TODO wanted separated really do
# raise differently distinguishable exceptions:
#   - Connectivity failures (DNS, timeout, connection refused) never
#     get a response back at all -- SyncHttpClient.execute() re-raises
#     whatever httpx raised (e.g. httpx.ConnectError, httpx.TimeoutException,
#     httpx.ReadTimeout -- all subclasses of httpx.HTTPError/httpx.TransportError).
#   - A bad/rotated token DOES get a response (Upstash's REST API
#     returns a 200 with an {"error": ...} JSON body, not a raised
#     HTTP-status exception) -- http.py's format_response() checks
#     response.get("error") and raises upstash_redis.errors.UpstashError,
#     a distinct, importable class.
# So catching UpstashError separately from the transport-level
# exception classes is a real, confirmed distinction, not a guess at
# an unstable exception shape.
from upstash_redis.errors import UpstashError  # noqa: E402


def _reject_real_action_role(role_name: str) -> str | None:
    """Returns an error string if `role_name` resolves to a dedicated
    module (a REGISTRY entry whose callable isn't generic_worker.run)
    with its own hardcoded prompt/chain, not something get_role_prompt()
    can meaningfully fetch. Returns None for anything else -- including
    roles that only *bootstrap* a brief and then still dispatch through
    generic_worker.run() (e.g. fact_detector.py), which is fine.

    NOTE: eo.registry.REAL_ACTION_ROLES is NOT the right lookup for this
    -- its keys are classifier-vocabulary labels the Inspector/Panel use
    ("verifier", "implementer", ...), not the literal role_name a test
    case or generic_worker.run() would pass ("reviewer", "code_writers",
    ...); those show up only as REAL_ACTION_ROLES' *values*. eo.registry
    .REGISTRY is the actual name -> callable dispatch table and is keyed
    the right way -- confirmed by checking both dicts directly against
    a running import, not assumed from the guide."""
    from eo.registry import REGISTRY
    from agents.generic_worker import run as generic_worker_run

    entry = REGISTRY.get(role_name)
    if entry is not None and entry["callable"] is not generic_worker_run:
        return (
            f"role_provider: '{role_name}' resolves to a dedicated module "
            f"(agents/{entry['callable'].__module__.rsplit('.', 1)[-1]}.py), "
            f"not generic_worker. Its real prompt and chain are hardcoded "
            f"module constants, not something get_role_prompt() can fetch "
            f"-- there is no live brief for this provider to test. See "
            f"this file's module docstring (SCOPE section) for how this "
            f"was confirmed. Point this test case at a generic_worker-"
            f"routed role instead (in eo.registry.REGISTRY, either absent "
            f"entirely or mapped to generic_worker.run)."
        )
    return None


def _get_brief_live_or_seed(role_name: str) -> tuple[str | None, str]:
    """Returns (brief, source) where source is "live" or "seed". Never
    raises -- a promptfoo eval run shouldn't die because Redis is
    unreachable from CI (see README's "Running in CI" section). Falls
    back to ROLE_PROMPTS_SEED and logs a warning rather than failing the
    case outright, so CI without UPSTASH_* secrets still runs, just
    against seed briefs instead of whatever's live."""
    from eo.registry import get_role_prompt, ROLE_PROMPTS_SEED

    try:
        brief = get_role_prompt(role_name)
        if brief is not None:
            return brief, "live"
        # Reachable, but this role has genuinely never been briefed.
        # Don't silently fall through to seed here -- that would mask a
        # real "this role_name is wrong" bug as a false pass.
        seed_brief = ROLE_PROMPTS_SEED.get(role_name)
        if seed_brief is not None:
            logger.warning(
                "Role '%s' has no live brief in the store; using ROLE_PROMPTS_SEED "
                "(this is expected for a role that's never been hired yet).", role_name)
            return seed_brief, "seed"
        return None, "live"
    except UpstashError as exc:
        # A response DID come back -- Upstash itself rejected the
        # request, almost always a bad/rotated REST token (see this
        # file's top-of-file comment for how this is distinguished from
        # a connectivity failure). Still falls back to seed rather than
        # hard-failing every case in the run -- an eval run shouldn't
        # die over this either -- but logged at ERROR, not warning, and
        # tagged with a distinct source string so it's visible at a
        # glance in promptfoo's own output (call_api()'s metadata.brief_source)
        # instead of looking identical to ordinary Redis flakiness.
        seed_brief = ROLE_PROMPTS_SEED.get(role_name)
        logger.error(
            "Upstash rejected the request for '%s' (%s: %s) -- this looks like a "
            "bad or rotated CI_UPSTASH_REDIS_REST_TOKEN, not a connectivity issue. "
            "Falling back to ROLE_PROMPTS_SEED, but check the token.", role_name,
            type(exc).__name__, exc)
        return seed_brief, "seed-auth-error"
    except Exception as exc:
        # Genuine connectivity failure (DNS, timeout, connection refused,
        # etc. -- httpx transport-level exceptions never got a response
        # to parse into an UpstashError at all). This is the case CI
        # without reachable Upstash should degrade gracefully from, so
        # it stays a warning and the original "seed" source string.
        seed_brief = ROLE_PROMPTS_SEED.get(role_name)
        logger.warning(
            "Could not reach the Role Library store for '%s' (%s: %s). "
            "Falling back to ROLE_PROMPTS_SEED.", role_name, type(exc).__name__, exc)
        return seed_brief, "seed"


def _build_live_chain(role_name: str) -> list:
    """Reuses agents/generic_worker.py's own chain-building, unmodified
    -- the same functions its run() calls when no key_override/
    chain_override is given. Falls back to a flat, unranked pool (no
    quota-snapshot ranking, no cooldown-awareness) if the quota snapshot
    itself can't be read (same Redis-unreachable case as the brief
    lookup above), rather than raising and failing every test case in
    a CI run with no Upstash secrets configured."""
    from agents.generic_worker import (
        _build_fallback_chain, _chain_step_for, _dynamic_max_chain_steps,
    )
    from eo.quota_sentinel import get_quota_snapshot
    from eo.registry import AGENT_CAPABILITIES

    try:
        quota_status = get_quota_snapshot()
        chain_keys = _build_fallback_chain(
            role_name, quota_status, max_steps=_dynamic_max_chain_steps(quota_status))
        if chain_keys:
            return [_chain_step_for(k) for k in chain_keys]
        logger.warning(
            "_build_fallback_chain('%s') returned no candidates; falling back to "
            "an unranked pool of every account tagged for this role.", role_name)
    except Exception as exc:
        logger.warning(
            "Could not read a live quota snapshot (%s: %s); falling back to an "
            "unranked account pool instead of quota/cooldown-ranked chain.",
            type(exc).__name__, exc)

    # Degraded fallback: prefers accounts whose natural_roles/strengths
    # tag this role, in AGENT_CAPABILITIES dict order, capped the same
    # as the live path's MAX_CHAIN_STEPS default (3). No quota ranking,
    # no cooldown-awareness, no provider-spreading -- just enough to
    # make the case runnable without a reachable Redis.
    #
    # D2 patch 7: falls through to the FULL account pool when NO
    # account tags this role at all, mirroring eo/panel.py::
    # _best_match()'s own fallback ("No natural match at all ... choose
    # from every provisioned account"). This fallthrough was previously
    # missing here -- confirmed while wiring CI (patch 6): any role
    # nobody has gotten around to natural_roles-tagging (e.g.
    # "contradiction_detector", a genuine generic_worker judgment role
    # -- see eo/registry.py's comment on its deliberate REAL_ACTION_ROLES
    # omission) returned an EMPTY chain here whenever Redis wasn't
    # reachable, even though production's own _best_match() would have
    # happily picked from the general pool for the exact same role. In
    # CI that surfaced as every case for an untagged role erroring with
    # "no usable account found" -- indistinguishable at a glance from a
    # real prompt regression. Still deliberately NOT raising/tightening
    # on an empty pool overall: an eval run without a reachable Redis
    # should degrade, not crash outright; see the call_api() empty-chain
    # error path below for what happens if AGENT_CAPABILITIES itself is
    # empty (a configuration problem, not a role-name problem).
    from agents.generic_worker import MAX_CHAIN_STEPS
    candidates = [key for key, info in AGENT_CAPABILITIES.items()
                  if role_name in info.get("natural_roles", [])]
    if not candidates:
        logger.warning(
            "No account's natural_roles tags '%s'; falling through to the full "
            "AGENT_CAPABILITIES pool (unranked) instead of returning an empty "
            "chain -- same fallthrough eo/panel.py::_best_match() uses in "
            "production for an untagged role.", role_name)
        candidates = list(AGENT_CAPABILITIES.keys())
    return [_chain_step_for(k) for k in candidates[:MAX_CHAIN_STEPS]]


# --------------------------------------------------------------------
# D2 patch 5 -- provider head-to-head comparison support.
#
# compare/providers.promptfooconfig.yaml configures one `providers:`
# entry per candidate LLM provider, each pointing at THIS SAME file
# with a different `provider_override` in its config. When set,
# call_api() below builds a single forced one-step chain instead of
# calling _build_live_chain()'s quota-ranked live chain -- that's what
# lets the identical role/task run against Groq vs Cerebras vs Mistral
# vs Gemini side by side in one promptfoo run (promptfoo's normal
# test-case x provider matrix does the actual comparison automatically
# once multiple `providers:` entries exist; nothing special needed on
# that side).
#
# Model/key_env pairs below are copied directly from real production
# chains already in this codebase, not guessed -- utils/llm_client.py's
# own module docstring for groq; agents/documentation_agent.py for
# mistral; agents/code_writer_lean.py for gemini; agents/dataset_analyst
# .py / agents/report_writer.py / agents/idea_planner.py (among others)
# for cerebras -- so a comparison run exercises models this codebase
# actually calls in production, not a placeholder that might not even
# be enabled on your account.
#
# Cloudflare and HuggingFace are deliberately NOT included here: both
# need a different step shape than the other four (Cloudflare needs
# "account_id_env" + "token_env" instead of a single "key_env" -- see
# utils/llm_client.py's own module docstring). Add either the same way
# if you want them in the comparison, using their real step shape
# rather than forcing them through this key_env-only dict.
PROVIDER_OVERRIDE_DEFAULTS = {
    "groq":     {"provider": "groq",     "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    "cerebras": {"provider": "cerebras", "model": "gpt-oss-120b",            "key_env": "CEREBRAS_API_KEY_1"},
    "mistral":  {"provider": "mistral",  "model": "mistral-medium-latest",   "key_env": "MISTRAL_API_KEY"},
    "gemini":   {"provider": "gemini",   "model": "gemini-3.6-flash",        "key_env": "GEMINI_API_KEY_1"},
}
# --------------------------------------------------------------------


def call_api(prompt: str, options: dict, context: dict) -> dict:
    """promptfoo entry point. `context["vars"]` carries each test case's
    `vars:` block from the YAML. Required var: role_name. task_text
    falls back to the rendered `prompt` string itself so a case can omit
    it and just template task_text straight into promptfooconfig's
    `prompts:` entry instead."""
    provider_config = (options or {}).get("config", {}) or {}
    test_vars = (context or {}).get("vars", {}) or {}

    role_name = test_vars.get("role_name") or provider_config.get("role_name")
    if not role_name:
        return {"error": "role_provider: test case is missing required var 'role_name'"}

    rejection = _reject_real_action_role(role_name)
    if rejection:
        return {"error": rejection}

    task_text = test_vars.get("task_text") or prompt
    allow_continuation = test_vars.get("allow_continuation", True)

    # Optional `context` var: mirrors agents/generic_worker.py's run(),
    # which prepends each input_keys' prior-stage output (and, when
    # enabled, the session's conversation-memory context) ahead of
    # "TASK: {task_text}" -- see that module's run() for the real
    # version. A promptfoo case can't replay a live session's bus state
    # or conversation history, so this is a static stand-in: pass
    # whatever fixed prior-stage text the case wants the role to see as
    # a plain `context` var, and it's stitched in the same shape. Falls
    # back to just the task when omitted (today's default behavior,
    # unchanged from patch 2).
    extra_context = test_vars.get("context")
    if extra_context:
        task_text = f"--- Prior context ---\n{extra_context}\n\nTASK: {task_text}"
    else:
        task_text = f"TASK: {task_text}"

    brief, brief_source = _get_brief_live_or_seed(role_name)
    if brief is None:
        return {"error": f"role_provider: '{role_name}' has no brief in either the "
                          f"live store or ROLE_PROMPTS_SEED -- check the role_name."}

    # D2 patch 5: an explicit provider_override (test var or provider
    # config) bypasses the quota-ranked live chain entirely in favor of
    # a single forced step -- see PROVIDER_OVERRIDE_DEFAULTS above.
    # Ordinary role-regression cases (patch 3/4, promptfooconfig.yaml)
    # never set this, so their behavior is completely unchanged.
    provider_override = test_vars.get("provider_override") or provider_config.get("provider_override")
    if provider_override:
        forced_step = PROVIDER_OVERRIDE_DEFAULTS.get(provider_override)
        if forced_step is None:
            return {"error": f"role_provider: unknown provider_override "
                              f"'{provider_override}' -- must be one of "
                              f"{sorted(PROVIDER_OVERRIDE_DEFAULTS)} (see this file's "
                              f"PROVIDER_OVERRIDE_DEFAULTS dict to add another)."}
        chain = [forced_step]
    else:
        chain = _build_live_chain(role_name)

    if not chain:
        return {"error": f"role_provider: no usable account found in AGENT_CAPABILITIES "
                          f"for role '{role_name}' (live and degraded-fallback chain "
                          f"building both returned empty)."}

    from utils.llm_client import generate_text

    # A dedicated, clearly-tagged session_id per call -- NOT None and
    # NOT reused across cases. generate_text() only logs usage/fires
    # usage_update when session_id is set (see its own docstring), so
    # this keeps eval runs out of a real user's quota-dashboard history
    # while still being identifiable in Upstash if you go looking. A
    # fixed/shared id across the whole eval run would also work but
    # would make concurrent promptfoo test cases collide in the log.
    eval_session_id = f"promptfoo-eval-{uuid.uuid4().hex[:12]}"

    try:
        output = generate_text(
            system_prompt=brief,
            user_content=task_text,
            chain=chain,
            agent_name=role_name,
            session_id=eval_session_id,
            allow_continuation=allow_continuation,
        )
    except Exception as exc:
        return {"error": f"generate_text() failed for role '{role_name}' using a "
                          f"{len(chain)}-step chain: {type(exc).__name__}: {exc}"}

    return {
        "output": output,
        "metadata": {
            "role_name": role_name,
            "brief_source": brief_source,   # "live" or "seed" -- surfaces in promptfoo's UI
            "chain_providers": [step.get("provider") for step in chain],
            "provider_override": provider_override,  # None for ordinary role-regression runs
        },
    }


# --------------------------------------------------------------------
# Patch 2b resolved (from patch 2's original list):
#   1. Dedicated-module roles -- turned out to need a REJECTION, not a
#      resolver table: traced all 20 REAL_ACTION_ROLES modules and none
#      of them call get_role_prompt() at all (each hardcodes its own
#      SYSTEM_PROMPT/CHAIN). See _reject_real_action_role() and the
#      module docstring's SCOPE section.
#   3. input_keys / conversation-memory context -- added as an optional
#      `context` var (static stand-in for a live session's stitched
#      prior-stage output; see call_api()'s comment for why it can't be
#      a full replay).
#
# Patch 6 (CI wiring, .github/workflows/ci.yml) resolved the two items
# that used to be listed here as "left for patch 6":
#   4. Cost/quota interaction -- CI reuses the same production account
#      pool (GROQ_API_KEY_6-14, GROQ_RESERVE_1/2, SGA_GROQ_1-3, etc.),
#      wired in as job-level secrets rather than separate CI-only keys.
#   5. Per-test timeout -- PROMPTFOO_EVAL_TIMEOUT_MS (120s) /
#      PROMPTFOO_MAX_EVAL_TIME_MS (600s), verified against promptfoo
#      0.122.0's own installed source rather than assumed. Set as job
#      env in ci.yml, not in this file or promptfooconfig.yaml.
#
# Patch 6 also surfaced a real gap that patch 7 (this revision) fixes
# directly in this file, not in CI config: _build_live_chain()'s
# degraded fallback used to return an EMPTY chain for any role with no
# natural_roles tag at all (e.g. contradiction_detector) whenever Redis
# was unreachable -- even though production's _best_match() falls
# through to the full account pool for the exact same case. See the
# comment on that fallthrough in _build_live_chain() above for the full
# story; CI's own UPSTASH-secrets requirement (ci.yml's comment on that
# job) was the workaround before this fix landed, and can now be
# relaxed to the seed-only path the package README originally described
# once this fix has run clean in CI.
# --------------------------------------------------------------------
