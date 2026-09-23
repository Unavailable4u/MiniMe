"""
tests/unit/test_eo_code_edit_apply.py — W5.2.

Covers eo/code_edit_apply.py: the pure, deterministic half of the
code-edit pipeline (no LLM, no DB — nothing to mock).

  1. Exact match, whole-line and mid-line, incl. the trailing-newline repair.
  2. Whitespace-normalised match: dropped / added indent, re-indented
     `replace`, blank edge lines, collapsed runs of spaces.
  3. Failures: not found (closest-line hint, gutter hint), ambiguous,
     ambiguous-but-a-range-disambiguates, malformed edits.
  4. create / delete / net-op bookkeeping, existing_paths, unknown_file.
  5. CRLF / lone-CR / mixed line endings.
  6. Multi-edit files: later edits see earlier ones, skipped-after-failure,
     unchanged, all failures collected.
  7. Scope: scope_violation flags, range tracking through earlier edits.
  8. validate_path, max_file_chars, diff_stats, to_edit_files.
"""
import pytest

from eo.code_edit_apply import (
    EditScope,
    apply_edits,
    diff_stats,
    find_matches,
    format_failures,
)

SRC = "def f():\n    x = 1\n    return x\n"


def _replace(path, search, replace):
    return {"path": path, "op": "replace", "search": search, "replace": replace}


def _one(result):
    assert result.ok, format_failures(result.failures)
    assert len(result.files) == 1
    return result.files[0]


# ---------------------------------------------------------------------------
# 1. exact
# ---------------------------------------------------------------------------
class TestExact:
    def test_single_line_mid_line(self):
        f = _one(apply_edits([_replace("a.py", "x = 1", "x = 2")], {"a.py": SRC}))
        assert f.proposed == "def f():\n    x = 2\n    return x\n"
        assert f.original == SRC
        assert f.op == "replace"
        assert f.match_kinds == ["exact"]

    def test_only_the_matched_text_changes(self):
        f = _one(apply_edits([_replace("a.py", "x = 1", "x = 2")], {"a.py": SRC}))
        assert f.proposed.replace("x = 2", "x = 1") == SRC

    def test_whole_line_block_with_trailing_newline(self):
        f = _one(apply_edits(
            [_replace("a.py", "    x = 1\n", "    x = 2\n    y = 3\n")], {"a.py": SRC}))
        assert f.proposed == "def f():\n    x = 2\n    y = 3\n    return x\n"

    def test_search_ends_with_newline_replace_forgot_it(self):
        # Without the repair this would glue "x = 2" onto "return x".
        f = _one(apply_edits([_replace("a.py", "    x = 1\n", "    x = 2")], {"a.py": SRC}))
        assert f.proposed == "def f():\n    x = 2\n    return x\n"

    def test_replace_adds_newline_search_lacked_it(self):
        # Without the repair this would leave a stray blank line.
        f = _one(apply_edits([_replace("a.py", "    x = 1", "    x = 2\n")], {"a.py": SRC}))
        assert f.proposed == "def f():\n    x = 2\n    return x\n"

    def test_empty_replace_deletes_whole_lines(self):
        f = _one(apply_edits([_replace("a.py", "    x = 1\n", "")], {"a.py": SRC}))
        assert f.proposed == "def f():\n    return x\n"

    def test_match_at_end_of_file_without_trailing_newline(self):
        f = _one(apply_edits([_replace("a.py", "b", "c")], {"a.py": "a\nb"}))
        assert f.proposed == "a\nc"

    def test_replace_text_containing_regex_and_backslashes_is_literal(self):
        text = 'pattern = r"\\d+"\n'
        f = _one(apply_edits([_replace("a.py", '"\\d+"', '"\\w+\\1"')], {"a.py": text}))
        assert f.proposed == 'pattern = r"\\w+\\1"\n'

    def test_overlapping_hits_count_as_ambiguous(self):
        assert len(find_matches("aaa", "aa")) == 2


