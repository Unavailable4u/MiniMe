"""
agents/code_writers.py — Code Writer Pool (Part 4, agent #3 of the v5
Master Blueprint).

Rewritten from the old OpenRouter/sequential version to match the blueprint
exactly:
- Provider: Cerebras only (no OpenRouter anywhere in this system).
- Concurrency: up to 5 modules written genuinely in parallel via
  ThreadPoolExecutor, one worker per Cerebras key (CEREBRAS_API_KEY_1..5).
- Model rotation: each worker cycles through 3 models in order if one
  fails -- gpt-oss-120b -> qwen-3-235b-a22b-instruct-2507 -> llama-4-scout-17b
  -- staying on its own assigned key throughout (workers don't share keys).

If there are more than 5 modules in a cycle, keys are reused round-robin
(modules 6, 7... share a key with modules 1, 2...) rather than failing --
an edge case the blueprint doesn't explicitly cover, but sharing beats
crashing.

Stage 6 step 6 addition: model rotation now goes through utils.llm_client's
generate_text() instead of a hand-rolled Cerebras client + retry loop, so
each worker's calls get usage-logged and fire usage_update events, same
as code_writer_lean.py. session_id/tier are optional passthroughs from
run() -- leaving them unset keeps behavior identical to before.
"""

import os
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from relay.emitter import emit_event
from utils.llm_client import generate_text
from eo.worker_pool import _select_workers as _select_workers_for_role
from eo.output_guard import validate_module_code

load_dotenv()

# Rotation order per worker -- if the first model errors transiently, the
# same worker (same key) tries the next one before giving up.
#
# NOTE (updated): Cerebras's public-endpoint catalog only guarantees
# gpt-oss-120b as a production model as of mid-2026; the qwen-3-235b and
# llama-4-scout models this list previously rotated to have been removed
# from the public endpoint (they now 404). zai-glm-4.7 and gemma-4-31b are
# the current preview-tier models -- preview means "may be pulled on short
# notice," so re-check https://inference-docs.cerebras.ai/models/overview
# if this rotation starts 404-ing again.
MODELS = [
    "gpt-oss-120b",
    "zai-glm-4.7",
    "gemma-4-31b",
]

# Migration Part 9 §2: replaces the old static KEY_ENVS + RESERVE_KEY_ENVS
# lists (which kept reserve accounts idle outside Expert/Beast) with a
# registry-driven pool, ranked by live quota every run. Mode now only
# controls how many of the pool get used AT ONCE (see run()), not who's
# eligible to be picked -- base and reserve accounts are always both in
# the fairness rotation.
ROLE_TAG = "implementer"


def _select_workers(worker_count: int, key_override=None, session_id: str = None) -> list:
    """Thin wrapper over eo/worker_pool.py's shared, role_tag-parameterized
    selection (Part 6 §6.2 extraction). Byte-for-byte the same fairness
    rotation this module always had — _eligible_pool()/_select_workers()
    used to be defined here directly; they now live in eo/worker_pool.py
    so agents/content_adapter_pool.py can reuse the exact same logic
    instead of a copy-pasted second implementation. Kept as a local
    wrapper (rather than rewriting every call site below to import
    _select_workers_for_role directly) so this file's own run() doesn't
    need to change at all.

    session_id: NEW — CO4 patch 3, optional passthrough to the shared
    selector's own "worker_pool_selection" event — see that function's
    docstring. agent_name is fixed to ROLE_TAG here since that's the
    only role this wrapper ever selects for."""
    return _select_workers_for_role(ROLE_TAG, worker_count, key_override,
                                     session_id=session_id, agent_name=ROLE_TAG)


# Fix 2 (reliability audit, follow-up to Fix 1): the chain built in
# _write_one_module() below only ever rotates MODELS on ONE fixed
# Cerebras key -- it never tries a different key, let alone a different
# provider. If that one Cerebras account is rate-limited/exhausted (even
# with Fix 1 making generate_text() correctly treat that as transient),
# there was nothing else in the chain to fall through to.
#
# This appends up to EXTRA_FALLBACK_STEPS more chain steps drawn from
# OTHER accounts -- any provider -- using the exact same ranked-by-quota
# selection eo/panel.py's _best_match() already does for ordinary
# hiring, so a Cerebras-wide outage no longer fails this worker outright.
#
# Imports are deferred inside the function, not at module level, on
# purpose: eo/registry.py imports agents.code_writers at load time (same
# as agents.generic_worker), and eo.panel/agents.generic_worker each
# import back from eo.registry -- an module-level import here would
# close that same circular loop agents/generic_worker.py's own docstring
# already flags and avoids the same way.
EXTRA_FALLBACK_STEPS = 2


