"""
tests/unit/test_eo_rolling_summary_b10.py — Patch B10.

Covers the two mitigations Patch B10 adds on top of Patch B9's fold
mechanism (see eo/rolling_summary.py's module docstring):

  1. Re-grounding: every REGROUND_EVERY_FOLDS-th fold uses the
     'rolling_summary_reground' role with a raw-turn buffer instead of
     chaining another incremental edit onto the existing summary via
     'rolling_summarizer'; the raw buffer itself stays capped at
     RAW_BUFFER_MAX_ENTRIES entries.
  2. Fact routing: fold_turns(), given an owner_id, checks each turn
     against fact_summarizer.extract_fact() and — when the model judges
     it durable — writes it into workspace_facts.py (Tier C) via
     record_section_entry(), independent of and prior to the fold
     itself; failures at any step here must never block the fold.

Isolation: same as test_eo_rolling_summary.py — rolling_summary.py does
`from memory.bus import read, write` (bound names), so the autouse
`fake_bus` fixture already covers it. `from eo import chat_workspace,
fact_summarizer, workspace_facts` are module imports, patched here as
`rolling_summary.chat_workspace.X` / `rolling_summary.fact_summarizer.X`
/ `rolling_summary.workspace_facts.X`, same convention
test_eo_conversation_memory.py already uses for its own eo.* imports.
The deferred `from agents.generic_worker import run as run_role` means
the call site to patch for the fold call itself is still
`agents.generic_worker.run`, exactly as in test_eo_rolling_summary.py.
"""
from unittest.mock import MagicMock

from eo import rolling_summary


def _mock_run(text="Updated summary."):
    return MagicMock(return_value={"text": text})


# ---------------------------------------------------------------------
# Re-grounding
# ---------------------------------------------------------------------

def test_incremental_folds_use_rolling_summarizer_role(monkeypatch):
    mock_run = _mock_run()
    monkeypatch.setattr("agents.generic_worker.run", mock_run)

    # 1st..5th folds (fold_count 1..5) are all incremental —
    # REGROUND_EVERY_FOLDS defaults to 6.
    for i in range(rolling_summary.REGROUND_EVERY_FOLDS - 1):
        rolling_summary.fold_turns("sess-b10-1", [{"role": "user", "text": f"turn {i}"}])

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["role"] == "rolling_summarizer"
    assert "RAW TURNS" not in call_kwargs["task_text"]


def test_every_nth_fold_regrounds_against_raw_turns(monkeypatch):
    mock_run = _mock_run()
    monkeypatch.setattr("agents.generic_worker.run", mock_run)

    session_id = "sess-b10-2"
    for i in range(rolling_summary.REGROUND_EVERY_FOLDS):
        rolling_summary.fold_turns(session_id, [{"role": "user", "text": f"turn {i}"}])

    call_kwargs = mock_run.call_args.kwargs
    assert call_kwargs["role"] == "rolling_summary_reground"
    assert "RAW TURNS" in call_kwargs["task_text"]
    # every folded turn so far should still be inside the raw buffer,
    # since REGROUND_EVERY_FOLDS folds happened and the buffer caps at
    # exactly that many entries
    for i in range(rolling_summary.REGROUND_EVERY_FOLDS):
        assert f"turn {i}" in call_kwargs["task_text"]


def test_raw_buffer_caps_at_reground_every_folds(monkeypatch):
    monkeypatch.setattr("agents.generic_worker.run", _mock_run())

    session_id = "sess-b10-3"
    total_folds = rolling_summary.REGROUND_EVERY_FOLDS + 3
    for i in range(total_folds):
        rolling_summary.fold_turns(session_id, [{"role": "user", "text": f"turn {i}"}])

    buffer = rolling_summary.read(rolling_summary._raw_buffer_key(session_id), default=[])
    assert len(buffer) == rolling_summary.RAW_BUFFER_MAX_ENTRIES
    # the oldest folded turns should have aged out of the buffer
    assert not any("turn 0]" in entry or "turn 0\n" in entry or entry.strip().endswith("turn 0") for entry in buffer)


def test_reground_pass_still_persists_and_truncates_like_incremental(monkeypatch):
    long_text = "y" * (rolling_summary.MAX_SUMMARY_CHARS + 200)
    monkeypatch.setattr("agents.generic_worker.run", _mock_run(long_text))

    session_id = "sess-b10-4"
    for i in range(rolling_summary.REGROUND_EVERY_FOLDS):
        result = rolling_summary.fold_turns(session_id, [{"role": "user", "text": f"turn {i}"}])

    assert result.endswith("...")
    assert len(result) <= rolling_summary.MAX_SUMMARY_CHARS + 3
    assert rolling_summary.get_summary(session_id) == result