# ---------------------------------------------------------------------------
# 2. whitespace-normalised
# ---------------------------------------------------------------------------
class TestWhitespaceMatch:
    def test_model_dropped_the_indent_replace_is_reindented(self):
        f = _one(apply_edits(
            [_replace("a.py", "x = 1\nreturn x", "x = 2\nreturn x * 2")], {"a.py": SRC}))
        assert f.proposed == "def f():\n    x = 2\n    return x * 2\n"
        assert f.match_kinds == ["whitespace"]

    def test_model_added_indent_replace_is_dedented(self):
        f = _one(apply_edits(
            [_replace("a.py", "        x = 1\n        return x\n",
                      "        x = 2\n        return x * 2\n")], {"a.py": SRC}))
        assert f.proposed == "def f():\n    x = 2\n    return x * 2\n"

    def test_collapsed_runs_of_spaces(self):
        f = _one(apply_edits([_replace("a.py", "    return   x\n", "    return 0\n")],
                             {"a.py": SRC}))
        assert f.proposed == "def f():\n    x = 1\n    return 0\n"
        assert f.match_kinds == ["whitespace"]

    def test_blank_edge_lines_in_search_do_not_add_blank_lines(self):
        f = _one(apply_edits(
            [_replace("a.py", "\n    x = 1\n\n", "\n    x = 2\n\n")], {"a.py": SRC}))
        assert f.proposed == "def f():\n    x = 2\n    return x\n"

    def test_non_uniform_indent_is_not_guessed_at(self):
        # search has 0 / 2 / 0-space lines vs the file's 4 / 4 / 4: no single
        # delta, so replace is used as written (no re-indent).
        text = "    a = 1\n    b = 2\n"
        f = _one(apply_edits(
            [_replace("a.py", "a = 1\n  b = 2\n", "a = 9\n  b = 9\n")], {"a.py": text}))
        assert f.proposed == "a = 9\n  b = 9\n"

    def test_tab_indented_file(self):
        text = "if x:\n\tfoo()\n\tbar()\n"
        f = _one(apply_edits(
            [_replace("a.py", "foo()\nbar()", "foo()\nbaz()\nbar()")], {"a.py": text}))
        assert f.proposed == "if x:\n\tfoo()\n\tbaz()\n\tbar()\n"

    def test_exact_match_is_preferred_over_whitespace(self):
        text = "a = 1\na  =  1\n"
        # exact hit on line 1 only; the sloppy-spaced line 2 must not make it ambiguous
        f = _one(apply_edits([_replace("a.py", "a = 1", "a = 5")], {"a.py": text}))
        assert f.proposed == "a = 5\na  =  1\n"
        assert f.match_kinds == ["exact"]

    def test_whitespace_match_can_delete_lines(self):
        f = _one(apply_edits([_replace("a.py", "x = 1\n", "")], {"a.py": SRC.replace("x = 1", "x  =  1")}))
        assert f.proposed == "def f():\n    return x\n"


