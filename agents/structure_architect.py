"""
agents/structure_architect.py — decides HOW the codebase should be organized
on disk. Does not touch the filesystem itself; it only outputs a plan of
operations that file_manager.py then executes mechanically.

Runs after Fixer+Tester, before file_manager.py.

Why this is its own agent and not just smarter code:
- Deciding whether a new module is a new file, belongs inside an existing
  file, needs its own subfolder, or makes an old file obsolete requires
  understanding the code's purpose -- a judgment call, not a string operation.
- Keeping it separate from file_manager.py means the LLM only ever proposes
  a plan (JSON); your own code is the only thing that ever writes, deletes,
  or moves files. If the model hallucinates a bad path, you can validate/
  reject the plan before anything touches disk.

Bug fix — two-mode planning: this used to hard-require `fixed_code` and
raise if it was empty, which made this agent (and file_manager.py
downstream) structurally unable to run for any task that doesn't produce
source code at all -- e.g. "build me a yearbook app with a folder per
year to keep memories in," which is a genuine, valid file/folder-
organizing request, not a coding one. The Inspector/Panel can (correctly)
decide a task needs "file_manager" without needing any code-writing role
at all; this agent shouldn't then be the thing that makes that
impossible.

Now genuinely two modes:
  - Code mode (unchanged behavior): fixed_code (preferred) or
    submitted_code is present -- plan write/move/delete/mkdir ops for
    those code modules, exactly as before.
  - No-code mode (NEW): neither is present -- plan a plain folder/file
    scaffold straight from the task description (and whatever idea/plan
    context exists), using "mkdir" and content-only "write" ops (no
    "module" field, since there's no code module to look up). This is a
    real, separate LLM call with its own prompt -- not a guess or a
    silent no-op -- so the plan it produces is genuinely shaped by what
    the person actually asked for.

Place this file at: agents/structure_architect.py
"""

import os
import re
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.llm_client import generate_text
from relay.emitter import emit_event
from eo.errors import MissingDependencyError   # NEW — bug fix

load_dotenv()

# GROQ_API_KEY_9 first so this agent draws from a separate daily quota
# than prompt_writer.py / gatekeeper.py, falling back to the shared
# GROQ_API_KEY if key #9 isn't set -- same intent as the original
# `os.getenv("GROQ_API_KEY_9") or os.getenv("GROQ_API_KEY")`, but now a
# real two-step chain: generate_text() also falls through to the second
# step on a transient provider error, not just a missing key, which the
# original single-resolved-key version couldn't do.
#
# timeout=30 preserved from the original `Groq(..., timeout=30)` -- this
# agent runs synchronously in the tier-3 pipeline right before file
# operations, so a stalled call should fail fast and fall through to the
# next chain step rather than hang the whole cycle. Routed through
# generate_text() instead of a hand-rolled client so this call also gets
# usage-logged -- previously it logged nothing.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY_9", "timeout": 30},
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY", "timeout": 30},
]

FILE_MAP_KEY = KEYS.get("file_map", "file_map")
APP_SLUG_KEY = KEYS.get("app_slug", "app_slug")
FILE_PLAN_KEY = "file_plan"

APPS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")

SYSTEM_PROMPT = """You are the file/folder architect for an autonomous coding
pipeline. You decide HOW a codebase should be organized on disk -- you do not
write code yourself, only structure decisions.

You will be given:
- the current file tree of the project (may be empty on cycle 1)
- the current file_map (module name -> existing file path, may be empty)
- the newly generated code modules for this cycle (name, language, and a
  short preview of the code)

For each module, decide ONE action:
- "write": create a new file, or overwrite/update an existing one if this
  module already has a mapped path. Choose a clear, idiomatic path and
  filename (e.g. group related modules under src/api/, src/models/, etc.
  instead of dumping everything flat into src/).
- "move": if a module already has a file but it should now live somewhere
  more appropriate given the growing project structure, specify old_path and
  new_path.

You may also propose:
- "delete": for files that are clearly obsolete/superseded and not referenced
  by file_map or the current modules. Be conservative -- only delete when
  you are confident, never delete README.md or files under tests/.
- "mkdir": only if you need an empty folder not implied by a write/move path.

Respond with ONLY valid JSON, no markdown fences, no explanation, in exactly
this shape:
{
  "operations": [
    {"action": "write", "module": "module_name", "path": "src/api/routes.py", "reason": "..."},
    {"action": "move", "module": "module_name", "old_path": "src/old.py", "new_path": "src/api/old.py", "reason": "..."},
    {"action": "delete", "path": "src/dead_code.py", "reason": "..."},
    {"action": "mkdir", "path": "src/utils", "reason": "..."}
  ]
}
Every module in the input MUST appear in exactly one "write" or "move"
operation. Keep paths relative to the app root, always starting with "src/"
or "tests/". Use lowercase_with_underscores for filenames.
"""

