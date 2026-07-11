"""
agents/dataset_analyst.py — Dataset Analyst (Part 3 §3.7).

REAL_ACTION_ROLES tool agent. Two-phase, same shape as the lean pipeline's
code_writer_lean.py -> sandbox_tester.py hand-off: one LLM call writes
analysis code for the requested task, then that code actually RUNS,
against the real dataset, in an E2B sandbox — this agent's output is a
genuine computed result, not the LLM's guess at one.

"Wraps sandbox_tester.py" specifically means: this module does not create
its own Sandbox or duplicate any of sandbox_tester.py's execution/error-
handling logic. It imports and calls agents.sandbox_tester._run_one_module()
unmodified, the exact function the tier-3 pool already uses per code
module. The dataset itself never touches sandbox_tester.py's interface
(which only accepts a code string) — it's base64-embedded into a small,
non-LLM-authored preamble prepended to the LLM-generated analysis code,
so the file is written to the sandbox's own filesystem before the
analysis code runs, with zero changes to sandbox_tester.py itself.

A deliberate, size-bounded scope: the raw dataset bytes are embedded
directly in the executed code, never sent to the LLM (only the filename
and task description are — keeps token cost flat regardless of dataset
size, unlike e.g. extraction_table_builder.py's per-abstract calls) and
never plotted (this sandbox's stdout/stderr capture has no image
channel) — CSV/TSV/JSON only, under MAX_DATASET_BYTES. A dataset that
doesn't fit this scope gets a clear "passed": False result explaining
why, the same posture sandbox_tester.py's own _run_one_module() already
takes toward a module with no code.

Result written to KEYS["dataset_analysis"]:
{"passed", "stdout", "stderr", "error", "parsed_result"}
-- the first four fields are exactly _run_one_module()'s own shape,
unchanged; "parsed_result" is this module's own addition: the JSON object
the generated code was instructed to print as its last stdout line,
already parsed out for a downstream role to read as structured data
instead of re-parsing raw stdout itself.
"""
import os
import sys
import re
import json
import time
import base64
import textwrap

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from relay.emitter import emit_event
from utils.llm_client import generate_text
from agents.sandbox_tester import _run_one_module

load_dotenv()

# Same three-tier fallback chain as agents/idea_planner.py and
# agents/reviewer_fixer_lean.py, reused for the same reason those two
# give: this is a single-pass generation call, not a worker pool, so
# there's no fairness rotation to do — just try each provider in order
# until one answers.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_1"},
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
]

# 2MB raw (before base64's ~33% inflation) -- generous for a CSV/TSV/JSON
# a research or analysis task would realistically hand this agent, small
# enough to embed directly in executed code without approaching E2B's
# code-size limits.
MAX_DATASET_BYTES = 2 * 1024 * 1024
SUPPORTED_EXTENSIONS = (".csv", ".tsv", ".json")

SYSTEM_PROMPT = """You are a data analyst. Write Python code that:
1. Reads the dataset already present at the exact relative path "{filename}" \
in the current working directory (do not invent a different filename or \
path). Use pandas for .csv/.tsv; use the json module directly for .json.
2. Performs the requested analysis.
3. Prints ONLY a single JSON object as the LAST line of stdout, \
summarizing the result (e.g. relevant computed statistics, a short \
natural-language "summary" field, any notable findings). Print nothing \
else -- no other prints, no plots (this sandbox's output capture has no \
image channel, so a plotting call would silently produce nothing useful).
4. Handles missing or malformed data defensively rather than crashing.
5. Does NOT fabricate a result. If the requested analysis genuinely can't \
be done with the given data (e.g. a referenced column doesn't exist), \
print a JSON object with an "error" field explaining exactly why instead \
of guessing.

Respond with ONLY the raw Python code, no markdown fences, no explanation."""


def _strip_fences(code: str) -> str:
    # Identical shape to code_writer_lean.py's own _strip_fences() --
    # duplicated rather than imported so the two agents' output-cleanup
    # logic doesn't accidentally couple.
    code = code.strip()
    if code.startswith("```"):
        code = code.split("```")[1]
        lines = code.split("\n", 1)
        if len(lines) > 1 and lines[0].strip().isalpha():
            code = lines[1]
        code = code.strip()
    return code