# ---------------------------------------------------------------------------
# 3. failures
# ---------------------------------------------------------------------------
class TestFailures:
    def test_not_found_names_the_closest_line(self):
        r = apply_edits([_replace("a.py", "    x = 100\n", "")], {"a.py": SRC})
        assert not r.ok and not r.files
        (fail,) = r.failures
        assert fail.code == "not_found"
        assert fail.index == 0 and fail.path == "a.py"
        assert "line 2" in fail.message and "x = 1" in fail.message

    def test_not_found_nothing_resembles(self):
        r = apply_edits([_replace("a.py", "zzzzzzzzzzzz()", "")], {"a.py": SRC})
        assert "Nothing in the file resembles" in r.failures[0].message

    def test_gutter_in_search_is_called_out(self):
        r = apply_edits(
            [_replace("a.py", "   2|     x = 1\n   3|     return x\n", "")], {"a.py": SRC})
        assert r.failures[0].code == "not_found"
        assert "gutter" in r.failures[0].message

    def test_ambiguous_lists_line_numbers(self):
        r = apply_edits([_replace("a.py", "x", "y")], {"a.py": SRC})
        (fail,) = r.failures
        assert fail.code == "ambiguous"
        assert "2 places" in fail.message and "2, 3" in fail.message

    def test_ambiguous_with_many_matches_is_truncated(self):
        text = "\n".join(["v = 1"] * 30) + "\n"
        r = apply_edits([_replace("a.py", "v = 1", "v = 2")], {"a.py": text})
        assert r.failures[0].code == "ambiguous"
        assert "and 20 more" in r.failures[0].message

    def test_find_matches_is_capped(self):
        assert len(find_matches("a" * 5000, "a")) == 200

    @pytest.mark.parametrize("edit,fragment", [
        ({"op": "replace", "search": "a", "replace": "b"}, "`path`"),
        ({"path": 5, "op": "replace"}, "`path`"),
        ({"path": "a.py", "op": "rename"}, "`op` must be one of"),
        ({"path": "a.py", "op": "replace", "search": "", "replace": "x"}, "non-empty"),
        ({"path": "a.py", "op": "replace", "search": "   \n", "replace": "x"}, "non-empty"),
        ({"path": "a.py", "op": "replace", "search": "a", "replace": None}, "`replace` must be a string"),
        ({"path": "a.py", "op": "replace", "search": "a"}, "`replace` must be a string"),
        ({"path": "n.py", "op": "create"}, "string `content`"),
        ({"path": "n.py", "op": "create", "content": None}, "string `content`"),
    ])
    def test_invalid_edits(self, edit, fragment):
        r = apply_edits([edit], {"a.py": "a\n"})
        assert not r.ok and fragment in r.failures[0].message
        assert r.failures[0].code == "invalid_edit"

    def test_non_dict_edit(self):
        r = apply_edits(["nope"], {"a.py": "a\n"})
        assert r.failures[0].code == "invalid_edit" and r.failures[0].path is None

    def test_missing_op_is_inferred(self):
        r = apply_edits([{"path": "a.py", "search": "a", "replace": "b"},
                         {"path": "n.py", "content": "hi\n"}], {"a.py": "a\n"})
        assert r.ok
        assert {f.path: f.op for f in r.files} == {"a.py": "replace", "n.py": "create"}

    def test_missing_op_with_nothing_to_infer_from(self):
        r = apply_edits([{"path": "a.py"}], {"a.py": "a\n"})
        assert r.failures[0].code == "invalid_edit"

    def test_dot_slash_prefix_is_stripped(self):
        r = apply_edits([_replace("./a.py", "a", "b")], {"a.py": "a\n"})
        assert _one(r).path == "a.py"

    def test_replace_on_unknown_file(self):
        r = apply_edits([_replace("other.py", "a", "b")], {"a.py": "a\n"})
        (fail,) = r.failures
        assert fail.code == "unknown_file"
        assert "not a file that was shown to you" in fail.message

    def test_delete_on_unknown_file(self):
        r = apply_edits([{"path": "other.py", "op": "delete"}], {"a.py": "a\n"})
        assert r.failures[0].code == "unknown_file"

    def test_all_failures_are_collected(self):
        r = apply_edits(
            [_replace("a.py", "nope", "x"), _replace("b.py", "zip", "x"),
             {"path": "c.py", "op": "delete"}],
            {"a.py": "a\n", "b.py": "b\n"})
        assert [f.index for f in r.failures] == [0, 1, 2]
        assert format_failures(r.failures).count("\n- ") == 2


