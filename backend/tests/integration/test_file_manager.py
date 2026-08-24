"""
tests/integration/test_file_manager.py — rebuild of the old
tests/test_file_manager.py.

Isolates file_manager.py's own write/move/delete logic, using a
hand-written file_plan (no LLM involved at all). This checks whether
file_manager.py itself is correct GIVEN a correct plan -- i.e. rules out
file_manager.py as the bug, separately from structure_architect.py (see
test_structure_architect.py for that half).

Includes one deliberate mismatch (a module name in the plan that does NOT
exist in fixed_code) to confirm the skip path fires with a clear reason --
file_manager.py only ever writes skip reasons to memory, not the console,
so this test checks the returned summary directly instead of relying on
stdout.

Unlike the old script, this writes real files under pytest's tmp_path
(via monkeypatching APPS_ROOT) instead of the repo's own
apps/__test_file_manager_scratch/ -- no scratch directory left behind on
disk, and safe to run concurrently / on every commit.
"""
from agents import file_manager
from memory.bus import KEYS, write

FAKE_FIXED_CODE = {
    "Sandbox Engine": {
        "language": "python",
        "code": "def run_sandboxed(fn, *args):\n    return fn(*args)\n",
    },
    "Callable Validator": {
        "language": "python",
        "code": "def is_safe_callable(fn):\n    return callable(fn)\n",
    },
}

# Two ops that correctly match fixed_code keys, and one deliberately
# broken one (mismatched module name) to prove the skip path reports a
# clear reason.
FAKE_PLAN = {
    "operations": [
        {"action": "write", "module": "Sandbox Engine",
         "path": "src/sandbox/sandbox_engine.py", "reason": "test"},
        {"action": "write", "module": "Callable Validator",
         "path": "src/validation/callable_validator.py", "reason": "test"},
        {"action": "write", "module": "callable_validator",  # <- deliberate mismatch
         "path": "src/validation/duplicate.py", "reason": "test (should be skipped)"},
    ]
}

SCRATCH_SLUG = "__test_file_manager_scratch"


def _seed():
    # app_slug MUST be written first -- see test_structure_architect.py's
    # _seed_code_mode() for why (every other key here gets namespaced by
    # whatever app_slug is active AT WRITE TIME).
    write(KEYS["app_slug"], SCRATCH_SLUG)
    write(KEYS["fixed_code"], FAKE_FIXED_CODE)
    write(file_manager.FILE_PLAN_KEY, FAKE_PLAN)
    write(file_manager.FILE_MAP_KEY, {})


def test_matched_modules_written_and_mismatch_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(file_manager, "APPS_ROOT", str(tmp_path))
    _seed()

    summary = file_manager.run_file_manager()

    assert len(summary["written"]) == 2, (
        "the two correctly-matched ops should be written; if this count "
        "changed, file_manager.py's own write logic has a bug beyond the "
        "module-name-mismatch case this test also checks"
    )
    assert len(summary["skipped"]) == 1
    assert "no code found" in summary["skipped"][0]["reason"]


def test_written_files_land_on_disk_with_expected_content(tmp_path, monkeypatch):
    monkeypatch.setattr(file_manager, "APPS_ROOT", str(tmp_path))
    _seed()

    summary = file_manager.run_file_manager()

    app_dir = tmp_path / SCRATCH_SLUG
    assert summary["app_dir"] == str(app_dir)

    written_path = app_dir / "src" / "sandbox" / "sandbox_engine.py"
    assert written_path.exists()
    assert "run_sandboxed" in written_path.read_text()

    skipped_path = app_dir / "src" / "validation" / "duplicate.py"
    assert not skipped_path.exists()