def _extract_json_result(stdout: str) -> dict | None:
    """Pulls the LAST valid JSON object out of stdout -- the system
    prompt asks for exactly one, on the last line, but takes the last
    PARSEABLE one rather than blindly trusting position, in case the
    analysis code printed anything else first despite the instruction
    not to."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    return None


def _resolve_dataset_path(task_text: str, dataset_path: str = None) -> str | None:
    """Explicit dataset_path wins outright. Otherwise KEYS["dataset_path"]
    -- a reserved hand-off key no upstream role writes yet (no file-
    upload-handling role exists in this system as of Part 3 §3.7), kept
    here so one can be wired in later with zero change to this function.
    Last resort: a bare filename-looking token in the task text itself
    (e.g. "analyze sales.csv for regional trends") -- the same informal
    convention academic_search.py's query string already relies on for
    "the task text IS the input," just narrowed to a file-extension
    pattern here."""
    if dataset_path:
        return dataset_path
    stored = read(KEYS["dataset_path"], default=None)
    if stored:
        return stored
    match = re.search(r"[\w\-./]+\.(?:csv|tsv|json)\b", task_text or "", re.IGNORECASE)
    return match.group(0) if match else None


def run(task_text: str = None, dataset_path: str = None, session_id: str = None,
        path: str = None, domain: str = None) -> dict:
    emit_event("agent_start", session_id=session_id, agent="dataset_analyst", path=path,
               payload={"label": "Dataset Analyst"})
    started = time.monotonic()

    def _done(result: dict) -> dict:
        duration_ms = int((time.monotonic() - started) * 1000)
        summary = "passed" if result.get("passed") else f"failed: {result.get('error') or 'see stderr'}"
        emit_event("agent_done", session_id=session_id, agent="dataset_analyst", path=path,
                   payload={"summary": summary, "duration_ms": duration_ms})
        write(KEYS["dataset_analysis"], result)
        return result

    resolved_path = _resolve_dataset_path(task_text, dataset_path)
    if not resolved_path or not os.path.isfile(resolved_path):
        # Deliberately NOT a MissingDependencyError: no upstream ROLE
        # produces a dataset file today (see _resolve_dataset_path's
        # docstring), so there's nothing eo/executor.py's self-heal could
        # sensibly insert. This is a real input gap, not a staffing gap
        # -- same distinction eo/errors.py's own docstring draws -- so it
        # gets the same graceful non-raising treatment
        # sandbox_tester.py's _run_one_module() already gives "no code
        # found for this module."
        return _done({
            "passed": False, "stdout": "", "stderr": "",
            "error": f"No readable dataset file found (looked for: {resolved_path or 'none named in task'}).",
            "parsed_result": None,
        })

    if not resolved_path.lower().endswith(SUPPORTED_EXTENSIONS):
        return _done({
            "passed": False, "stdout": "", "stderr": "",
            "error": f"Unsupported dataset type '{os.path.splitext(resolved_path)[1]}' "
                     f"-- this agent only handles {', '.join(SUPPORTED_EXTENSIONS)}.",
            "parsed_result": None,
        })

    size = os.path.getsize(resolved_path)
    if size > MAX_DATASET_BYTES:
        return _done({
            "passed": False, "stdout": "", "stderr": "",
            "error": f"Dataset is {size} bytes, over this agent's {MAX_DATASET_BYTES}-byte cap.",
            "parsed_result": None,
        })

    with open(resolved_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode("ascii")
    filename = os.path.basename(resolved_path)

    user_content = json.dumps({
        "task": task_text or f"Summarize the dataset '{filename}'.",
        "filename": filename,
    })
    try:
        raw = generate_text(
            SYSTEM_PROMPT.format(filename=filename), user_content, CHAIN,
            agent_name="Dataset Analyst", session_id=session_id, path=path, domain=domain,
        )
        analysis_code = _strip_fences(raw)
    except RuntimeError as exc:
        analysis_code = (
            "import json\n"
            f"print(json.dumps({{'error': 'analysis code generation failed: {exc}'}}))"
        )

    # Non-LLM-authored preamble -- writes the dataset to the sandbox's
    # filesystem from the embedded base64 before the generated analysis
    # code (which only ever sees the plain filename, per the system
    # prompt) runs against it.
    preamble = textwrap.dedent(f"""\
        import base64 as _b64
        with open({filename!r}, "wb") as _f:
            _f.write(_b64.b64decode({encoded!r}))
        """)
    full_code = preamble + "\n" + analysis_code

    _, sandbox_result = _run_one_module("dataset_analysis", {"language": "python", "code": full_code})
    sandbox_result["parsed_result"] = _extract_json_result(sandbox_result.get("stdout", ""))
    return _done(sandbox_result)


if __name__ == "__main__":
    result = run(task_text="Summarize this dataset.", dataset_path=sys.argv[1] if len(sys.argv) > 1 else None)
    print(json.dumps(result, indent=2))