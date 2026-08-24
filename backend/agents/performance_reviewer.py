"""
agents/performance_reviewer.py — Performance Reviewer (Patch 8, rollout
guide §3: the `implementer` pipeline split).

REAL_ACTION_ROLES tool agent, same two-phase shape as agents/
dataset_analyst.py: one LLM call writes a small profiling harness for a
SINGLE code module, then that harness actually RUNS, in an E2B sandbox,
via agents.sandbox_tester._run_one_module() unmodified — the exact
function the tier-3 pool already uses per code module. This agent's
output is a genuinely measured timing/memory result, not the LLM's guess
at one, which is the whole reason this role exists as a dedicated module
instead of a generic_worker text role (see the rollout guide's own §3
"needs real execution/profiling access" framing).

Advisory only: this role's output is a *flag for fixer*, not a pass/fail
gate the way sandbox_tester's own test_results are. fixer/verifier still
catch real bugs on a cycle where this step is skipped entirely (account
exhausted, no code passed test_results yet, etc.) — see
eo/structure.py's STRUCTURE_TEMPLATES["coding"] comment for why it's
positioned after verifier, before fixer.

Module selection: unlike dataset_analyst (one dataset per run), this
reads the WHOLE fixed_code/submitted_code dict and profiles ONE module
from it per run — the first module that has already PASSED
KEYS["test_results"] (no point profiling code that doesn't even work
yet), in insertion order. A multi-module project only gets its first
passing module profiled per cycle; that's a deliberate scope limit for
this patch, not an oversight — see the module docstring note in
KEYS["performance_review"] (memory/bus.py) for the result shape this
writes.

Result written to KEYS["performance_review"]:
{"module", "passed", "stdout", "stderr", "error", "parsed_result"}
-- "module" is this agent's own addition (which module got profiled);
the next four fields are exactly _run_one_module()'s own shape,
unchanged; "parsed_result" is this module's own addition, same
convention as dataset_analyst.py: the JSON object the generated harness
was instructed to print as its last stdout line, already parsed out for
fixer to read as structured data instead of re-parsing raw stdout
itself.
"""
import json
import os
import sys
import time

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agents.sandbox_tester import _run_one_module
from memory.bus import KEYS, read, write
from relay.emitter import emit_event
from utils.llm_client import generate_text

load_dotenv()

# Patch 8's own reserve key (env(example).txt: "-> RESERVE, untagged —
# hold for §3 (logic_architect / performance_reviewer)", GEMINI_API_KEY_13
# specifically — GEMINI_API_KEY_12 is logic_architect's, see eo/registry.py's
# AGENT_CAPABILITIES entry). Two steps on the SAME key, not two different
# accounts: gemini-3.1-flash-lite first for this advisory, mechanical-harness
# role's sake (meaningfully higher free-tier daily headroom than the
# gemini-3.6-flash every other tag-driven Gemini entry in this system runs
# as), falling back to gemini-3.6-flash — the one Gemini model this whole
# codebase has actually confirmed GA and non-404ing post-Patch-5b (see
# agents/generic_worker.py's PROVIDER_DEFAULT_MODEL comment) — on the same
# key if flash-lite's own quota or availability is the problem. This is a
# genuinely single-account role by design (only 1 of the 2 reserve keys is
# this role's), so it has no OTHER account to fall through to the way a
# tag-driven generic_worker role does via _best_match()'s whole-pool
# fallback — that's an acceptable, explicitly-accepted tradeoff for an
# advisory-only role (see module docstring), not an oversight.
CHAIN = [
    {"provider": "gemini", "model": "gemini-3.1-flash-lite", "key_env": "GEMINI_API_KEY_13"},
    # Quota-reality fix, §11b (2026-07-30): was GEMINI_API_KEY_13 on both
    # steps (§6's single-account gap) -- different models, same key, so
    # it was fine as a rate-limit fallback but zero protection against
    # that one account being suspended/revoked/otherwise fully down.
    # GEMINI_API_KEY_14 (one of the 5 newly-provisioned Gemini keys) gives
    # this role genuine account-level redundancy: a suspended/revoked
    # _13 no longer takes out both fallback rungs at once.
    {"provider": "gemini", "model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY_14"},
]

SYSTEM_PROMPT = """You are a performance reviewer. Given a Python module's source code, \
write a SEPARATE, SELF-CONTAINED Python script (not a modification of the module itself) that:
1. Defines the module's source code as a string variable, or writes it to a temp file and \
imports it -- whichever is simpler for this module's shape. Do not assume the module is \
already importable from anywhere on disk.
2. Constructs small, synthetic but REALISTIC inputs for the module's main entry point(s) \
(function/class described in the module below). Prefer a moderately-sized input (e.g. a few \
hundred to a few thousand elements for a list/dict-processing function) that would actually \
reveal a slow algorithm, not a trivial 1-element case.
3. Times execution with time.perf_counter() around the call, and measures peak memory with \
the tracemalloc module (tracemalloc.start() before, tracemalloc.get_traced_memory() after).
4. Prints ONLY a single JSON object as the LAST line of stdout, with fields: "elapsed_seconds" \
(float), "peak_memory_kb" (float), "input_size" (what size input was used, for context), and \
"notes" (a short string -- flag anything like an obviously quadratic loop, unbounded \
recursion, or large intermediate data structure you noticed while writing this harness; \
empty string if nothing stands out). Print nothing else -- no other prints.
5. Handles the module failing to import or run defensively: if it does, print a JSON object \
with an "error" field explaining exactly why instead of crashing with a bare traceback.
6. Does NOT fabricate a measurement. If the module has no clear callable entry point to \
profile, print a JSON object with an "error" field explaining that instead of inventing one.

Respond with ONLY the raw Python code for this profiling script, no markdown fences, no \
explanation."""


