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

Stage 6 step 6: model rotation now goes through utils.llm_client's
generate_text() instead of a hand-rolled Cerebras client + retry loop, so
this agent's calls get usage-logged and fire usage_update events the same
way prompt_writer_lean's do. session_id/tier are optional passthroughs —
leaving them unset keeps behavior identical to before.
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.llm_client import generate_text

# Same rotation as agents/code_writers.py — see that file's docstring for
# why this list isn't the blueprint's original one (model deprecations).
MODELS = ["gpt-oss-120b", "zai-glm-4.7", "gemma-4-31b"]
KEY_ENV = "CEREBRAS_API_KEY_1"  # first key of the production 5-key pool

# Expressed as a llm_client chain: same provider and key each step, only
# the model changes — this is what "rotation" means for this agent.
CHAIN = [{"provider": "cerebras", "model": m, "key_env": KEY_ENV} for m in MODELS]

# Part 8.5's simplicity constraint, verbatim from the blueprint text.
SYSTEM_PROMPT = """You are a code generator for a lean, single-file build task. \
Given a JSON module spec (name, description, language, inputs, outputs, \
edge_cases, constraints), write the complete, working code for that module \
in the language specified by the spec's "language" field. If "language" is \
missing or empty, default to Python.

Honor every item in "constraints" as an explicit user requirement (e.g. \
brevity, no external libraries, a specific technique) alongside the \
functional spec.

For small, self-contained modules, write the simplest correct \
implementation. Do not introduce adapter, bridge, or service-indirection \
layers unless the spec explicitly calls for integrating with an external \
system. A single file solving the stated problem is preferred over \
multiple files that only forward calls to each other.

Respond with ONLY the raw code, no markdown code fences, no explanation, \
no commentary before or after."""


def _strip_fences(code: str) -> str:
    code = code.strip()
    if code.startswith("```"):
        code = code.split("```")[1]
        # first line after the opening fence is often a language tag
        # (python, c, cpp, javascript, ...) regardless of what language
        # was actually requested — drop it if it looks like a bare tag
        # rather than actual code.
        lines = code.split("\n", 1)
        if len(lines) > 1 and lines[0].strip().isalpha():
            code = lines[1]
        code = code.strip()
    return code


def run(module_spec: dict = None, session_id: str = None, path: str = None) -> dict:
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
    user_content = json.dumps(module_spec)

    try:
        raw = generate_text(
            SYSTEM_PROMPT,
            user_content,
            CHAIN,
            agent_name="Code Writer (lean)",
            session_id=session_id,
            path=path,  # Migration Part 27 §1: generate_text() now accepts `path` for real
        )
        code = _strip_fences(raw)
        if not code:
            code = f"# CODE WRITER FAILED: model returned empty output for '{name}'."
    except RuntimeError as exc:
        code = f"# CODE WRITER FAILED: {exc}"

    result = {"name": name, "language": module_spec.get("language") or "python", "code": code}
    write(KEYS["tier1_code"], result)
    return result


if __name__ == "__main__":
    spec = read(KEYS["tier1_module_spec"], default={"name": "reverse_string", "description": "reverse a string from stdin"})
    result = run(spec)
    print(result["code"])