# ---------------------------------------------------------------------------
# 4. create / delete
# ---------------------------------------------------------------------------
class TestCreateDelete:
    def test_create(self):
        r = apply_edits([{"path": "new.py", "op": "create", "content": "print(1)\n"}], {})
        f = _one(r)
        assert (f.op, f.original, f.proposed, f.added, f.removed) == ("create", "", "print(1)\n", 1, 0)

    def test_create_empty_file(self):
        f = _one(apply_edits([{"path": "new.py", "op": "create", "content": ""}], {}))
        assert f.op == "create" and f.proposed == ""

    def test_create_over_a_shown_file_fails(self):
        r = apply_edits([{"path": "a.py", "op": "create", "content": "x"}], {"a.py": "a\n"})
        assert r.failures[0].code == "already_exists"

    def test_create_over_an_existing_unshown_file_fails(self):
        r = apply_edits([{"path": "big.py", "op": "create", "content": "x"}], {},
                        existing_paths=["big.py"])
        (fail,) = r.failures
        assert fail.code == "already_exists" and "only files shown to you" in fail.message

    def test_create_twice_fails(self):
        r = apply_edits([{"path": "n.py", "op": "create", "content": "1"},
                         {"path": "n.py", "op": "create", "content": "2"}], {})
        assert r.failures[0].code == "already_exists"
        assert r.failures[0].index == 1

    def test_replace_after_create_in_same_batch(self):
        r = apply_edits([{"path": "n.py", "op": "create", "content": "a = 1\n"},
                         _replace("n.py", "a = 1", "a = 2")], {})
        f = _one(r)
        assert (f.op, f.proposed) == ("create", "a = 2\n")

    def test_delete(self):
        f = _one(apply_edits([{"path": "a.py", "op": "delete"}], {"a.py": "a\nb\n"}))
        assert (f.op, f.original, f.proposed, f.removed, f.added) == ("delete", "a\nb\n", "", 2, 0)

    def test_edit_after_delete_fails(self):
        r = apply_edits([{"path": "a.py", "op": "delete"}, _replace("a.py", "a", "b")],
                        {"a.py": "a\n"})
        assert r.failures[0].code == "unknown_file"
        assert "already deleted" in r.failures[0].message

    def test_delete_then_create_nets_to_replace(self):
        r = apply_edits([{"path": "a.py", "op": "delete"},
                         {"path": "a.py", "op": "create", "content": "new\n"}], {"a.py": "old\n"})
        f = _one(r)
        assert (f.op, f.original, f.proposed) == ("replace", "old\n", "new\n")

    def test_create_then_delete_proposes_nothing(self):
        r = apply_edits([{"path": "n.py", "op": "create", "content": "x"},
                         {"path": "n.py", "op": "delete"}], {})
        assert r.ok and not r.files and not r.unchanged

    def test_files_are_reported_in_first_touch_order(self):
        r = apply_edits([_replace("b.py", "b", "B"), _replace("a.py", "a", "A")],
                        {"a.py": "a\n", "b.py": "b\n"})
        assert [f.path for f in r.files] == ["b.py", "a.py"]

    def test_to_edit_files_shape(self):
        r = apply_edits([_replace("a.py", "a", "b"), {"path": "n.py", "op": "create", "content": "n"},
                         {"path": "d.py", "op": "delete"}],
                        {"a.py": "a\n", "d.py": "d\n"})
        assert r.to_edit_files() == [
            {"path": "a.py", "op": "replace", "content": "b\n"},
            {"path": "n.py", "op": "create", "content": "n"},
            {"path": "d.py", "op": "delete", "content": ""},
        ]


