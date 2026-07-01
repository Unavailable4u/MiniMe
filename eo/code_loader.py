"""
eo/code_loader.py — Stage 4 step 5's missing piece for tier 2.

Part 2.5: "Tier 2 calls directly into the existing 19-agent roster's
specialists" — but every one of those specialists (reviewer.py,
fixer_pool.py, code_writers.py's refactor path, etc.) reads its input from
memory.bus's submitted_code key, which normally only gets populated by a
tier-3 cycle's own Code Writer Pool. A tier-2 "directed task against an
EXISTING codebase" (Part 2.1's own tier-2 definition) has no such cycle —
the code already exists on disk under apps/{app_slug}/. This module is
the bridge: read what's actually on disk, write it into memory in the
exact shape the tier-3 agents already expect, so they can't tell the
difference between "code a Code Writer just produced this cycle" and
"code that's been sitting in the repo since a prior cycle."

Deliberately NOT part of the tier-3 pipeline and NOT invoked by loop.py —
this only exists for eo/loop_v4.py's tier-2 path.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

APPS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")


def list_available_apps() -> list:
    """Every app_slug currently sitting under apps/, for a CLI/frontend to
    offer as choices when a tier-2 task doesn't name one explicitly."""
    if not os.path.isdir(APPS_ROOT):
        return []
    return sorted(
        name for name in os.listdir(APPS_ROOT)
        if os.path.isdir(os.path.join(APPS_ROOT, name)) and not name.startswith("__")
    )


def load_existing_app(app_slug: str) -> dict:
    """
    Reads every .py file under apps/{app_slug}/src/, writes them into
    memory as KEYS["submitted_code"] (same shape the Code Writer Pool
    produces: {module_key: {"language": "python", "code": "..."}}), sets
    KEYS["app_slug"] so every subsequent memory.bus call is namespaced
    to this app, and returns the loaded dict.

    module_key is the file's path relative to src/ with slashes turned
    into underscores (e.g. "todo/task_editor.py" -> "todo_task_editor") —
    stable, collision-resistant, and close enough to the tier-3 agents'
    own module-name convention that review/fix output referencing it
    reads naturally.

    Raises FileNotFoundError if the app_slug doesn't exist on disk at all
    — a typo here should fail loudly, not silently operate on an empty
    codebase.
    """
    app_dir = os.path.join(APPS_ROOT, app_slug)
    src_dir = os.path.join(app_dir, "src")
    if not os.path.isdir(app_dir):
        available = list_available_apps()
        raise FileNotFoundError(
            f"No app found at {app_dir}. Available apps: {available or '(none)'}"
        )
    write(KEYS["app_slug"], app_slug)
    submitted_code = {}
    if os.path.isdir(src_dir):
        for root, _dirs, files in os.walk(src_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                full_path = os.path.join(root, fname)
                rel_path = os.path.relpath(full_path, src_dir)
                module_key = rel_path[:-3].replace(os.sep, "_")
                with open(full_path, "r", encoding="utf-8") as f:
                    code = f.read()
                submitted_code[module_key] = {"language": "python", "code": code}
    write(KEYS["submitted_code"], submitted_code)
    return submitted_code


if __name__ == "__main__":
    import json
    slug = sys.argv[1] if len(sys.argv) > 1 else None
    if not slug:
        print("Usage: python eo/code_loader.py <app_slug>")
        print(f"Available apps: {list_available_apps()}")
        sys.exit(1)
    loaded = load_existing_app(slug)
    print(json.dumps({k: {"language": v["language"], "chars": len(v["code"])} for k, v in loaded.items()}, indent=2))
