"""
tests/unit/test_eo_rolling_summary.py — Patch B9.

Covers:
  1. get_summary() on a session with nothing folded yet returns "" (the
     same no-history-yet convention every other read in this module
     tree uses).
  2. fold_turns() calls the 'rolling_summarizer' role with both the
     existing summary and the new turn(s), and persists whatever it
     returns under the "conversation:{session_id}:summary" key.
  3. fold_turns()'s MAX_SUMMARY_CHARS truncation.
  4. fold_turns()'s fail-quiet contract on a falsy session_id/empty
     turns list, and on the role call raising.
  5. conversation_memory.append_turn()'s integration point: once a turn
     is trimmed off Tier A, it's handed to rolling_summary instead of
     just discarded.

Isolation: rolling_summary.py does `from memory.bus import read, write`
(names bound directly into its own namespace), so the autouse `fake_bus`
fixture already covers it with zero per-test setup. The deferred
`from agents.generic_worker import run as run_role` import means the
call site to patch is `agents.generic_worker.run`.
"""
from unittest.mock import MagicMock

import pytest

from eo import conversation_memory, rolling_summary


@pytest.fixture(autouse=True)
def _no_workspace_context(monkeypatch):
    """Same reasoning as test_eo_conversation_memory.py's own fixture of
    this name — keep the workspace-facts/linked-chat/note-taker
    collaborators as their real "nothing to add" shape so these tests
    can focus on the rolling-summary integration point specifically."""
    monkeypatch.setattr(conversation_memory.chat_workspace, "workspace_for_chat",
                         lambda *a, **kw: None)
    monkeypatch.setattr(conversation_memory.chat_store, "get_linked_context_text",
                         lambda *a, **kw: "")


def test_get_summary_defaults_to_empty_string():
    assert rolling_summary.get_summary("sess-1") == ""


def test_get_summary_falsy_session_id_is_safe():
    assert rolling_summary.get_summary(None) == ""
    assert rolling_summary.get_summary("") == ""


def test_fold_turns_persists_role_output(monkeypatch):
    mock_run = MagicMock(return_value={"text": "Updated summary text."})
    monkeypatch.setattr("agents.generic_worker.run", mock_run)

    result = rolling_summary.fold_turns(
        "sess-2", [{"role": "user", "text": "We decided to use Postgres."}]
    )

    assert result == "Updated summary text."
    assert rolling_summary.get_summary("sess-2") == "Updated summary text."

    # role, existing summary, and the new turn text all reached the call
    _, kwargs = mock_run.call_args
    assert kwargs.get("role") == "rolling_summarizer" or mock_run.call_args.kwargs.get("role") == "rolling_summarizer"
    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["session_id"] == "sess-2"
    assert call_kwargs["include_conversation_context"] is False
    assert "(none yet)" in call_kwargs["task_text"]
    assert "We decided to use Postgres." in call_kwargs["task_text"]


def test_fold_turns_merges_with_existing_summary(monkeypatch):
    mock_run = MagicMock(return_value={"text": "Merged summary."})
    monkeypatch.setattr("agents.generic_worker.run", mock_run)

    rolling_summary.fold_turns("sess-3", [{"role": "user", "text": "First fact."}])
    rolling_summary.fold_turns("sess-3", [{"role": "assistant", "text": "Second fact."}])

    call_kwargs = mock_run.call_args.kwargs
    assert "Merged summary." in call_kwargs["task_text"]  # prior stored summary was fed back in
    assert "Second fact." in call_kwargs["task_text"]


def test_fold_turns_truncates_to_max_chars(monkeypatch):
    long_text = "x" * (rolling_summary.MAX_SUMMARY_CHARS + 500)
    monkeypatch.setattr("agents.generic_worker.run", MagicMock(return_value={"text": long_text}))

    result = rolling_summary.fold_turns("sess-4", [{"role": "user", "text": "hi"}])

    assert len(result) <= rolling_summary.MAX_SUMMARY_CHARS + 3  # + "..."
    assert result.endswith("...")


def test_fold_turns_noop_on_falsy_input(monkeypatch):
    mock_run = MagicMock()
    monkeypatch.setattr("agents.generic_worker.run", mock_run)

    assert rolling_summary.fold_turns("", [{"role": "user", "text": "hi"}]) is None
    assert rolling_summary.fold_turns("sess-5", []) is None
    mock_run.assert_not_called()


def test_fold_turns_swallows_role_exception(monkeypatch):
    monkeypatch.setattr("agents.generic_worker.run", MagicMock(side_effect=RuntimeError("boom")))

    result = rolling_summary.fold_turns("sess-6", [{"role": "user", "text": "hi"}])

    assert result is None
    assert rolling_summary.get_summary("sess-6") == ""  # nothing written on failure


def test_append_turn_folds_dropped_turns_instead_of_discarding(monkeypatch):
    """Fill Tier A past MAX_STORED_TURNS and confirm the oldest turn(s)
    get handed to rolling_summary.fold_turns_async() rather than just
    vanishing — the actual behavior change this patch makes."""
    captured = {}

    def fake_fold_async(session_id, turns, owner_id=None):
        captured["session_id"] = session_id
        captured["turns"] = turns
        captured["owner_id"] = owner_id

    monkeypatch.setattr(conversation_memory.rolling_summary, "fold_turns_async", fake_fold_async)

    session_id = "sess-7"
    for i in range(conversation_memory.MAX_STORED_TURNS + 1):
        conversation_memory.append_turn(session_id, "user", f"turn {i}")

    assert captured["session_id"] == session_id
    assert captured["turns"] == [{"role": "user", "text": "turn 0"}]


def test_append_turn_does_not_fold_when_under_cap(monkeypatch):
    mock_fold = MagicMock()
    monkeypatch.setattr(conversation_memory.rolling_summary, "fold_turns_async", mock_fold)

    conversation_memory.append_turn("sess-8", "user", "just one turn")

    mock_fold.assert_not_called()


def test_get_full_context_prepends_summary_when_present(monkeypatch):
    monkeypatch.setattr(conversation_memory.rolling_summary, "get_summary",
                         lambda session_id: "Earlier: the user set up Postgres.")
    conversation_memory.append_turn("sess-9", "user", "What was the DB again?")

    context = conversation_memory.get_full_context("sess-9")

    assert "Earlier: the user set up Postgres." in context
    assert context.index("Earlier: the user set up Postgres.") < context.index("What was the DB again?")