# ---------------------------------------------------------------------------
# 5. line endings
# ---------------------------------------------------------------------------
class TestLineEndings:
    CRLF = "def f():\r\n    x = 1\r\n    return x\r\n"

    def test_crlf_file_lf_search_exact(self):
        f = _one(apply_edits(
            [_replace("a.py", "    x = 1\n", "    x = 2\n    y = 3\n")], {"a.py": self.CRLF}))
        assert f.proposed == "def f():\r\n    x = 2\r\n    y = 3\r\n    return x\r\n"

    def test_crlf_file_multiline_search(self):
        f = _one(apply_edits(
            [_replace("a.py", "    x = 1\n    return x\n", "    return 0\n")], {"a.py": self.CRLF}))
        assert f.proposed == "def f():\r\n    return 0\r\n"

    def test_crlf_only_edited_lines_change(self):
        f = _one(apply_edits([_replace("a.py", "x = 1", "x = 2")], {"a.py": self.CRLF}))
        assert f.proposed == self.CRLF.replace("x = 1", "x = 2")

    def test_crlf_whitespace_match(self):
        f = _one(apply_edits(
            [_replace("a.py", "x = 1\nreturn x", "x = 2\nreturn x")], {"a.py": self.CRLF}))
        assert f.proposed == "def f():\r\n    x = 2\r\n    return x\r\n"
        assert f.match_kinds == ["whitespace"]

    def test_crlf_model_sent_crlf_search(self):
        f = _one(apply_edits(
            [_replace("a.py", "    x = 1\r\n", "    x = 2\r\n")], {"a.py": self.CRLF}))
        assert f.proposed == "def f():\r\n    x = 2\r\n    return x\r\n"

    def test_lone_cr_file(self):
        text = "a = 1\rb = 2\rc = 3\r"
        f = _one(apply_edits([_replace("a.py", "b = 2\n", "b = 9\nb2 = 9\n")], {"a.py": text}))
        assert f.proposed == "a = 1\rb = 9\rb2 = 9\rc = 3\r"

    def test_mixed_endings_are_not_normalised(self):
        text = "a\r\nb\nc\r\nd\r\n"  # CRLF dominates
        f = _one(apply_edits([_replace("a.py", "c", "C")], {"a.py": text}))
        assert f.proposed == "a\r\nb\nC\r\nd\r\n"

    def test_mixed_new_lines_take_the_dominant_ending(self):
        text = "a\nb\nc\r\n"  # LF dominates
        f = _one(apply_edits([_replace("a.py", "b\n", "b\nb2\n")], {"a.py": text}))
        assert f.proposed == "a\nb\nb2\nc\r\n"

    def test_crlf_line_numbers_in_matches(self):
        (m,) = find_matches(self.CRLF, "return x")
        assert (m.start_line, m.end_line) == (3, 3)
        assert self.CRLF[m.start:m.end] == "return x"

    def test_crlf_delete_whole_lines(self):
        f = _one(apply_edits([_replace("a.py", "    x = 1\n", "")], {"a.py": self.CRLF}))
        assert f.proposed == "def f():\r\n    return x\r\n"

    def test_crlf_diff_stats_count_lines_not_endings(self):
        f = _one(apply_edits([_replace("a.py", "x = 1", "x = 2")], {"a.py": self.CRLF}))
        assert (f.added, f.removed) == (1, 1)


# ---------------------------------------------------------------------------
# 6. several edits to one file
# ---------------------------------------------------------------------------
class TestMultiEdit:
    def test_later_edit_sees_earlier_result(self):
        r = apply_edits([_replace("a.py", "x = 1", "x = 2"),
                         _replace("a.py", "x = 2", "x = 3")], {"a.py": SRC})
        assert _one(r).proposed == "def f():\n    x = 3\n    return x\n"

    def test_failed_edit_skips_the_rest_for_that_path_only(self):
        r = apply_edits(
            [_replace("a.py", "nope", "x"), _replace("a.py", "x = 1", "x = 2"),
             _replace("b.py", "b", "B")],
            {"a.py": SRC, "b.py": "b\n"})
        assert [f.code for f in r.failures] == ["not_found", "skipped"]
        assert [f.path for f in r.files] == ["b.py"]  # a.py is left out entirely

    def test_edits_netting_to_no_change_are_reported_unchanged(self):
        r = apply_edits([_replace("a.py", "x = 1", "x = 2"),
                         _replace("a.py", "x = 2", "x = 1")], {"a.py": SRC})
        assert r.ok and not r.files and r.unchanged == ["a.py"]

    def test_replace_x_with_x_is_unchanged(self):
        r = apply_edits([_replace("a.py", "x = 1", "x = 1")], {"a.py": SRC})
        assert r.unchanged == ["a.py"]

    def test_ambiguous_message_mentions_earlier_edits(self):
        r = apply_edits([_replace("a.py", "x = 1", "v = 1"), _replace("a.py", "v", "w")],
                        {"a.py": "x = 1\nv = 0\n"})
        assert r.failures[0].code == "ambiguous"
        assert "after your earlier edits" in r.failures[0].message

    def test_input_files_are_not_mutated(self):
        files = {"a.py": SRC}
        apply_edits([_replace("a.py", "x = 1", "x = 2")], files)
        assert files == {"a.py": SRC}

    def test_empty_edits_list(self):
        r = apply_edits([], {"a.py": SRC})
        assert r.ok and not r.files and not r.unchanged


