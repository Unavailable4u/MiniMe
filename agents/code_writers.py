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
"""

import os
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from cerebras.cloud.sdk import Cerebras, RateLimitError, APIStatusError, APIConnectionError, APITimeoutError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

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

_TRANSIENT_ERRORS = (RateLimitError, APIStatusError, APIConnectionError, APITimeoutError)

SYSTEM_PROMPT = """You are a focused implementer. Write complete, runnable Python code
for the module described below. Follow the spec exactly. Include basic input validation.
Do not invent features outside the spec. Output ONLY the code, no explanation, no markdown
code fences."""

_client_cache = {}


def _get_client(key_env: str) -> Cerebras:
    key = os.getenv(key_env)
    if not key:
        return None
    if key_env not in _client_cache:
        _client_cache[key_env] = Cerebras(api_key=key)
    return _client_cache[key_env]


def _strip_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = code.split("```")[1]
        if code.startswith("python"):
            code = code[6:]
        code = code.strip()
    return code


def _write_one_module(module_spec: dict, key_env: str) -> tuple[str, str]:
    """
    Runs on one worker thread with one fixed Cerebras key. Tries each model
    in MODELS, in order, staying on this same key throughout. Returns
    (module_name, code).
    """
    name = module_spec.get("name", "?")
    client = _get_client(key_env)

    if client is None:
        return name, (f"# CODE WRITER FAILED: {key_env} not set. "
                       f"No code generated for module '{name}'.")

    user_content = json.dumps(module_spec)
    last_exc = None

    for model_index, model in enumerate(MODELS):
        print(f"    [Code Writer:{key_env}] module '{name}' trying model: {model}")
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            raw = response.choices[0].message.content or ""
            code = _strip_fences(raw)
            if not code:
                code = (f"# CODE WRITER FAILED: model returned empty content. "
                         f"No code generated for module '{name}'.")
            return name, code

        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            is_last = model_index == len(MODELS) - 1
            print(f"    [Code Writer:{key_env}] {model} failed "
                  f"({exc.__class__.__name__}) for '{name}'"
                  + ("" if is_last else ", trying next model..."))

    return name, (f"# CODE WRITER FAILED: all models exhausted on {key_env}. "
                   f"Last error: {last_exc}")


def run():
    specs = read(KEYS["module_specs"])
    modules = specs["modules"]
    results = {}

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(_write_one_module, module, KEY_ENVS[i % len(KEY_ENVS)]): module
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