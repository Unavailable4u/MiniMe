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

Place this file at: agents/structure_architect.py
"""

import os
import sys
import json
from dotenv import load_dotenv
from groq import Groq

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

load_dotenv()

# Use your second Groq key here so this agent draws from a separate daily
# quota than prompt_writer.py / gatekeeper.py, which use GROQ_API_KEY.
api_key = os.getenv("GROQ_API_KEY_9") or os.getenv("GROQ_API_KEY")
client = Groq(api_key=api_key, timeout=30)

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


def run_structure_architect() -> dict:
    fixed_code = read(KEYS["fixed_code"])
    if not fixed_code:
        raise ValueError("No fixed_code found in memory. Run Fixer+Tester first.")

    app_slug = read(APP_SLUG_KEY, default=None)
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

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content
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

    write(FILE_PLAN_KEY, plan)
    return plan


if __name__ == "__main__":
    plan = run_structure_architect()
    print(json.dumps(plan, indent=2))