"""
tests/unit/test_eo_scratchpad.py — Patch B7.

eo/scratchpad.py is a small memory.bus-backed store, same shape family
as eo/tool_budget.py (B6) — no mocking needed beyond the autouse
fake_bus fixture already used across this suite. Covers: write/resolve/
list, the safety-net cap, and the two ways a scratchpad ends up empty
(explicit resolve_note() calls vs. the end-of-task clear_scratchpad()
backstop) — the latter is the specific property B7's acceptance
criterion cares about: no scratchpad key survives task completion, even
if nothing was ever explicitly resolved.
"""
from eo import scratchpad


def test_write_note_returns_an_id():
    note_id = scratchpad.write_note("session-a", "remember to check X")
    assert isinstance(note_id, str) and note_id


def test_write_note_appends_to_list_notes():
    scratchpad.write_note("session-a", "first note")
    scratchpad.write_note("session-a", "second note")
    notes = scratchpad.list_notes("session-a")
    assert [n["text"] for n in notes] == ["first note", "second note"]


def test_notes_are_scoped_per_session():
    scratchpad.write_note("session-a", "a's note")
    scratchpad.write_note("session-b", "b's note")
    assert [n["text"] for n in scratchpad.list_notes("session-a")] == ["a's note"]
    assert [n["text"] for n in scratchpad.list_notes("session-b")] == ["b's note"]


def test_list_notes_empty_for_unknown_session():
    assert scratchpad.list_notes("never-written") == []


def test_resolve_note_removes_only_that_note():
    id1 = scratchpad.write_note("session-a", "keep")
    id2 = scratchpad.write_note("session-a", "resolve me")
    resolved = scratchpad.resolve_note("session-a", id2)
    assert resolved is True
    remaining = scratchpad.list_notes("session-a")
    assert len(remaining) == 1
    assert remaining[0]["id"] == id1


def test_resolve_note_returns_false_for_unknown_id():
    scratchpad.write_note("session-a", "note")
    assert scratchpad.resolve_note("session-a", "not-a-real-id") is False


def test_resolve_note_returns_false_for_unknown_session():
    assert scratchpad.resolve_note("never-written", "whatever") is False


def test_safety_net_cap_drops_oldest_notes():
    ids = [scratchpad.write_note("session-a", f"note {i}")
           for i in range(scratchpad.MAX_SCRATCHPAD_NOTES + 5)]
    notes = scratchpad.list_notes("session-a")
    assert len(notes) == scratchpad.MAX_SCRATCHPAD_NOTES
    # oldest 5 got dropped; the newest MAX_SCRATCHPAD_NOTES survive, in order
    assert [n["id"] for n in notes] == ids[5:]


def test_clear_scratchpad_empties_notes_that_were_never_resolved():
    scratchpad.write_note("session-a", "note 1")
    scratchpad.write_note("session-a", "note 2")
    scratchpad.clear_scratchpad("session-a")
    assert scratchpad.list_notes("session-a") == []


def test_clear_scratchpad_only_affects_its_own_session():
    scratchpad.write_note("session-a", "a's note")
    scratchpad.write_note("session-b", "b's note")
    scratchpad.clear_scratchpad("session-a")
    assert scratchpad.list_notes("session-a") == []
    assert [n["text"] for n in scratchpad.list_notes("session-b")] == ["b's note"]


def test_clear_scratchpad_is_safe_on_an_empty_session():
    # No prior write_note() call at all — clear_scratchpad() must not raise.
    scratchpad.clear_scratchpad("never-written")
    assert scratchpad.list_notes("never-written") == []