# ---------------------------------------------------------------------------
# 7. scope
# ---------------------------------------------------------------------------
DUP = "a = 1\nb = 2\na = 1\nc = 3\n"


class TestScope:
    def test_range_disambiguates_an_otherwise_ambiguous_search(self):
        r = apply_edits([_replace("a.py", "a = 1", "a = 9")], {"a.py": DUP},
                        scope=EditScope({"a.py": [(3, 3)]}))
        assert _one(r).proposed == "a = 1\nb = 2\na = 9\nc = 3\n"

    def test_range_on_first_occurrence(self):
        r = apply_edits([_replace("a.py", "a = 1", "a = 9")], {"a.py": DUP},
                        scope=EditScope({"a.py": [(1, 1)]}))
        assert _one(r).proposed == "a = 9\nb = 2\na = 1\nc = 3\n"

    def test_without_scope_it_stays_ambiguous(self):
        r = apply_edits([_replace("a.py", "a = 1", "a = 9")], {"a.py": DUP})
        assert r.failures[0].code == "ambiguous"

    def test_whole_file_scope_does_not_disambiguate(self):
        r = apply_edits([_replace("a.py", "a = 1", "a = 9")], {"a.py": DUP},
                        scope=EditScope({"a.py": None}))
        assert r.failures[0].code == "ambiguous"

    def test_range_covering_both_is_still_ambiguous_and_says_so(self):
        r = apply_edits([_replace("a.py", "a = 1", "a = 9")], {"a.py": DUP},
                        scope=EditScope({"a.py": [(1, 4)]}))
        assert r.failures[0].code == "ambiguous"
        assert "It matches 2 places" in r.failures[0].message

    def test_range_that_only_partly_narrows_says_so(self):
        text = "a\na\na\nb\n"
        r = apply_edits([_replace("a.py", "a", "z")], {"a.py": text},
                        scope=EditScope({"a.py": [(1, 2)]}))
        assert r.failures[0].code == "ambiguous"
        assert "Even inside the lines you were shown as selected" in r.failures[0].message

    def test_touching_a_range_counts_when_nothing_is_fully_inside(self):
        text = "x = 1\ny = 2\nx = 1\ny = 2\n"
        # multi-line search overlaps range (2,3) on both candidates; neither is
        # fully inside it, both touch it -> still ambiguous.
        r = apply_edits([_replace("a.py", "x = 1\ny = 2\n", "")], {"a.py": text},
                        scope=EditScope({"a.py": [(2, 3)]}))
        assert r.failures[0].code == "ambiguous"

    def test_in_scope_edit_has_no_violation(self):
        r = apply_edits([_replace("a.py", "b = 2", "b = 3")], {"a.py": DUP},
                        scope=EditScope({"a.py": [(2, 2)]}))
        f = _one(r)
        assert not f.scope_violation and f.scope_reasons == []

    def test_edit_outside_range_is_applied_but_flagged(self):
        r = apply_edits([_replace("a.py", "c = 3", "c = 4")], {"a.py": DUP},
                        scope=EditScope({"a.py": [(2, 2)]}))
        f = _one(r)
        assert f.proposed.endswith("c = 4\n")
        assert f.scope_violation
        assert "line 4 is outside the range(s) you referenced (2-2)" in f.scope_reasons[0]

    def test_multiline_edit_partly_inside_is_not_flagged(self):
        r = apply_edits([_replace("a.py", "b = 2\na = 1\n", "b = 2\n")], {"a.py": DUP},
                        scope=EditScope({"a.py": [(2, 2)]}))
        assert not _one(r).scope_violation

    def test_whole_file_scope_never_flags(self):
        r = apply_edits([_replace("a.py", "c = 3", "c = 4")], {"a.py": DUP},
                        scope=EditScope({"a.py": None}))
        assert not _one(r).scope_violation

    def test_file_not_referenced_is_flagged(self):
        r = apply_edits([_replace("other.py", "o", "p")], {"other.py": "o\n", "a.py": DUP},
                        scope=EditScope({"a.py": None}))
        f = _one(r)
        assert f.scope_violation and "was not one of the files you referenced" in f.scope_reasons[0]

    def test_no_scope_no_flags(self):
        f = _one(apply_edits([_replace("a.py", "c = 3", "c = 4")], {"a.py": DUP}))
        assert not f.scope_violation

    def test_new_file_not_referenced_is_flagged(self):
        r = apply_edits([{"path": "n.py", "op": "create", "content": "x"}], {},
                        scope=EditScope({"a.py": None}))
        assert _one(r).scope_violation

    def test_new_file_that_was_referenced_is_fine(self):
        r = apply_edits([{"path": "n.py", "op": "create", "content": "x"}], {},
                        scope=EditScope({"n.py": None}))
        assert not _one(r).scope_violation

    def test_delete_of_partly_referenced_file_is_flagged(self):
        r = apply_edits([{"path": "a.py", "op": "delete"}], {"a.py": DUP},
                        scope=EditScope({"a.py": [(1, 1)]}))
        assert "only referenced part of it" in _one(r).scope_reasons[0]

    def test_delete_of_wholly_referenced_file_is_fine(self):
        r = apply_edits([{"path": "a.py", "op": "delete"}], {"a.py": DUP},
                        scope=EditScope({"a.py": None}))
        assert not _one(r).scope_violation

    def test_ranges_shift_when_an_earlier_edit_adds_lines(self):
        text = "h\na = 1\nb\na = 1\n"
        # Selection is line 4 (the second `a = 1`). Edit 1 inserts two lines at
        # the top, pushing the selection to line 6; edit 2 must still find it.
        r = apply_edits(
            [_replace("a.py", "h\n", "h\nh2\nh3\n"), _replace("a.py", "a = 1", "a = 9")],
            {"a.py": text}, scope=EditScope({"a.py": [(4, 4)]}))
        f = _one(r)
        assert f.proposed == "h\nh2\nh3\na = 1\nb\na = 9\n"
        # Edit 1 (line 1) is itself outside the selection, so it IS flagged;
        # edit 2 must NOT be: the selection moved from line 4 to line 6 and
        # the tracker followed it. An un-shifted range would flag it too.
        assert len(f.scope_reasons) == 1 and "line 1 is outside" in f.scope_reasons[0]

    def test_ranges_shift_when_an_earlier_edit_removes_lines(self):
        text = "h\nh2\nh3\na = 1\nb\na = 1\n"
        r = apply_edits(
            [_replace("a.py", "h2\nh3\n", ""), _replace("a.py", "a = 1", "a = 9")],
            {"a.py": text}, scope=EditScope({"a.py": [(6, 6)]}))
        assert _one(r).proposed == "h\na = 1\nb\na = 9\n"

    def test_range_before_the_edit_is_untouched(self):
        text = "a = 1\nb\nc\nd\n"
        r = apply_edits(
            [_replace("a.py", "d\n", "d\ne\nf\n"), _replace("a.py", "a = 1", "a = 2")],
            {"a.py": text}, scope=EditScope({"a.py": [(1, 1)]}))
        f = _one(r)
        # Edit 1 (line 4) is outside the selection and is flagged; edit 2 on
        # line 1 sits before it, so its range must not have moved.
        assert len(f.scope_reasons) == 1 and "line 4 is outside" in f.scope_reasons[0]

    def test_scope_object_is_not_mutated(self):
        scope = EditScope({"a.py": [(4, 4)]})
        apply_edits([_replace("a.py", "a = 1\n", "a = 1\nx\ny\n")], {"a.py": DUP}, scope=scope)
        assert scope.files == {"a.py": [(4, 4)]}

    def test_duplicate_reasons_are_collapsed(self):
        r = apply_edits([_replace("a.py", "c = 3", "c = 4"), _replace("a.py", "c = 4", "c = 3x")],
                        {"a.py": DUP}, scope=EditScope({"a.py": [(2, 2)]}))
        assert len(_one(r).scope_reasons) == 1


