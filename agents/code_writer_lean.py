"""
agents/code_writer_lean.py — Code Writer (1-worker), Part 2.4's tier-1
pipeline, second step.

Same provider, same model rotation, and the same first key (Cerebras
CEREBRAS_API_KEY_1) as agents/code_writers.py's 5-worker pool — Part 2.4's
table says explicitly this shares "the same pool as the production
5-worker Code Writer Pool." The only difference from the pool version is
concurrency: one worker, one module, no ThreadPoolExecutor needed.

Includes the Part 8.5 simplicity constraint in its own system prompt
(rather than the production Code Writer's prompt) — Part 8.5 is explicit
that this is a tier-0/1-only guardrail and should NOT touch the tier-3
Code Writer Pool's prompt, since large multi-module projects sometimes
legitimately need adapter/service layers that a single small module never
does.
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from cerebras.cloud.sdk import Cerebras, RateLimitError, APIStatusError, APIConnectionError, APITimeoutError

# Same rotation as agents/code_writers.py — see that file's docstring for
# why this list isn't the blueprint's original one (model deprecations).
MODELS = ["gpt-oss-120b", "zai-glm-4.7", "gemma-4-31b"]
KEY_ENV = "CEREBRAS_API_KEY_1"  # first key of the production 5-key pool

_TRANSIENT_ERRORS = (RateLimitError, APIStatusError, APIConnectionError, APITimeoutError)

# Part 8.5's simplicity constraint, verbatim from the blueprint text.
SYSTEM_PROMPT = """You are a focused implementer. Write complete, runnable Python \
code for the module described below. Follow the spec exactly. Include basic \
input validation. Do not invent features outside the spec.

For small, self-contained modules, write the simplest correct \
implementation. Do not introduce adapter, bridge, or service-indirection \
layers unless the spec explicitly calls for integrating with an external \
system. A single file solving the stated problem is preferred over \
multiple files that only forward calls to each other.

Output ONLY the code, no explanation, no markdown code fences."""

_client_cache = {}


def _get_client():
    key = os.getenv(KEY_ENV)
    if not key:
        return None
    if KEY_ENV not in _client_cache:
        _client_cache[KEY_ENV] = Cerebras(api_key=key)
    return _client_cache[KEY_ENV]


def _strip_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = code.split("```")[1]
        if code.startswith("python"):
            code = code[6:]
        code = code.strip()
    return code


def run(module_spec: dict = None) -> dict:
    if module_spec:
        write(KEYS["tier1_module_spec"], module_spec)
    else:
        module_spec = read(KEYS["tier1_module_spec"])
        if not module_spec:
            raise ValueError(
                "No tier1_module_spec found in memory and none passed in. "
                "Run prompt_writer_lean first."
            )
    name = module_spec.get("name", "module")
    client = _get_client()
    if client is None:
        code = f"# CODE WRITER FAILED: {KEY_ENV} not set. No code generated for '{name}'."
        result = {"name": name, "code": code}
        write(KEYS["tier1_code"], result)
        return result
    user_content = json.dumps(module_spec)
    last_exc = None
    code = None
    for model_index, model in enumerate(MODELS):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_content},
                ],
            )
            code = _strip_fences(response.choices[0].message.content or "")
            if code:
                break
        except _TRANSIENT_ERRORS as exc:
            last_exc = exc
            is_last = model_index == len(MODELS) - 1
            print(f"  [Code Writer (lean)] {model} failed "
                  f"({exc.__class__.__name__})" + ("" if is_last else ", trying next model..."))
    if not code:
        code = f"# CODE WRITER FAILED: all models exhausted. Last error: {last_exc}"
    result = {"name": name, "language": "python", "code": code}
    write(KEYS["tier1_code"], result)
    return result


if __name__ == "__main__":
    spec = read(KEYS["tier1_module_spec"], default={"name": "reverse_string", "description": "reverse a string from stdin"})
    result = run(spec)
    print(result["code"])
