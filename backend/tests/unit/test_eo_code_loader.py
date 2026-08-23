"""
tests/unit/test_eo_code_loader.py — Patch 7e-2.

eo/code_loader.py had zero test coverage before this. It's the bridge
that lets a tier-2 "directed task against an EXISTING codebase" fake
having gone through a tier-3 Code Writer Pool cycle -- every
downstream specialist (reviewer.py, fixer_pool.py, etc.) trusts
KEYS["submitted_code"]'s shape unconditionally, so a wrong module_key
or a missed file here doesn't fail loudly, it silently hands a
reviewer/fixer a codebase that's missing pieces or has the wrong
names, and file_manager.write_back_existing_app() then has nowhere
correct to write a fix back to.

Isolation: code_loader.py does `from memory.bus import write, KEYS`
(bound names in its own namespace) -- tests patch `write` on the
code_loader module object, same gotcha as every other cache/registry
module in this batch. APPS_ROOT is a module-level constant computed
from the file's own location at import time -- tests monkeypatch it
to a tmp_path-based apps root so nothing here ever touches the real
backend/apps/ directory.
"""
import os

import eo.code_loader as code_loader


def _use_tmp_apps_root(monkeypatch, tmp_path):
    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    monkeypatch.setattr(code_loader, "APPS_ROOT", str(apps_root))
    return apps_root


# ---------------------------------------------------------------------
# list_available_apps
# ---------------------------------------------------------------------

def test_list_available_apps_returns_empty_list_when_apps_root_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(code_loader, "APPS_ROOT", str(tmp_path / "nonexistent_apps"))
    assert code_loader.list_available_apps() == []


def test_list_available_apps_returns_sorted_directory_names(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    (apps_root / "zebra_app").mkdir()
    (apps_root / "alpha_app").mkdir()

    assert code_loader.list_available_apps() == ["alpha_app", "zebra_app"]


def test_list_available_apps_skips_files_not_just_directories(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    (apps_root / "real_app").mkdir()
    (apps_root / "stray_file.txt").write_text("not an app")

    assert code_loader.list_available_apps() == ["real_app"]


def test_list_available_apps_skips_dunder_prefixed_directories(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    (apps_root / "real_app").mkdir()
    (apps_root / "__pycache__").mkdir()

    assert code_loader.list_available_apps() == ["real_app"]


# ---------------------------------------------------------------------
# load_existing_app — missing app
# ---------------------------------------------------------------------

def test_load_existing_app_raises_file_not_found_for_unknown_slug(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    (apps_root / "real_app").mkdir()

    try:
        code_loader.load_existing_app("nonexistent_app")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as exc:
        assert "real_app" in str(exc)


def test_load_existing_app_missing_slug_error_lists_no_apps_when_none_exist(monkeypatch, tmp_path):
    _use_tmp_apps_root(monkeypatch, tmp_path)
    try:
        code_loader.load_existing_app("nonexistent_app")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError as exc:
        assert "(none)" in str(exc)


# ---------------------------------------------------------------------
# load_existing_app — real app, no src/ dir
# ---------------------------------------------------------------------

def test_load_existing_app_with_no_src_dir_returns_empty_but_sets_app_slug(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    (apps_root / "bare_app").mkdir()
    seen_writes = {}
    monkeypatch.setattr(code_loader, "write",
                         lambda key, value: seen_writes.setdefault(key, []).append(value))

    result = code_loader.load_existing_app("bare_app")

    assert result == {}
    assert seen_writes[code_loader.KEYS["app_slug"]] == ["bare_app"]
    assert seen_writes[code_loader.KEYS["submitted_code"]] == [{}]


# ---------------------------------------------------------------------
# load_existing_app — real app with src/
# ---------------------------------------------------------------------

def test_load_existing_app_reads_py_files_and_builds_module_keys(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    src_dir = apps_root / "todo_app" / "src"
    (src_dir / "todo").mkdir(parents=True)
    (src_dir / "todo" / "task_editor.py").write_text("def edit(): pass\n")
    (src_dir / "main.py").write_text("def main(): pass\n")

    seen_writes = {}
    monkeypatch.setattr(code_loader, "write",
                         lambda key, value: seen_writes.setdefault(key, []).append(value))

    result = code_loader.load_existing_app("todo_app")

    assert result["todo_task_editor"] == {"language": "python", "code": "def edit(): pass\n"}
    assert result["main"] == {"language": "python", "code": "def main(): pass\n"}
    assert len(result) == 2


def test_load_existing_app_skips_non_python_files(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    src_dir = apps_root / "mixed_app" / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "main.py").write_text("code")
    (src_dir / "README.md").write_text("docs")
    (src_dir / "data.json").write_text("{}")
    monkeypatch.setattr(code_loader, "write", lambda key, value: None)

    result = code_loader.load_existing_app("mixed_app")

    assert list(result.keys()) == ["main"]


def test_load_existing_app_writes_file_map_relative_to_src_with_forward_slashes(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    src_dir = apps_root / "todo_app" / "src"
    (src_dir / "todo").mkdir(parents=True)
    (src_dir / "todo" / "task_editor.py").write_text("code")

    seen_writes = {}
    monkeypatch.setattr(code_loader, "write",
                         lambda key, value: seen_writes.setdefault(key, []).append(value))

    code_loader.load_existing_app("todo_app")

    file_map = seen_writes[code_loader.KEYS.get("file_map", "file_map")][0]
    assert file_map["todo_task_editor"] == "src/todo/task_editor.py"


def test_load_existing_app_sets_app_slug_before_writing_submitted_code(monkeypatch, tmp_path):
    """Callers rely on KEYS["app_slug"] being namespaced BEFORE
    submitted_code/file_map are written, since every subsequent
    memory.bus call for this app must land under the right namespace
    -- pinned via write-call order."""
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    (apps_root / "empty_app" / "src").mkdir(parents=True)

    order = []
    monkeypatch.setattr(code_loader, "write", lambda key, value: order.append(key))

    code_loader.load_existing_app("empty_app")

    assert order[0] == code_loader.KEYS["app_slug"]


def test_load_existing_app_returns_the_same_dict_it_wrote_as_submitted_code(monkeypatch, tmp_path):
    apps_root = _use_tmp_apps_root(monkeypatch, tmp_path)
    src_dir = apps_root / "solo_app" / "src"
    src_dir.mkdir(parents=True)
    (src_dir / "main.py").write_text("print(1)")

    seen_writes = {}
    monkeypatch.setattr(code_loader, "write",
                         lambda key, value: seen_writes.setdefault(key, []).append(value))

    result = code_loader.load_existing_app("solo_app")

    assert result == seen_writes[code_loader.KEYS["submitted_code"]][0]
