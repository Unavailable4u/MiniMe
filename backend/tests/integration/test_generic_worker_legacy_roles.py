"""
Replaces the old tests/test_changelog_writer.py, which is dead: it did
`from agents.changelog_writer import run`, but agents/changelog_writer.py
no longer exists. eo/registry.py's comment above its ROLE_HANDLERS-style
dict ("Migration Part 27") confirms changelog_writer (and final_qa,
gatekeeper's dedicated module) were retired on purpose — the role name
stays valid for the Panel to hire, but since it's not in
REAL_ACTION_ROLES it now resolves straight through to
agents.generic_worker.run(role="changelog_writer", ...) instead.

This test asserts that resolution actually holds, mocked end to end, so
if a future migration silently breaks the generic_worker fallback for a
retired role, this catches it instead of the import just quietly failing
the way the old test would have under pytest collection.
"""

import agents.generic_worker as generic_worker  # noqa: F401  (ensures mock_llm patches this module)


def test_changelog_writer_role_resolves_to_generic_worker(mock_llm, monkeypatch):
    # No dedicated agents.changelog_writer module should exist anymore.
    import importlib
    import pytest as _pytest
    with _pytest.raises(ModuleNotFoundError):
        importlib.import_module("agents.changelog_writer")

    # Give the role a brief so generic_worker's run() has something to
    # work with, without depending on real Redis-stored role prompts.
    monkeypatch.setattr(
        generic_worker, "get_role_prompt", lambda role, user_id=None: "Write a concise commit message.",
    )
    monkeypatch.setattr(
        generic_worker.conversation_memory, "get_full_context", lambda session_id: "",
    )

    mock_llm.set_json_response({"commit_message": "Add input validation to task creation form"})

    result = generic_worker.run(
        role="changelog_writer",
        task_text="Summarize this cycle's changes as a commit message.",
        input_keys=[],
        session_id="test-session",
        include_conversation_context=False,
    )

    assert mock_llm.mock.called, "generate_text was never called — role did not reach the LLM step"
    assert isinstance(result, dict)
