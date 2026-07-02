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

# One key per parallel worker slot, per Part 4's "keys #1-#5".
KEY_ENVS = [
    "CEREBRAS_API_KEY_1",
    "CEREBRAS_API_KEY_2",
    "CEREBRAS_API_KEY_3",
    "CEREBRAS_API_KEY_4",
    "CEREBRAS_API_KEY_5",
]

MAX_WORKERS = 5

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
                       session_id: str = None, tier: int = None) -> tuple[str, str]:
    """
    Runs on one worker thread with one fixed Cerebras key. Tries each model
    in MODELS, in order, staying on this same key throughout, via
    generate_text() (so usage gets logged). Returns (module_name, code).

    worker_id is this worker's key-slot number (1-5) for labeling only --
    it's not unique per module when there are more than 5 modules and keys
    get reused round-robin, which is intentional: it's the same worker/key
    doing a second module, not a 6th worker appearing.
    """
    name = module_spec.get("name", "?")
    agent_name = f"code_writer_{worker_id}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, tier=tier,
               payload={"label": f"Code Writer {worker_id} — {name}"})
    started = time.monotonic()

    def _done(code: str) -> tuple[str, str]:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = code if len(code) <= 300 else code[:300] + "..."
        emit_event("agent_done", session_id=session_id, agent=agent_name, tier=tier,
                   payload={"summary": summary, "duration_ms": duration_ms})
        return name, code

    chain = [{"provider": "cerebras", "model": m, "key_env": key_env} for m in MODELS]
    user_content = json.dumps(module_spec)

    # Tier 2 == a directed refactor against a small existing app (router.py's
    # DIRECTED_TASK_MAP), same spirit as tier-1/tier-0 -- gets the simplicity
    # constraint. Tier 3 == the full 19-agent loop building bigger, more
    # versatile projects -- keeps the bare prompt, unchanged from before.
    system_prompt = SYSTEM_PROMPT
    if tier == 2:
        system_prompt += SIMPLICITY_CONSTRAINT

    try:
        raw = generate_text(
            system_prompt,
            user_content,
            chain,
            agent_name=agent_name,
            session_id=session_id,
            tier=tier,
        )
        code = _strip_fences(raw)
        if not code:
            code = f"# CODE WRITER FAILED: model returned empty content. No code generated for module '{name}'."
    except RuntimeError as exc:
        code = f"# CODE WRITER FAILED: {exc}"

    return _done(code)


def run(session_id: str = None, tier: int = None):
    specs = read(KEYS["module_specs"])
    modules = specs["modules"]
    results = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(
                _write_one_module, module, KEY_ENVS[i % len(KEY_ENVS)],
                (i % len(KEY_ENVS)) + 1, session_id=session_id, tier=tier,
            ): module
            for i, module in enumerate(modules)
        }
        for future in as_completed(futures):
            name, code = future.result()
            results[name] = code
            print(f"    [Code Writer] wrote module: {name} ({len(code)} chars)")

    write(KEYS["submitted_code"], results)
    return results


if __name__ == "__main__":
    results = run()
    for name, code in results.items():
        print(f"\n=== {name} ===")
        print(code[:300] + ("..." if len(code) > 300 else ""))