# NEW — bug fix: no-code mode's own prompt. Deliberately NOT a variant of
# the code-mode prompt above -- the two modes plan fundamentally different
# things (organizing existing code modules vs designing a folder/file
# layout from a plain description), so sharing one prompt with a couple of
# conditional sentences would leave the model guessing which rules apply
# when. This one never mentions "module" or "code" at all.
NO_CODE_SYSTEM_PROMPT = """You are a file/folder architect for a general-purpose \
task-execution system. This particular task does not involve writing code -- it \
needs a plain folder/file structure created to organize or hold something (notes, \
records, media, a per-year archive, etc.). You decide WHAT files and folders to \
create and where -- you do not generate the substantive content of any file \
yourself beyond a short, genuinely useful placeholder (e.g. a one-line README, an \
empty folder for the person to fill in later).

You will be given the task description and any planning context already produced \
for it (e.g. a feature list). Design a clear, sensibly-named folder/file layout \
that actually satisfies what was asked -- if the task describes a specific set of \
items (e.g. "a folder for every year from my birth year to now"), enumerate them \
for real rather than a token example.

For each folder, use "mkdir". For each file, use "write" with a short "content" \
string (may be empty "" for a genuinely empty placeholder file, e.g. a blank note \
file the person will fill in) -- never a "module" field, there is no code module \
here.

Respond with ONLY valid JSON, no markdown fences, no explanation, in exactly this \
shape:
{
  "operations": [
    {"action": "mkdir", "path": "memories/1998", "reason": "..."},
    {"action": "write", "path": "memories/1998/notes.md", "content": "# 1998\\n", "reason": "..."}
  ]
}
Keep paths relative to the app root -- do NOT prefix them with "src/" or "tests/", \
those are source-code conventions that don't apply here. Use lowercase_with_underscores \
or plain numbers for names, whichever is clearer for what's being organized.
"""


def _get_project_tree(app_dir: str) -> list:
    if not os.path.isdir(app_dir):
        return []
    tree = []
    for root, _, files in os.walk(app_dir):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, app_dir)
            tree.append(rel)
    return sorted(tree)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _code_preview(code: str, max_chars: int = 400) -> str:
    return code if len(code) <= max_chars else code[:max_chars] + "...(truncated)"


def _mermaid_id(text: str) -> str:
    # Mermaid node IDs can't contain slashes/dots/etc -- sanitize while
    # keeping the readable label (set separately) intact.
    return "n" + re.sub(r"[^A-Za-z0-9_]", "_", text)


def _build_mermaid(plan: dict) -> str:
    """Turns the operations plan into a flowchart -- there's no
    module-depends-on-module relationship here (that's dependency_mapper.py's
    job), so this instead shows what structure_architect.py actually decided:
    module-->path for writes, old_path-->new_path for moves, and distinct
    shapes for delete/mkdir so they're visually distinguishable from writes.

    Bug fix: a no-code "write" op has no "module" (see NO_CODE_SYSTEM_PROMPT
    above) -- shown as its own leaf node instead of a module-->path edge,
    same visual family as "mkdir" since both are just "this gets created."
    """
    lines = ["graph TD"]
    for op in plan.get("operations", []):
        action = op.get("action")
        if action == "write":
            module = op.get("module")
            path = op.get("path", "?")
            pid = _mermaid_id(f"path_{path}")
            if module:
                mid = _mermaid_id(f"mod_{module}")
                lines.append(f'{mid}["{module}"] -->|write| {pid}["{path}"]')
            else:
                lines.append(f'{pid}("{path}")')
        elif action == "move":
            old_path = op.get("old_path", "?")
            new_path = op.get("new_path", "?")
            oid = _mermaid_id(f"path_{old_path}")
            nid = _mermaid_id(f"path_{new_path}")
            lines.append(f'{oid}["{old_path}"] -->|move| {nid}["{new_path}"]')
        elif action == "delete":
            path = op.get("path", "?")
            pid = _mermaid_id(f"path_{path}")
            lines.append(f'{pid}["{path}"]:::deleted')
        elif action == "mkdir":
            path = op.get("path", "?")
            pid = _mermaid_id(f"path_{path}")
            lines.append(f'{pid}[["{path}/"]]')
    lines.append("classDef deleted fill:#7f1d1d,stroke:#ef4444,color:#fca5a5")
    return "\n".join(lines)


