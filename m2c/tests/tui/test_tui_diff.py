"""Patch C09a: building and collapsing diffs (m2c.tui.diff)."""
from m2c.tui.diff import (HEAD_LINES, MAX_DIFF_LINES, MAX_INPUT_CHARS, Existing, collapse, diff_for_call, expand,
                          unified_diff)


def kinds(diff):
    return [line.kind for line in diff.lines]


def test_edit_file_shows_old_to_new():
    d = diff_for_call("edit_file", {"path": "a.py", "old_string": "x = 1\n", "new_string": "x = 2\ny = 3\n"})
    assert (d.added, d.removed) == (2, 1)
    assert [(l.kind, l.text) for l in d.lines if l.kind != "hunk"] == [("del", "x = 1"), ("add", "x = 2"), ("add", "y = 3")]
    assert d.title == "a.py  +2 -1"


def test_edit_file_replace_all_is_noted():
    d = diff_for_call("edit_file", {"path": "a", "old_string": "a", "new_string": "b", "replace_all": True})
    assert "every occurrence" in d.title


def test_write_file_against_existing_text():
    d = diff_for_call("write_file", {"path": "f", "content": "one\ntwo\nthree\n"},
                      lambda p: Existing("ok", "one\nTWO\nthree\n"))
    assert (d.added, d.removed) == (1, 1)


def test_write_file_new_file_and_unreadable_file():
    new = diff_for_call("write_file", {"path": "f", "content": "a\nb\n"}, lambda p: Existing("missing"))
    assert new.note == "new file" and (new.added, new.removed) == (2, 0)
    protected = diff_for_call("write_file", {"path": ".env", "content": "a\n"}, lambda p: Existing("unreadable"))
    assert protected.note == "current contents not shown"
    no_reader = diff_for_call("write_file", {"path": "f", "content": "a\n"})
    assert no_reader.note == "current contents not shown"


def test_other_tools_and_malformed_input_have_no_diff():
    assert diff_for_call("bash", {"command": "ls"}) is None
    assert diff_for_call("read_file", {"path": "a"}) is None
    assert diff_for_call("edit_file", {"path": "a", "old_string": 1, "new_string": "b"}) is None
    assert diff_for_call("write_file", "not a dict") is None
    assert diff_for_call("write_file", {"content": "x"}) is None


def test_a_very_large_diff_is_collapsed_and_expands_bounded():
    content = "\n".join(f"line {i}" for i in range(30_000))
    d = diff_for_call("write_file", {"path": "big.txt", "content": content}, lambda p: Existing("missing"))
    assert d.added == 30_000                       # the count is of the whole change
    shown, hidden = collapse(d)
    assert len(shown) == HEAD_LINES and hidden == len(d.lines) - HEAD_LINES
    full = expand(d)
    assert len(full) == MAX_DIFF_LINES + 1 and full[-1].kind == "note" and "not shown" in full[-1].text


def test_a_small_diff_is_not_collapsed():
    d = unified_diff("a", "x\n", "y\n")
    shown, hidden = collapse(d)
    assert hidden == 0 and shown == d.lines


def test_huge_input_is_not_diffed_line_by_line():
    big = "a\n" * (MAX_INPUT_CHARS // 2 + 10)
    d = unified_diff("a", big, big + "b\n")
    assert len(d.lines) == 1 and d.lines[0].kind == "note" and d.added > 0


def test_diff_text_is_cleaned():
    d = diff_for_call("edit_file", {"path": "a\x1b[31m", "old_string": "x\x1b]0;t\x07", "new_string": "y\u202e"})
    text = d.title + "".join(l.text for l in d.lines)
    assert "\x1b" not in text and "\u202e" not in text and "\x07" not in text
