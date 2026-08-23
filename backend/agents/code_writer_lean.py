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

2026-08-12 fix: the 3-step "rotation" below (gpt-oss-120b -> zai-glm-4.7
-> gemma-4-31b) was fake redundancy — every step read the same
CEREBRAS_API_KEY_1, so a single cooldown on that one account took out all
three steps at once; different models, same blast radius. Like
prompt_writer_lean.py, this is the tier-1 lean pipeline, so it bypasses
eo/registry.py's build_fallback_chain() by design (eo/registry.py:110)
and can't just call into the registry the way code_writer.py's own fix
did. Two changes: each rotation step now uses its own Cerebras account
(CEREBRAS_API_KEY_1/_2/_3, real siblings of the production 5-key pool —
see eo/registry.py), so the three steps actually fail independently; and
a genuine second-provider step is appended after them (Gemini, its own
key/account/infrastructure), matching the same hardcoded-second-provider
fix used in prompt_writer_lean.py and already present in
reviewer_fixer_lean.py, so a Cerebras-wide outage doesn't take this agent
down entirely.
"""
import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.llm_client import generate_text
from eo.errors import MissingDependencyError   # NEW — bug fix

# OR-3d: Cerebras -> OpenRouter. Was a 3-model rotation (gpt-oss-120b /
# zai-glm-4.7 / gemma-4-31b), each pinned to its own Cerebras account so a
# cooldown on one account didn't kill all three steps at once (see the
# 2026-08-12 fix note below, kept for history). OpenRouter's free tier is
# auto-routed -- "openrouter/free" (not a pinned model slug, see
# utils/llm_client.py's OR-1 notes) picks the underlying model itself, so
# there's no equivalent of a 3-model rotation to preserve here. What DOES
# still need preserving is the account independence: three steps on three
# different OPENROUTER_API_KEY_N accounts, so one account's rpm/rpd
# cooldown (OR-1d's request-count gating) still only takes out one step,
# not all three. This module bypasses eo/registry.py's
# build_fallback_chain() by design (eo/registry.py:110), same as before,
# so these keys are hardcoded siblings rather than registry-selected.
MODEL_KEYS = ["OPENROUTER_API_KEY_1", "OPENROUTER_API_KEY_2", "OPENROUTER_API_KEY_3"]

# Expressed as a llm_client chain: same key-independence fix as before,
# now on OpenRouter, plus the same hardcoded genuinely-different provider
# as a last resort (Gemini) -- unchanged, so a total OpenRouter outage
# doesn't take this agent down entirely.
CHAIN = [
    {"provider": "openrouter", "model": "openrouter/free", "key_env": k}
    for k in MODEL_KEYS
] + [
    {"provider": "gemini", "model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY_1"},
]

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


def run(module_spec: dict = None, session_id: str = None, path: str = None,
        domain: str = None) -> dict:
    if module_spec:
        write(KEYS["tier1_module_spec"], module_spec)
    else:
        module_spec = read(KEYS["tier1_module_spec"])
        if not module_spec:
            # Bug fix: consistent error type (see eo/errors.py). This is
            # the tier-1 "lean" fixed pipeline (path="direct") -- won't
            # auto-heal via executor.py's adaptive-only insertion, same
            # note as sandbox_tester_lean's identical fix.
            raise MissingDependencyError(
                "prompt_writer_lean",
                "No tier1_module_spec found in memory and none passed in. "
                "Run prompt_writer_lean first.",
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
            domain=domain,  # Migration Part 2 §2.6: cost-tracking gap
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