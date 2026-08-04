"""
tests/integration/test_structure_architect.py — mocked rebuild of the old
tests/test_structure_architect.py.

The old version hit a real LLM specifically to check whether the model
itself normalizes module names (e.g. "Sandbox Engine" -> "sandbox_engine")
in a way that would break file_manager.py's exact-match lookup. That's a
real-model-behavior question, not something a mocked test can answer --
it belongs in tests/manual/ against a live provider, not here.

What DOES belong here, and is exactly the "per-agent expected-JSON-shape
checking" sector 2 calls for:
  - code mode's normal path (operations reference the given module names)
  - code mode's fallback when the LLM's JSON is unparseable (one "write"
    op per module at a flat default path -- file_manager.py must always
    get something valid to execute)
  - the newer no-code mode (a task with neither fixed_code nor
    submitted_code plans a plain folder/file scaffold instead of raising)
  - the mermaid string always gets attached and written to FILE_PLAN_KEY
"""
import json

import agents.structure_architect as structure_architect  # noqa: F401
from memory.bus import write, read, KEYS
from agents.structure_architect import FILE_PLAN_KEY

FIXED_CODE = {
    "todo_storage": {"language": "python", "code": "def add_todo(todos, item):\n    return todos\n"},
    "todo_api": {"language": "python", "code": "def get_todo(todos, index):\n    return todos[index]\n"},
}


def _seed_code_mode():
    # app_slug MUST be written first -- every other key here gets
    # namespaced by whatever app_slug is active AT WRITE TIME (see
    # memory/bus.py's _namespaced()/_current_app_slug()), so seeding
    # fixed_code before app_slug would file it under the wrong (or no)
    # namespace and run_structure_architect()'s own read() -- which runs
    # after this test's app_slug is active -- would silently miss it.
    write(KEYS["app_slug"], "__test_structure_architect_scratch")
    write(KEYS["fixed_code"], FIXED_CODE)
    write(KEYS["file_map"], {})


def test_code_mode_produces_one_write_op_per_module(mock_llm):
    _seed_code_mode()
    mock_llm.set_json_response({
        "operations": [
            {"action": "write", "module": "todo_storage", "path": "src/todo_storage.py", "reason": "new"},
            {"action": "write", "module": "todo_api", "path": "src/todo_api.py", "reason": "new"},
        ]
    })

    plan = structure_architect.run_structure_architect()

    ops = plan["operations"]
    covered = {op["module"] for op in ops if op.get("module")}
    assert covered == set(FIXED_CODE.keys()), (
        "every module in fixed_code must appear in exactly one operation -- "
        "if a name here doesn't exactly match a fixed_code key, "
        "file_manager.py's exact-match lookup silently skips the write"
    )
    assert "mermaid" in plan
    assert read(FILE_PLAN_KEY) == plan


def test_code_mode_falls_back_safely_on_unparseable_json(mock_llm):
    _seed_code_mode()
    mock_llm.set_response("Sure! Here's my plan: not actually JSON at all.")

    plan = structure_architect.run_structure_architect()

    ops = plan["operations"]
    covered = {op["module"] for op in ops if op.get("module")}
    assert covered == set(FIXED_CODE.keys()), "fallback must still cover every module"
    for op in ops:
        assert op["action"] == "write"
        assert op["path"].startswith("src/")


def test_no_code_mode_plans_a_folder_scaffold_instead_of_raising(mock_llm):
    """Bug-fix regression guard: neither fixed_code nor submitted_code
    present must NOT raise -- it plans a plain folder/file layout from
    task_text instead."""
    write(KEYS["app_slug"], "__test_structure_architect_scratch_nocode")

    mock_llm.set_json_response({
        "operations": [
            {"action": "mkdir", "path": "memories/1998", "reason": "per-year folder"},
            {"action": "write", "path": "memories/1998/notes.md", "content": "# 1998\n", "reason": "placeholder"},
        ]
    })

    plan = structure_architect.run_structure_architect(
        task_text="Make a folder for every year from 1998 to keep memories in."
    )

    ops = plan["operations"]
    assert any(op["action"] == "mkdir" for op in ops)
    assert all("module" not in op for op in ops), "no-code plan ops must never carry a 'module' field"
    assert "mermaid" in plan


def test_no_code_mode_fallback_on_unparseable_json(mock_llm):
    write(KEYS["app_slug"], "__test_structure_architect_scratch_nocode_fallback")
    mock_llm.set_response("not json")

    plan = structure_architect.run_structure_architect(task_text="organize my files")

    assert plan["operations"] == [
        {"action": "mkdir", "path": "files", "reason": "fallback: architect output was not valid JSON"},
    ]
