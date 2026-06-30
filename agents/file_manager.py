"""
agents/file_manager.py — executes the file plan produced by
structure_architect.py. No LLM call here, by design: this is the only code
in the pipeline allowed to touch the real filesystem, so it stays
deterministic and auditable. It never invents a path itself anymore -- it
trusts (and lightly validates) the architect's plan.

Runs after structure_architect.py, before report_writer.py.

Place this file at: agents/file_manager.py (overwrite the previous version)
"""

import os
import sys
import json

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

FILE_MAP_KEY = KEYS.get("file_map", "file_map")
APP_SLUG_KEY = KEYS.get("app_slug", "app_slug")
FILE_PLAN_KEY = "file_plan"

APPS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")


def _slugify(text: str, max_len: int = 40) -> str:
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return slug[:max_len] or "untitled_app"


def _get_or_create_app_slug() -> str:
    slug = read(APP_SLUG_KEY, default=None)
    if slug:
        return slug
    idea = read(KEYS["original_idea"], default="untitled_app")
    slug = _slugify(idea)
    write(APP_SLUG_KEY, slug)
    return slug


def _ensure_app_skeleton(app_dir: str) -> None:
    os.makedirs(os.path.join(app_dir, "src"), exist_ok=True)
    os.makedirs(os.path.join(app_dir, "tests"), exist_ok=True)
    readme_path = os.path.join(app_dir, "README.md")
    if not os.path.exists(readme_path):
        idea = read(KEYS["original_idea"], default="")
        with open(readme_path, "w") as f:
            f.write(f"# {os.path.basename(app_dir)}\n\n{idea}\n\n"
                     "_Generated and maintained by the autonomous AI loop. "
                     "Do not edit by hand if the loop is still running — your "
                     "changes will be overwritten next cycle._\n")


def _safe_relpath(app_dir: str, rel_path: str) -> str:
    """Rejects any path that would escape the app directory (e.g. ../../etc)."""
    full = os.path.normpath(os.path.join(app_dir, rel_path))
    if not full.startswith(os.path.normpath(app_dir) + os.sep):
        raise ValueError(f"Rejected unsafe path from plan: {rel_path}")
    return full


def _get_module_code(fixed_code: dict, module_name: str):
    data = fixed_code.get(module_name)
    if data is None:
        return "python", ""
    if isinstance(data, dict):
        return data.get("language", "python"), data.get("code", "")
    return "python", data or ""


def run_file_manager() -> dict:
    fixed_code = read(KEYS["fixed_code"])
    plan = read(FILE_PLAN_KEY)
    if not fixed_code:
        raise ValueError("No fixed_code found in memory. Run Fixer+Tester first.")
    if not plan:
        raise ValueError("No file_plan found in memory. Run structure_architect.py first.")

    app_slug = _get_or_create_app_slug()
    app_dir = os.path.join(APPS_ROOT, app_slug)
    _ensure_app_skeleton(app_dir)

    file_map = read(FILE_MAP_KEY, default={})
    written, deleted, moved, skipped = [], [], [], []

    for op in plan.get("operations", []):
        action = op.get("action")

        try:
            if action == "mkdir":
                full = _safe_relpath(app_dir, op["path"])
                os.makedirs(full, exist_ok=True)

            elif action == "write":
                module_name = op.get("module")
                _, code = _get_module_code(fixed_code, module_name)
                if not code:
                    skipped.append({"op": op, "reason": "no code found for module"})
                    continue
                rel_path = op["path"]
                full = _safe_relpath(app_dir, rel_path)
                os.makedirs(os.path.dirname(full), exist_ok=True)
                with open(full, "w") as f:
                    f.write(code)
                file_map[module_name] = rel_path
                written.append(rel_path)

            elif action == "move":
                old_full = _safe_relpath(app_dir, op["old_path"])
                new_full = _safe_relpath(app_dir, op["new_path"])
                if os.path.exists(old_full):
                    os.makedirs(os.path.dirname(new_full), exist_ok=True)
                    os.rename(old_full, new_full)
                    moved.append({"from": op["old_path"], "to": op["new_path"]})
                module_name = op.get("module")
                if module_name:
                    file_map[module_name] = op["new_path"]

            elif action == "delete":
                rel_path = op["path"]
                # Hard safety rule: never let the plan delete README or tests/
                if rel_path == "README.md" or rel_path.startswith("tests/"):
                    skipped.append({"op": op, "reason": "protected path, refused"})
                    continue
                full = _safe_relpath(app_dir, rel_path)
                if os.path.exists(full):
                    os.remove(full)
                    deleted.append(rel_path)

            else:
                skipped.append({"op": op, "reason": f"unknown action '{action}'"})

        except (KeyError, ValueError) as exc:
            skipped.append({"op": op, "reason": str(exc)})

    write(FILE_MAP_KEY, file_map)

    summary = {
        "app_dir": app_dir,
        "app_slug": app_slug,
        "written": written,
        "moved": moved,
        "deleted": deleted,
        "skipped": skipped,
    }
    write("last_file_manager_summary", summary)
    return summary


if __name__ == "__main__":
    result = run_file_manager()
    print(json.dumps(result, indent=2))