def _extra_fallback_chain_steps(primary_key_env: str, already_used: set = None) -> list:
    from eo.panel import _best_match
    from eo.quota_sentinel import get_quota_snapshot
    from agents.generic_worker import _chain_step_for

    quota_status = get_quota_snapshot()
    # already_used (NEW): the keys _model_rotation_chain() below already
    # spent on this worker's own MODELS rotation, so this tail doesn't
    # re-offer one of those as a "different" account.
    exclude = {primary_key_env} | (already_used or set())
    steps = []
    for _ in range(EXTRA_FALLBACK_STEPS):
        candidate = _best_match(ROLE_TAG, quota_status, exclude=exclude)
        if candidate is None:
            break
        exclude.add(candidate)
        steps.append(_chain_step_for(candidate))
    return steps


# Fix 3 (reliability audit, follow-up to Fix 2) -- follows the exact bug
# code_writer_lean.py's 2026-08-12 fix already found and fixed in the
# tier-1 pipeline (see that file's docstring): the MODELS rotation below
# used to read the SAME key_env for all three model names, so three
# "different" models were really one Cerebras quota bucket -- a single
# 429/cooldown on that one account skipped all three at once. That's the
# "code_writer_1 ... skipped -- still cooling down" trio you get in the
# logs when CEREBRAS_API_KEY_1 gets rate-limited.
#
# code_writer_lean.py could just hardcode three sibling keys
# (CEREBRAS_API_KEY_1/_2/_3) because it's a fixed single-worker pipeline.
# This file can't do that the same way: `key_env` here is whichever
# account eo/worker_pool.py's fairness rotation handed THIS worker for
# THIS run, so a second hardcoded key could just as easily collide with
# a DIFFERENT worker's own assigned key running concurrently in the same
# ThreadPoolExecutor. Instead, each step after the first asks the same
# quota-/cooldown-aware picker _extra_fallback_chain_steps() above
# already uses for another live account from the SAME natural-roles pool
# (implementer-tagged Cerebras keys only, so the model names stay
# accurate to what's actually being called) -- falling back to
# `primary_key_env` only once that pool is genuinely exhausted, which
# just degrades to the old (safe, if not independent) behavior instead
# of raising.
def _model_rotation_chain(primary_key_env: str) -> list:
    from eo.panel import _is_cooling_down, _sorted_by_quota
    from eo.quota_sentinel import get_quota_snapshot
    from eo.registry import AGENT_CAPABILITIES

    quota_status = get_quota_snapshot()
    cerebras_pool = [
        k for k, info in AGENT_CAPABILITIES.items()
        if info.get("provider") == "cerebras" and ROLE_TAG in info.get("natural_roles", [])
    ]
    exclude = {primary_key_env}
    key_envs = [primary_key_env]
    for _ in range(len(MODELS) - 1):
        candidates = [
            k for k in cerebras_pool
            if k not in exclude and not _is_cooling_down(k, quota_status)
        ]
        if not candidates:
            key_envs.append(primary_key_env)  # pool exhausted -- degrade, don't crash
            continue
        candidate = _sorted_by_quota(candidates, quota_status)[0]
        exclude.add(candidate)
        key_envs.append(candidate)
    return [{"provider": "cerebras", "model": m, "key_env": k} for m, k in zip(MODELS, key_envs)]


SYSTEM_PROMPT = """You are a focused implementer. Write complete, runnable Python code
for the module described below. Follow the spec exactly. Include basic input validation.
Do not invent features outside the spec. Output ONLY the code, no explanation, no markdown
code fences."""

# Part 8.5 simplicity constraint, verbatim from code_writer_lean.py (kept as
# a separate constant, duplicated rather than imported, so the two agents'
# prompts don't accidentally couple). Applied ONLY at tier 2 -- see
# _write_one_module(). Tier 3 (the full 5-worker roster) must NOT get this:
# per code_writer_lean.py's docstring, large multi-module tier-3 projects
# sometimes legitimately need adapter/service layers that a tier-2 directed
# refactor against a small existing app never does.
SIMPLICITY_CONSTRAINT = """

For small, self-contained modules, write the simplest correct \
implementation. Do not introduce adapter, bridge, or service-indirection \
layers unless the spec explicitly calls for integrating with an external \
system. A single file solving the stated problem is preferred over \
multiple files that only forward calls to each other."""


def _strip_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = code.split("```")[1]
        if code.startswith("python"):
            code = code[6:]
        code = code.strip()
    return code