def _code_plan(fixed_code: dict, session_id: str, tier: int, domain: str = None) -> dict:
    """Existing (pre-fix) behavior, unchanged: plan write/move/delete/mkdir
    operations for a set of already-generated code modules."""
    # Migration Part B (session isolation fix): get_current_app_slug(),
    # not read(APP_SLUG_KEY, ...) -- "app_slug" is exempt from
    # memory.bus's namespacing, so a raw read would see (or plan a file
    # layout against) an unrelated session's existing app directory. See
    # api/task_runner.py's _run_tier3_hires() / memory/bus.py's
    # set_app_slug().
    from memory.bus import get_current_app_slug
    app_slug = get_current_app_slug()
    app_dir = os.path.join(APPS_ROOT, app_slug) if app_slug else None
    project_tree = _get_project_tree(app_dir) if app_dir else []
    file_map = read(FILE_MAP_KEY, default={})

    modules_for_prompt = {}
    for name, data in fixed_code.items():
        if name == "_fixer_error":
            continue
        if isinstance(data, dict):
            language = data.get("language", "python")
            code = data.get("code", "")
        else:
            language = "python"
            code = data or ""
        modules_for_prompt[name] = {
            "language": language,
            "code_preview": _code_preview(code),
        }

    user_prompt = (
        "Current project file tree:\n" + json.dumps(project_tree, indent=2)
        + "\n\nCurrent file_map (module -> existing path):\n" + json.dumps(file_map, indent=2)
        + "\n\nNew/updated modules this cycle:\n" + json.dumps(modules_for_prompt, indent=2)
    )

    raw = generate_text(SYSTEM_PROMPT, user_prompt, CHAIN, agent_name="Structure Architect",
                         session_id=session_id, tier=tier, domain=domain)
    cleaned = _strip_fences(raw)

    try:
        plan = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fail safe: fall back to one "write" op per module at a flat default
        # path, so file_manager.py still has something valid to execute.
        plan = {
            "operations": [
                {
                    "action": "write",
                    "module": name,
                    "path": file_map.get(name, f"src/{name}.py"),
                    "reason": "fallback: architect output was not valid JSON",
                }
                for name in modules_for_prompt
            ]
        }
    return plan


def _no_code_plan(task_text: str, session_id: str, tier: int, domain: str = None) -> dict:
    """NEW — bug fix: plan a plain folder/file scaffold when there's no
    code to organize. Pulls in whatever idea/plan context this session
    has produced so far (idea_planner's output, if it ran) alongside the
    task text itself, same "give the model everything genuinely relevant,
    let it decide" spirit as _code_plan() above -- this is a real planning
    call, not a token/fallback one."""
    current_plan = read(KEYS["current_plan"], default=None)
    idea = read(KEYS["original_idea"], default=None)

    user_prompt = f"Task: {task_text or '(no task text available)'}"
    if idea:
        user_prompt += f"\n\nOriginal idea: {idea}"
    if current_plan:
        user_prompt += f"\n\nPlan produced for this task so far:\n{json.dumps(current_plan, indent=2)}"

    raw = generate_text(NO_CODE_SYSTEM_PROMPT, user_prompt, CHAIN,
                         agent_name="Structure Architect (no-code)",
                         session_id=session_id, tier=tier, domain=domain)
    cleaned = _strip_fences(raw)

    try:
        plan = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fail safe: a single top-level folder named from the task, rather
        # than nothing at all -- file_manager.py still has something valid
        # to execute, same principle as _code_plan()'s own fallback.
        plan = {
            "operations": [
                {"action": "mkdir", "path": "files",
                 "reason": "fallback: architect output was not valid JSON"},
            ]
        }
    return plan


def run_structure_architect(session_id: str = None, tier: int = None,
                             task_text: str = None, domain: str = None) -> dict:
    """
    Bug fix: no longer hard-requires fixed_code. Prefers fixed_code
    (Fixer Pool's output) over submitted_code (Code Writers' raw output,
    same preference order file_manager.py already uses) when planning a
    code layout; when NEITHER is present, plans a plain folder/file
    scaffold from task_text instead of raising -- see this module's
    docstring for why a "no code at all" task is a legitimate, separate
    case rather than an error.

    `task_text` (NEW param) is only used by the no-code path -- the code
    path doesn't need it, it already has the actual modules to work from.
    """
    fixed_code = read(KEYS["fixed_code"], default=None)
    submitted_code = read(KEYS["submitted_code"], default=None)
    code_source = fixed_code or submitted_code

    if code_source:
        plan = _code_plan(code_source, session_id, tier, domain=domain)
    else:
        plan = _no_code_plan(task_text, session_id, tier, domain=domain)

    plan["mermaid"] = _build_mermaid(plan)
    write(FILE_PLAN_KEY, plan)
    emit_event("structure_plan", session_id, agent="structure_architect",
               payload={"mermaid": plan["mermaid"]})
    return plan


if __name__ == "__main__":
    plan = run_structure_architect()
    print(json.dumps(plan, indent=2))