# ---------------------------------------------------------------------------
# 8. validation, limits, stats
# ---------------------------------------------------------------------------
class TestValidationAndLimits:
    def test_validate_path_value_error_becomes_invalid_path(self):
        def validate(p):
            if p.startswith("bad"):
                raise ValueError("nope")
        r = apply_edits([_replace("bad.py", "a", "b"), _replace("a.py", "a", "b")],
                        {"bad.py": "a\n", "a.py": "a\n"}, validate_path=validate)
        (fail,) = r.failures
        assert fail.code == "invalid_path" and "nope" in fail.message
        assert [f.path for f in r.files] == ["a.py"]

    def test_validate_path_runs_for_create_and_delete_too(self):
        def validate(p):
            raise ValueError("no")
        r = apply_edits([{"path": "n.py", "op": "create", "content": "x"},
                         {"path": "a.py", "op": "delete"}], {"a.py": "a"}, validate_path=validate)
        assert [f.code for f in r.failures] == ["invalid_path", "invalid_path"]

    def test_max_file_chars(self):
        r = apply_edits([_replace("a.py", "a", "a" * 50)], {"a.py": "a\n"}, max_file_chars=10)
        (fail,) = r.failures
        assert fail.code == "too_large" and "over the 10 limit" in fail.message
        assert not r.files

    def test_max_file_chars_applies_to_created_files(self):
        r = apply_edits([{"path": "n.py", "op": "create", "content": "x" * 20}], {}, max_file_chars=10)
        assert r.failures[0].code == "too_large"

    def test_max_file_chars_does_not_apply_to_deletes(self):
        r = apply_edits([{"path": "a.py", "op": "delete"}], {"a.py": "x" * 50}, max_file_chars=10)
        assert r.ok and r.files[0].op == "delete"


class TestDiffStats:
    def test_identical(self):
        assert diff_stats("a\nb\n", "a\nb\n") == (0, 0)

    def test_one_line_changed(self):
        assert diff_stats("a\nb\nc\n", "a\nB\nc\n") == (1, 1)

    def test_pure_insert(self):
        assert diff_stats("a\nc\n", "a\nb\nc\n") == (1, 0)

    def test_pure_delete(self):
        assert diff_stats("a\nb\nc\n", "a\nc\n") == (0, 1)

    def test_empty_original(self):
        assert diff_stats("", "a\nb\n") == (2, 0)

    def test_empty_proposed(self):
        assert diff_stats("a\nb\n", "") == (0, 2)

    def test_no_trailing_newline_is_not_a_line(self):
        assert diff_stats("a\nb", "a\nb\n") == (0, 0)

    def test_stats_come_back_on_the_file_result(self):
        f = _one(apply_edits([_replace("a.py", "    x = 1\n", "    x = 2\n    y = 3\n")], {"a.py": SRC}))
        assert (f.added, f.removed) == (2, 1)