def _write_one_module(module_spec: dict, key_env: str, worker_id: int,
                       session_id: str = None, path: str = None,
                       domain: str = None, design_approach: str = None) -> tuple[str, str]:
    """
    Runs on one worker thread with one fixed Cerebras key. Tries each model
    in MODELS, in order, staying on this same key throughout, via
    generate_text() (so usage gets logged). Returns (module_name, code).

    worker_id is this worker's key-slot number (1-5) for labeling only --
    it's not unique per module when there are more than 5 modules and keys
    get reused round-robin, which is intentional: it's the same worker/key
    doing a second module, not a 6th worker appearing.

    design_approach: Patch 8 (rollout guide §3) — logic_architect's own
    stage_output, if that role ran earlier in this plan (see run()'s own
    read of it below). Optional so every existing caller that hires
    "implementer" WITHOUT "logic_architect" ahead of it keeps working
    exactly as before with no design_approach field in the spec at all.
    """
    name = module_spec.get("name", "?")
    agent_name = f"code_writer_{worker_id}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Code Writer {worker_id} — {name}"})
    started = time.monotonic()

    def _done(code: str) -> tuple[str, str]:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = code if len(code) <= 300 else code[:300] + "..."
        emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
                   payload={"summary": summary, "duration_ms": duration_ms})
        return name, code

    # Fix 3: each MODELS step now gets its own live Cerebras account where
    # possible, instead of all three sharing key_env's single quota bucket
    # (see _model_rotation_chain()'s docstring above).
    chain = _model_rotation_chain(key_env)
    # Fix 2: extend past the model rotation with a few steps on OTHER
    # accounts/providers (see _extra_fallback_chain_steps() above),
    # instead of failing as soon as every key the rotation touched is
    # exhausted.
    chain += _extra_fallback_chain_steps(key_env, already_used={step["key_env"] for step in chain})
    spec_for_prompt = dict(module_spec)
    if design_approach:
        # Patch 8 — folded into the SAME per-module JSON prompt payload
        # implementer already gets (not a separate message, not the whole
        # conversation-context prepend generic_worker roles get), so the
        # design decision actually reaches the code being written instead
        # of just sitting in the registry/execution-order metadata. Every
        # module in this spec's list gets the SAME design_approach text —
        # logic_architect designs at the task/plan level (see its own
        # ROLE_PROMPTS_SEED brief), not a separate approach per module.
        spec_for_prompt["design_approach"] = design_approach
    user_content = json.dumps(spec_for_prompt)

    # Tier 2 == a directed refactor against a small existing app (router.py's
    # DIRECTED_TASK_MAP), same spirit as tier-1/tier-0 -- gets the simplicity
    # constraint. Tier 3 == the full 19-agent loop building bigger, more
    # versatile projects -- keeps the bare prompt, unchanged from before.
    # Migration Part 26 fix: this used to check `tier == 2`, but callers
    # (eo/executor.py) now pass the string `path`, not the old int `tier`.
    # Per PATH_TO_TIER = {"instant": 0, "direct": 1, "fixed": 2, "adaptive": 3}
    # (eo/panel.py, eo/loop_v4.py), tier 2's path label is "fixed" -- so this
    # check is now on path, not a bare int comparison that would silently
    # never match again.
    system_prompt = SYSTEM_PROMPT
    if path == "fixed":
        system_prompt += SIMPLICITY_CONSTRAINT

    try:
        raw = generate_text(
            system_prompt,
            user_content,
            chain,
            agent_name=agent_name,
            session_id=session_id,
            path=path,  # Migration Part 27 §1: generate_text() now accepts `path` for real
            domain=domain,  # Migration Part 2 §2.6: cost-tracking gap
        )
        code = _strip_fences(raw)
        if not code:
            code = f"# CODE WRITER FAILED: model returned empty content. No code generated for module '{name}'."
    except RuntimeError as exc:
        code = f"# CODE WRITER FAILED: {exc}"

    return _done(code)