# ---------------------------------------------------------------------
# Durable-fact routing
# ---------------------------------------------------------------------

def test_route_durable_facts_noop_without_owner_id(monkeypatch):
    mock_workspace_lookup = MagicMock()
    monkeypatch.setattr(rolling_summary.chat_workspace, "workspace_for_chat", mock_workspace_lookup)
    monkeypatch.setattr("agents.generic_worker.run", _mock_run())

    rolling_summary.fold_turns("sess-b10-5", [{"role": "user", "text": "We decided to use Postgres."}])

    mock_workspace_lookup.assert_not_called()


def test_route_durable_facts_noop_without_workspace(monkeypatch):
    monkeypatch.setattr(rolling_summary.chat_workspace, "workspace_for_chat", lambda *a, **kw: None)
    mock_extract = MagicMock()
    monkeypatch.setattr(rolling_summary.fact_summarizer, "extract_fact", mock_extract)
    monkeypatch.setattr("agents.generic_worker.run", _mock_run())

    rolling_summary.fold_turns("sess-b10-6", [{"role": "user", "text": "hi"}], owner_id="user-1")

    mock_extract.assert_not_called()


def test_worth_remembering_fact_is_written_to_workspace_facts(monkeypatch):
    monkeypatch.setattr(rolling_summary.chat_workspace, "workspace_for_chat",
                         lambda *a, **kw: {"id": "ws-1"})
    monkeypatch.setattr(rolling_summary.fact_summarizer, "extract_fact",
                         lambda *a, **kw: {"worth_remembering": True, "category": "decision",
                                            "title": "Use Postgres", "summary": "The team decided to use Postgres."})
    mock_record = MagicMock()
    monkeypatch.setattr(rolling_summary.workspace_facts, "record_section_entry", mock_record)
    monkeypatch.setattr("agents.generic_worker.run", _mock_run())

    rolling_summary.fold_turns(
        "sess-b10-7",
        [{"role": "user", "text": "We decided to use Postgres."}],
        owner_id="user-1",
    )

    mock_record.assert_called_once()
    args, kwargs = mock_record.call_args
    assert args[0] == "ws-1"
    assert args[1] == "decisions"   # CATEGORY_TO_SECTION["decision"]
    assert args[2]["title"] == "Use Postgres"
    assert kwargs["source"] == "rolling_summary_fold"
    assert kwargs["source_ref"] == "sess-b10-7"


def test_fact_not_worth_remembering_is_not_written(monkeypatch):
    monkeypatch.setattr(rolling_summary.chat_workspace, "workspace_for_chat",
                         lambda *a, **kw: {"id": "ws-1"})
    monkeypatch.setattr(rolling_summary.fact_summarizer, "extract_fact", lambda *a, **kw: None)
    mock_record = MagicMock()
    monkeypatch.setattr(rolling_summary.workspace_facts, "record_section_entry", mock_record)
    monkeypatch.setattr("agents.generic_worker.run", _mock_run())

    rolling_summary.fold_turns("sess-b10-8", [{"role": "user", "text": "lol ok"}], owner_id="user-1")

    mock_record.assert_not_called()


def test_fact_routing_failure_does_not_block_the_fold(monkeypatch):
    monkeypatch.setattr(rolling_summary.chat_workspace, "workspace_for_chat",
                         MagicMock(side_effect=RuntimeError("db down")))
    monkeypatch.setattr("agents.generic_worker.run", _mock_run("Fold still happened."))

    result = rolling_summary.fold_turns(
        "sess-b10-9", [{"role": "user", "text": "hi"}], owner_id="user-1"
    )

    assert result == "Fold still happened."


def test_fold_turns_async_forwards_owner_id(monkeypatch):
    captured = {}

    def fake_fold_turns(session_id, turns, owner_id=None):
        captured["session_id"] = session_id
        captured["turns"] = turns
        captured["owner_id"] = owner_id

    monkeypatch.setattr(rolling_summary, "fold_turns", fake_fold_turns)

    thread_holder = {}
    import threading
    real_thread = threading.Thread

    def capturing_thread(*args, **kwargs):
        t = real_thread(*args, **kwargs)
        thread_holder["thread"] = t
        return t

    monkeypatch.setattr(rolling_summary.threading, "Thread", capturing_thread)

    rolling_summary.fold_turns_async("sess-b10-10", [{"role": "user", "text": "hi"}], owner_id="user-9")
    thread_holder["thread"].join()

    assert captured == {
        "session_id": "sess-b10-10",
        "turns": [{"role": "user", "text": "hi"}],
        "owner_id": "user-9",
    }