def _strip_fences(code: str) -> str:
    # Same shape as dataset_analyst.py's own _strip_fences() -- duplicated
    # rather than imported for the same reason that module gives: these
    # two agents' output-cleanup logic shouldn't accidentally couple.
    code = code.strip()
    if code.startswith("```"):
        code = code.split("```")[1]
        lines = code.split("\n", 1)
        if len(lines) > 1 and lines[0].strip().isalpha():
            code = lines[1]
        code = code.strip()
    return code


def _extract_json_result(stdout) -> dict | None:
    """Pulls the LAST valid JSON object out of stdout -- the system prompt
    asks for exactly one, on the last line, but takes the last PARSEABLE
    one rather than blindly trusting position, in case the harness
    printed anything else first despite the instruction not to.

    stdout can come back from _run_one_module() as either a single string
    or a list of strings -- confirmed against a real run (E2B's
    execution.logs.stdout's exact type varies by SDK version/build, and
    sandbox_tester.py's own _run_one_module() passes it through
    unmodified either way, see its own code). Normalizing here, rather
    than in sandbox_tester.py, keeps this fix scoped to this agent's own
    read of that field instead of changing a function three other agents
    (dataset_analyst.py, fixer_pool.py, security_scanner.py) also call.
    """
    if isinstance(stdout, list):
        stdout = "\n".join(str(line) for line in stdout)
    if not stdout:
        return None
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _select_module_to_profile(fixed_code: dict, test_results: dict) -> tuple[str, dict] | tuple[None, None]:
    """First module (insertion order) that both has code AND has already
    PASSED test_results -- no point profiling code that doesn't even work
    yet (see module docstring's "Module selection" note). Returns
    (name, module_data) or (None, None) if nothing qualifies."""
    for name, data in fixed_code.items():
        if name == "_fixer_error":
            continue
        result = test_results.get(name)
        if result and result.get("passed"):
            return name, data
    return None, None


def run(session_id: str = None, path: str = None, domain: str = None) -> dict:
    emit_event("agent_start", session_id=session_id, agent="performance_reviewer", path=path,
               payload={"label": "Performance Reviewer"})
    started = time.monotonic()

    def _done(result: dict) -> dict:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = "passed" if result.get("passed") else f"failed: {result.get('error') or 'see stderr'}"
        emit_event("agent_done", session_id=session_id, agent="performance_reviewer", path=path,
                   payload={"summary": summary, "duration_ms": duration_ms})
        write(KEYS["performance_review"], result)
        return result

    # Same fallback pattern sandbox_tester.py's own run_sandbox_tester()
    # and dataset_analyst.py both already use: fixed_code first, fall back
    # to submitted_code if Fixer Pool hasn't run this cycle.
    fixed_code = read(KEYS["fixed_code"], default=None) or read(KEYS["submitted_code"], default={})
    test_results = read(KEYS["test_results"], default={})

    if not fixed_code:
        return _done({
            "module": None, "passed": False, "stdout": "", "stderr": "",
            "error": "No fixed_code or submitted_code found in memory — nothing to profile yet.",
            "parsed_result": None,
        })

    module_name, module_data = _select_module_to_profile(fixed_code, test_results)
    if module_name is None:
        # Deliberately NOT a MissingDependencyError: sandbox_tester simply
        # hasn't produced a passing module yet this cycle (or none of them
        # pass) -- a real, expected state on a cycle with failing tests,
        # not a staffing gap eo/executor.py's self-heal could fix by
        # inserting a role. Same graceful-skip posture dataset_analyst.py's
        # own docstring describes for its own "nothing to do yet" case.
        return _done({
            "module": None, "passed": False, "stdout": "", "stderr": "",
            "error": "No module has a passing test_results entry yet — nothing safe to profile this cycle.",
            "parsed_result": None,
        })

    code = module_data.get("code", "") if isinstance(module_data, dict) else str(module_data)
    user_content = json.dumps({"module_name": module_name, "code": code})

    try:
        raw = generate_text(
            SYSTEM_PROMPT, user_content, CHAIN,
            agent_name="Performance Reviewer", session_id=session_id, path=path, domain=domain,
        )
        harness_code = _strip_fences(raw)
    except RuntimeError as exc:
        harness_code = (
            "import json\n"
            f"print(json.dumps({{'error': 'profiling harness generation failed: {exc}'}}))"
        )

    _, sandbox_result = _run_one_module(f"{module_name}_perf_review", {"language": "python", "code": harness_code})
    sandbox_result["module"] = module_name
    sandbox_result["parsed_result"] = _extract_json_result(sandbox_result.get("stdout", ""))
    return _done(sandbox_result)


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