def _derive_specs_from_task_text(task_text: str, session_id: str = None,
                                  domain: str = None) -> dict:
    """Fallback spec synthesis for when this module gets hired directly by
    the tier-3 adaptive Panel WITHOUT the legacy "prompt_writer" role
    ahead of it in the plan. This module's original v5 contract assumed
    prompt_writer.run() had always already written KEYS["module_specs"]
    before this ran — a fine assumption for the old fixed 19-agent
    pipeline, not a safe one for the Panel's hires-driven pipeline (Part
    10), which is free to pick any subset of roles. Rather than crashing
    with a bare TypeError on `specs["modules"]` (the exact bug this fixes
    — a hired "implementer" with no "prompt_writer" upstream left
    module_specs unwritten -> None -> not subscriptable), ask the same
    kind of single-shot spec question prompt_writer.py asks, seeded
    directly from the raw task text, and write the result to the same key
    so any later reader still finds it there.
    """
    from utils.llm_client import generate_text
    spec_prompt = """You are a technical spec writer. Given a task description, break it
into 1-3 independent modules that can be built in parallel without depending on each
other's internal code (only on their defined interface). If the task is small enough
to be one module, return just one.

Output ONLY a JSON object with a "modules" key containing a list. Each module must have:
- "name": short module name
- "description": what it does
- "inputs": expected inputs
- "outputs": expected outputs
- "edge_cases": list of edge cases to handle

Respond with ONLY valid JSON, no markdown, no explanation."""
    # Quota-reality fix, §4 (2026-07-30): GitHub Models retired in full --
    # its fallback step is removed here, not replaced.
    chain = [
        {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    ]
    try:
        raw_text = generate_text(
            spec_prompt, f"Task: {task_text}", chain,
            agent_name="Code Writers (spec fallback)", session_id=session_id,
            domain=domain,  # Migration Part 2 §2.6: cost-tracking gap
        ).strip()
        if raw_text.startswith("```"):
            raw_text = raw_text.split("```")[1]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
            raw_text = raw_text.strip()
        specs = json.loads(raw_text)
        if not specs.get("modules"):
            raise ValueError("empty modules list")
    except Exception:
        # Last-resort single module so this never hard-crashes even if
        # the fallback LLM call itself fails -- one module named after
        # the task is always a valid, if unrefined, plan.
        specs = {"modules": [{
            "name": "main",
            "description": task_text or "Implement the requested task.",
            "inputs": "see description",
            "outputs": "see description",
            "edge_cases": [],
        }]}
    write(KEYS["module_specs"], specs)
    return specs


def run(session_id: str = None, path: str = None, expanded: bool = False,
        key_override=None, task_text: str = None, domain: str = None):
    """
    Migration Part 5 §2.3 — key_override, if given, is the Panel's specific
    account-selection decision for this hire (eo.router's
    build_execution_graph_from_hires(), threaded through by
    eo.executor.execute_graph()):

    key_override: None (default) -> today's exact behavior, this module
        picks its own worker keys from KEY_ENVS/RESERVE_KEY_ENVS via
        _worker_keys(expanded), as it always has.
    key_override: a single key_env string -> use ONLY that account for
        every module in this call (the Panel decided one specific,
        under-quota Cerebras account should do this hire's work).
    key_override: a list of key_env strings -> use exactly those accounts
        as the parallel worker pool for this call, instead of
        _worker_keys(expanded) -- this is what a multi-hire "implementer"
        staffing decision (build_execution_graph_from_hires()'s
        list-handling) turns into.

    task_text: NEW — bug fix. Only used as a fallback seed for
    _derive_specs_from_task_text() when KEYS["module_specs"] hasn't been
    written yet (see that function's docstring). Optional so every
    existing caller that doesn't pass it keeps working exactly as before
    whenever prompt_writer DID already run.
    """
    specs = read(KEYS["module_specs"])
    if not specs or not specs.get("modules"):
        specs = _derive_specs_from_task_text(task_text, session_id=session_id, domain=domain)
    modules = specs["modules"]
    results = {}

    # Patch 8 (rollout guide §3) — logic_architect is a tag-driven
    # generic_worker role (eo/registry.py's AGENT_CAPABILITIES), so its
    # output lands at stage_output:{session_id}:logic_architect
    # (agents/generic_worker.py's own convention), NOT one of the legacy
    # v5 bus keys this module's other reads use. None (default) if
    # logic_architect wasn't hired ahead of "implementer" in this plan,
    # or session_id itself is unset (e.g. the __main__ block below) —
    # either way _write_one_module() below treats a missing
    # design_approach exactly like today's behavior, unchanged.
    design_approach = read(f"stage_output:{session_id}:logic_architect", default=None) if session_id else None

    worker_count = 8 if expanded else 5
    key_envs = _select_workers(worker_count, key_override, session_id=session_id)

    with ThreadPoolExecutor(max_workers=len(key_envs)) as executor:
        futures = {
            executor.submit(
                _write_one_module, module, key_envs[i % len(key_envs)],
                (i % len(key_envs)) + 1, session_id=session_id, path=path,
                domain=domain, design_approach=design_approach,
            ): module
            for i, module in enumerate(modules)
        }
        for future in as_completed(futures):
            name, code = future.result()
            # D3 Part 2: guard the module code before it's trusted downstream
            # by sandbox_tester.py. Fail-open, matching _write_one_module()'s
            # own pattern above -- a validation failure marks this module
            # failed rather than crashing the whole run.
            is_valid, reason = validate_module_code(code)
            if not is_valid:
                print(f"    [Code Writer] module '{name}' failed output validation ({reason}); marking as failed")
                code = f"# CODE WRITER FAILED: output validation failed ({reason})"
            results[name] = code
            print(f"    [Code Writer] wrote module: {name} ({len(code)} chars)")

    write(KEYS["submitted_code"], results)
    return results


if __name__ == "__main__":
    results = run()
    for name, code in results.items():
        print(f"\n=== {name} ===")
        print(code[:300] + ("..." if len(code) > 300 else ""))