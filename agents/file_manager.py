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
        if action == "write":
            print(f"  [File Manager] plan wants module='{op.get('module')}' -> {op.get('path')}")

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
                with open(full, "w", encoding="utf-8") as f:
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
    for s in skipped:
        op = s["op"]
        label = op.get("module", op.get("path"))
        print(f"  [File Manager] skipped: {label} — {s['reason']}")

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


def write_back_existing_app() -> dict:
    """
    Tier-2's write-back path (Part 2.5 / DIRECTED_TASK_MAP's "debug" and
    "refactor" routes) -- deliberately NOT run_file_manager() above.

    run_file_manager() interprets a `file_plan` produced by
    structure_architect.py, which only runs at tier 3. Tier 2 has no such
    plan -- what it DOES have is eo/code_loader.py's own record of exactly
    which path each module was read from (KEYS["file_map"], written by
    load_existing_app() at the same time it populates submitted_code).
    This function is the write-back half of that: given code that's since
    been changed by fixer_pool.py (debug) or code_writers.py (refactor),
    write each module back to the path it was loaded from. No plan
    interpretation, no new-file placement decisions -- if a module wasn't
    part of the original load (e.g. a refactor introduced a brand-new
    file), there's no known path for it and it's skipped rather than
    guessed at.

    Prefers KEYS["fixed_code"] (debug's output) over KEYS["submitted_code"]
    (refactor's output, or code_loader's own unmodified load) when both are
    present, since fixed_code being present means fixer_pool ran this
    cycle and its output is what should win.

    Deliberately does not gate on sandbox_tester's pass/fail -- matches
    the existing tier-3 precedent, where file_manager.py already runs
    before gatekeeper.py's verdict, not after/conditional on it.
    """
    app_slug = read(APP_SLUG_KEY, default=None)
    if not app_slug:
        raise ValueError("No app_slug in memory -- write_back_existing_app() must run "
                          "after eo/code_loader.py has loaded an app.")
    app_dir = os.path.join(APPS_ROOT, app_slug)

    file_map = read(FILE_MAP_KEY, default={})
    fixed_code = read(KEYS["fixed_code"], default=None)
    submitted_code = read(KEYS["submitted_code"], default=None)
    code_source = fixed_code if fixed_code else (submitted_code or {})

    written, skipped = [], []
    for module_name, data in code_source.items():
        rel_path = file_map.get(module_name)
        if not rel_path:
            skipped.append({"module": module_name,
                             "reason": "no known on-disk path for this module "
                                       "(not part of the original code_loader "
                                       "load) -- refusing to guess a location"})
            continue
        _, code = _get_module_code(code_source, module_name)
        if not code:
            skipped.append({"module": module_name, "reason": "no code content"})
            continue
        try:
            full = _safe_relpath(app_dir, rel_path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as f:
                f.write(code)
            written.append(rel_path)
        except ValueError as exc:
            skipped.append({"module": module_name, "reason": str(exc)})

    for s in skipped:
        print(f"  [File Manager — writeback] skipped: {s['module']} — {s['reason']}")

    summary = {"app_dir": app_dir, "app_slug": app_slug, "written": written, "skipped": skipped}
    write("last_file_manager_summary", summary)
    return summary


def write_back_test_code() -> dict:
    """
    Tier-2's "add_tests" write-back path (DIRECTED_TASK_MAP["add_tests"] =
    ["test_writer", "sandbox_tester", ...]). Deliberately separate from
    write_back_existing_app(): that function re-writes a module's OWN
    source file back to its original on-disk path, from KEYS["fixed_code"]/
    KEYS["submitted_code"]. add_tests never changes a module's own source
    -- it only generates NEW test code (KEYS["test_code"], written by
    test_writer.py) that belongs in its own file under tests/.

    The module source and its generated test are stitched together with
    the exact same format sandbox_tester.py's _run_one_module() already
    uses to execute them (module code + a comment separator + the test
    code, run as one script) -- kept identical on purpose, so the file
    that lands on disk is the same thing that already passed or failed
    in the sandbox, not a reinterpretation of it.

    Same fixed_code-preferred-if-present, else submitted_code fallback as
    write_back_existing_app() -- but per sandbox_tester.py's own comment,
    add_tests never runs Fixer Pool, so in practice this will always come
    from submitted_code.

    Skips a module if:
      - it has no generated test code at all (test_writer.py may have
        dropped it -- e.g. an unsafe bare `except`, or malformed model
        output), or
      - its only "test" is the literal placeholder "# no testable logic"
        (Part 4's rule for modules with nothing meaningfully testable) --
        writing that alone as a test file adds no value, or
      - no module source can be found to stitch the test against.
    """
    app_slug = read(APP_SLUG_KEY, default=None)
    if not app_slug:
        raise ValueError("No app_slug in memory -- write_back_test_code() must run "
                          "after eo/code_loader.py has loaded an app.")
    app_dir = os.path.join(APPS_ROOT, app_slug)
    os.makedirs(os.path.join(app_dir, "tests"), exist_ok=True)

    fixed_code = read(KEYS["fixed_code"], default=None)
    submitted_code = read(KEYS["submitted_code"], default=None)
    code_source = fixed_code if fixed_code else (submitted_code or {})
    test_code_map = read(KEYS["test_code"], default={})

    written, skipped = [], []
    for module_name, test_code in test_code_map.items():
        if not isinstance(test_code, str) or not test_code.strip():
            skipped.append({"module": module_name, "reason": "no generated test code"})
            continue
        if test_code.strip() == "# no testable logic":
            skipped.append({"module": module_name, "reason": "nothing meaningfully testable"})
            continue
        _, module_code = _get_module_code(code_source, module_name)
        if not module_code:
            skipped.append({"module": module_name,
                             "reason": "no module source found to stitch tests against"})
            continue
        # Same stitching format as sandbox_tester.py's _run_one_module() --
        # do not let this drift from that format.
        full_code = (module_code.rstrip()
                     + "\n\n# --- Generated tests (Test Writer) ---\n" + test_code)
        try:
            rel_path = os.path.join("tests", f"test_{module_name}.py")
            full = _safe_relpath(app_dir, rel_path)
            with open(full, "w", encoding="utf-8") as f:
                f.write(full_code)
            written.append(rel_path)
        except ValueError as exc:
            skipped.append({"module": module_name, "reason": str(exc)})

    for s in skipped:
        print(f"  [File Manager — test writeback] skipped: {s['module']} — {s['reason']}")

    summary = {"app_dir": app_dir, "app_slug": app_slug, "written": written, "skipped": skipped}
    write("last_file_manager_summary", summary)
    return summary
    result = run_file_manager()
    print(json.dumps(result, indent=2))