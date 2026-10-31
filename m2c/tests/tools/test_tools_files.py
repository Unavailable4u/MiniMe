"""
m2c/tests/tools/test_tools_files.py -- Patch C05: read_file, list_dir,
edit_file, write_file and delete_path (m2c.tools.files), driven through
``run_tool`` so the argument validation and the result shape are exercised too.

Groups, in file order:

* read_file       line ranges, truncation marker, binary / non-UTF-8 refusal,
                  FIFOs, huge files, the "seen" record.
* list_dir        caps, symlink marks, depth, hidden protected entries.
* edit_file       unique / missing / non-unique matches, diff, read-before-edit.
* write_file      read-before-write, changed-since-read, atomic replace, mode,
                  parent directories, final-symlink refusal.
* interrupted     fault injection at os.replace / os.fsync / os.write, a real
  writes          child killed with SIGKILL between the temp write and the
                  rename, stale temp cleanup, and the concurrent-change check.
* races           a component swapped for a symlink between check and open.
* delete_path     files, empty / non-empty / recursive directories, the root,
                  .git, symlinks.

Run: cd m2c && pytest tests/tools/test_tools_files.py -v
"""
from __future__ import annotations

import hashlib
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from tools_fixtures import SECRET, call, fails, mode_of, ok, write

import m2c
from m2c.tools import Caps, ToolContext
from m2c.tools import files as tool_files

SRC_ROOT = Path(m2c.__file__).resolve().parent.parent  # .../m2c/src


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def temp_files(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.startswith(".m2c-write."))


# ==========================================================================
# read_file
# ==========================================================================
def test_read_returns_numbered_lines(ctx, proj):
    write(proj, "f.txt", "alpha\nbeta\ngamma\n")
    out = ok(call(ctx, "read_file", path="f.txt"))
    lines = out.splitlines()
    assert [ln.split("\t", 1)[1] for ln in lines] == ["alpha", "beta", "gamma"]
    assert [ln.split("\t", 1)[0].strip() for ln in lines] == ["1", "2", "3"]


def test_read_without_a_trailing_newline(ctx, proj):
    write(proj, "f.txt", "one\ntwo")
    out = ok(call(ctx, "read_file", path="f.txt"))
    assert [ln.split("\t", 1)[1] for ln in out.splitlines()] == ["one", "two"]


@pytest.mark.parametrize(
    "start,end,expected",
    [(2, 3, ["b", "c"]), (4, None, ["d", "e"]), (None, 2, ["a", "b"]), (5, 5, ["e"]), (1, 99, list("abcde"))],
)
def test_read_line_ranges(ctx, proj, start, end, expected):
    write(proj, "f.txt", "a\nb\nc\nd\ne\n")
    args = {k: v for k, v in (("start_line", start), ("end_line", end)) if v is not None}
    out = ok(call(ctx, "read_file", path="f.txt", **args))
    assert [ln.split("\t", 1)[1] for ln in out.splitlines()] == expected
    # numbering follows the file, not the slice
    first = out.splitlines()[0].split("\t", 1)[0].strip()
    assert first == str(start or 1)


def test_read_range_past_the_end_says_so(ctx, proj):
    write(proj, "f.txt", "a\nb\n")
    result = call(ctx, "read_file", path="f.txt", start_line=50)
    assert result.ok and "past the end" in result.output and "2 lines" in result.output


def test_read_end_before_start_is_invalid(ctx, proj):
    write(proj, "f.txt", "a\nb\n")
    fails(call(ctx, "read_file", path="f.txt", start_line=3, end_line=2), "invalid_args")


def test_read_empty_file(ctx, proj):
    write(proj, "empty.txt", "")
    assert "empty" in ok(call(ctx, "read_file", path="empty.txt"))


def test_read_missing_file(ctx):
    fails(call(ctx, "read_file", path="nope.txt"), "not_found")


def test_read_directory_is_refused_with_a_hint(ctx, proj):
    (proj / "d").mkdir()
    result = fails(call(ctx, "read_file", path="d"), "is_a_directory")
    assert "list_dir" in result.output


def test_read_through_a_file_as_if_it_were_a_directory(ctx, proj):
    write(proj, "f.txt")
    fails(call(ctx, "read_file", path="f.txt/inner"), "not_a_directory")


def test_oversize_read_is_truncated_with_a_marker_and_a_way_to_continue(proj):
    context = ToolContext(proj, caps=Caps(read_output_chars=400))
    write(proj, "big.txt", "".join(f"line number {i}\n" for i in range(1, 200)))
    result = call(context, "read_file", path="big.txt")
    assert result.ok and result.truncated is True
    assert len(result.output) < 1000
    m = re.search(r"\[truncated: showing lines 1-(\d+) of 199; call read_file with start_line=(\d+)", result.output)
    assert m and int(m.group(2)) == int(m.group(1)) + 1
    # and the continuation really continues
    nxt = ok(call(context, "read_file", path="big.txt", start_line=int(m.group(2))))
    assert nxt.splitlines()[0].split("\t", 1)[0].strip() == m.group(2)


def test_a_small_read_is_not_marked_truncated(ctx, proj):
    write(proj, "f.txt", "a\n")
    assert call(ctx, "read_file", path="f.txt").truncated is False


def test_one_enormous_line_is_cut_and_flagged(proj):
    context = ToolContext(proj, caps=Caps(max_line_chars=50))
    write(proj, "min.js", "x" * 5000 + "\nshort\n")
    result = call(context, "read_file", path="min.js")
    assert result.ok and result.truncated is True
    assert "line truncated" in result.output and "5000" in result.output
    assert "short" in result.output
    assert len(result.output) < 400


def test_binary_file_with_a_nul_byte_is_refused(ctx, proj):
    (proj / "blob.bin").write_bytes(b"abc\0def")
    result = fails(call(ctx, "read_file", path="blob.bin"), "binary_file")
    assert "abc" not in result.output


def test_png_header_is_binary(ctx, proj):
    (proj / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR")
    fails(call(ctx, "read_file", path="a.png"), "binary_file")


def test_non_utf8_text_is_refused(ctx, proj):
    (proj / "latin1.txt").write_bytes(b"caf\xe9\n")
    fails(call(ctx, "read_file", path="latin1.txt"), "not_utf8")


def test_utf8_with_a_bom_and_multibyte_characters(ctx, proj):
    (proj / "u.txt").write_bytes("﻿héllo ✓\n".encode("utf-8"))
    assert "héllo ✓" in ok(call(ctx, "read_file", path="u.txt"))


def test_a_fifo_is_refused_without_blocking(ctx, proj):
    os.mkfifo(proj / "pipe")
    box = {}

    def go():
        box["r"] = call(ctx, "read_file", path="pipe")

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "read_file blocked on a FIFO"
    fails(box["r"], "not_a_file")


def test_read_follows_a_symlink_that_stays_inside(ctx, proj):
    write(proj, "real.txt", "inside\n")
    (proj / "alias.txt").symlink_to("real.txt")
    assert "inside" in ok(call(ctx, "read_file", path="alias.txt"))


def test_read_refuses_a_symlink_that_leaves_the_project(ctx, proj, outside):
    (proj / "leak.txt").symlink_to(outside / "secret.txt")
    result = fails(call(ctx, "read_file", path="leak.txt"), "outside_root")
    assert SECRET not in result.output


def test_read_records_seen_state(ctx, proj):
    f = write(proj, "f.txt", "alpha\n")
    ok(call(ctx, "read_file", path="f.txt"))
    rec = ctx.seen.get(f)
    st = f.stat()
    assert rec is not None
    assert (rec.size, rec.mtime_ns, rec.sha256) == (st.st_size, st.st_mtime_ns, sha(b"alpha\n"))


def test_a_ranged_read_records_the_whole_file_hash(ctx, proj):
    f = write(proj, "f.txt", "a\nb\nc\n")
    ok(call(ctx, "read_file", path="f.txt", start_line=2, end_line=2))
    assert ctx.seen.get(f).sha256 == sha(b"a\nb\nc\n")


def test_a_failed_read_records_nothing(ctx, proj):
    (proj / "blob.bin").write_bytes(b"\0\0")
    call(ctx, "read_file", path="blob.bin")
    call(ctx, "read_file", path="missing")
    assert len(ctx.seen) == 0


def test_reading_via_a_symlink_records_the_real_file(ctx, proj):
    real = write(proj, "real.txt", "x\n")
    (proj / "alias.txt").symlink_to("real.txt")
    ok(call(ctx, "read_file", path="alias.txt"))
    assert ctx.seen.get(real) is not None


def test_file_larger_than_the_whole_file_limit_is_streamed_not_loaded(proj):
    context = ToolContext(proj, caps=Caps(max_file_bytes=1000, read_output_chars=300))
    f = write(proj, "huge.log", "".join(f"entry {i:04d}\n" for i in range(500)))
    assert f.stat().st_size > 1000
    out = ok(call(context, "read_file", path="huge.log", start_line=3, end_line=4))
    assert [ln.split("\t", 1)[1] for ln in out.splitlines()] == ["entry 0002", "entry 0003"]
    # no whole-file hash exists, so the file cannot be edited or overwritten
    assert context.seen.get(f).sha256 is None
    fails(call(context, "edit_file", path="huge.log", old_string="entry 0002", new_string="x"), "file_too_large")
    fails(call(context, "write_file", path="huge.log", content="x"), "file_too_large")
    assert f.read_text().startswith("entry 0000")


def test_streamed_read_stops_at_the_output_cap_with_a_marker(proj):
    context = ToolContext(proj, caps=Caps(max_file_bytes=1000, read_output_chars=300))
    write(proj, "huge.log", "".join(f"entry {i:04d}\n" for i in range(500)))
    result = call(context, "read_file", path="huge.log")
    assert result.ok and result.truncated and "start_line=" in result.output


def test_streamed_read_still_refuses_binary_data(proj):
    context = ToolContext(proj, caps=Caps(max_file_bytes=100))
    (proj / "big.bin").write_bytes(b"text\n" * 50 + b"\0" + b"more\n" * 50)
    fails(call(context, "read_file", path="big.bin"), "binary_file")


# ==========================================================================
# list_dir
# ==========================================================================
def test_list_dir_sorts_directories_first_and_marks_them(ctx, proj):
    write(proj, "b.txt")
    write(proj, "A.txt")
    (proj / "zdir").mkdir()
    (proj / "adir").mkdir()
    lines = ok(call(ctx, "list_dir", path=".")).splitlines()
    assert lines == ["adir/", "zdir/", "A.txt", "b.txt"]


def test_list_dir_path_defaults_to_the_root(ctx, proj):
    write(proj, "f.txt")
    assert "f.txt" in ok(call(ctx, "list_dir"))


def test_list_dir_marks_symlinks_without_showing_outside_targets(ctx, proj, outside):
    write(proj, "real.txt")
    (proj / "inner").symlink_to("real.txt")
    (proj / "leak").symlink_to(outside / "secret.txt")
    (proj / "broken").symlink_to("nowhere-at-all")
    out = ok(call(ctx, "list_dir"))
    by_name = {ln.split()[0]: ln for ln in out.splitlines()}
    assert "symlink" in by_name["inner"] and "real.txt" in by_name["inner"]
    assert "symlink" in by_name["leak"] and "outside" in by_name["leak"]
    assert str(outside) not in out and "secret.txt" not in out
    assert "broken" in by_name["broken"] and "symlink" in by_name["broken"]


def test_list_dir_depth(ctx, proj):
    write(proj, "a/b/c/deep.txt")
    one = ok(call(ctx, "list_dir", path=".", max_depth=1))
    two = ok(call(ctx, "list_dir", path=".", max_depth=2))
    three = ok(call(ctx, "list_dir", path=".", max_depth=4))
    assert "b/" not in one
    assert "  b/" in two and "c/" not in two
    assert "deep.txt" in three
    assert "    c/" in three  # indentation shows nesting


def test_list_dir_does_not_descend_into_git_or_follow_symlinked_directories(ctx, proj, outside):
    write(proj, ".git/HEAD", "ref\n")
    write(proj, "src/a.py")
    (proj / "linkdir").symlink_to(outside)
    out = ok(call(ctx, "list_dir", path=".", max_depth=4))
    assert ".git/" in out and "HEAD" not in out
    assert "secret.txt" not in out


def test_list_dir_caps_entries_with_a_marker(proj):
    context = ToolContext(proj, caps=Caps(list_max_entries=5))
    for i in range(20):
        write(proj, f"f{i:02d}.txt")
    result = call(context, "list_dir")
    assert result.ok and result.truncated
    assert result.output.count(".txt") == 5
    assert "truncated" in result.output


def test_list_dir_empty_directory(ctx, proj):
    (proj / "e").mkdir()
    assert "empty" in ok(call(ctx, "list_dir", path="e"))


def test_list_dir_errors(ctx, proj):
    write(proj, "f.txt")
    fails(call(ctx, "list_dir", path="missing"), "not_found")
    fails(call(ctx, "list_dir", path="f.txt"), "not_a_directory")
    fails(call(ctx, "list_dir", path="../elsewhere"), "outside_root")


def test_list_dir_refuses_a_protected_directory_and_hides_protected_entries(ctx, proj):
    write(proj, ".ssh/config", SECRET)
    write(proj, ".env", SECRET)
    write(proj, "id_rsa", SECRET)
    write(proj, "keep.txt")
    fails(call(ctx, "list_dir", path=".ssh"), "protected_path")
    out = ok(call(ctx, "list_dir", path=".", max_depth=3))
    assert "keep.txt" in out
    for hidden in (".ssh", ".env", "id_rsa"):
        assert hidden not in out


# ==========================================================================
# edit_file
# ==========================================================================
def _read_then(ctx, name):
    ok(call(ctx, "read_file", path=name))


def test_edit_replaces_one_unique_match_and_returns_a_diff(ctx, proj):
    f = write(proj, "f.txt", "one\ntwo\nthree\n")
    _read_then(ctx, "f.txt")
    out = ok(call(ctx, "edit_file", path="f.txt", old_string="two", new_string="TWO"))
    assert f.read_text() == "one\nTWO\nthree\n"
    assert "--- a/f.txt" in out and "+++ b/f.txt" in out
    assert "-two" in out and "+TWO" in out and " one" in out


def test_edit_string_not_found(ctx, proj):
    f = write(proj, "f.txt", "one\n")
    _read_then(ctx, "f.txt")
    result = fails(call(ctx, "edit_file", path="f.txt", old_string="zzz", new_string="y"), "string_not_found")
    assert "not found" in result.output
    assert f.read_text() == "one\n"


def test_edit_non_unique_says_how_many_matches(ctx, proj):
    f = write(proj, "f.txt", "x\nx\nx\ny\n")
    _read_then(ctx, "f.txt")
    result = fails(call(ctx, "edit_file", path="f.txt", old_string="x", new_string="z"), "string_not_unique")
    assert re.search(r"\b3\b", result.output) and "replace_all" in result.output
    assert f.read_text() == "x\nx\nx\ny\n"


def test_edit_replace_all(ctx, proj):
    f = write(proj, "f.txt", "x\nx\nx\ny\n")
    _read_then(ctx, "f.txt")
    out = ok(call(ctx, "edit_file", path="f.txt", old_string="x", new_string="z", replace_all=True))
    assert f.read_text() == "z\nz\nz\ny\n"
    assert "3" in out.splitlines()[0]


def test_edit_replace_all_with_a_single_match_is_fine(ctx, proj):
    f = write(proj, "f.txt", "only\n")
    _read_then(ctx, "f.txt")
    ok(call(ctx, "edit_file", path="f.txt", old_string="only", new_string="one", replace_all=True))
    assert f.read_text() == "one\n"


def test_edit_matches_exactly_not_fuzzily(ctx, proj):
    write(proj, "f.txt", "a  b\n")
    _read_then(ctx, "f.txt")
    fails(call(ctx, "edit_file", path="f.txt", old_string="a b", new_string="x"), "string_not_found")
    fails(call(ctx, "edit_file", path="f.txt", old_string="A  B", new_string="x"), "string_not_found")


def test_edit_empty_old_string_is_invalid(ctx, proj):
    write(proj, "f.txt", "a\n")
    _read_then(ctx, "f.txt")
    fails(call(ctx, "edit_file", path="f.txt", old_string="", new_string="x"), "invalid_args")


def test_edit_identical_strings_is_a_no_change_error(ctx, proj):
    write(proj, "f.txt", "a\n")
    _read_then(ctx, "f.txt")
    fails(call(ctx, "edit_file", path="f.txt", old_string="a", new_string="a"), "no_change")


def test_edit_can_delete_text_and_multiline_strings_work(ctx, proj):
    f = write(proj, "f.txt", "keep\nremove me\nplease\nkeep too\n")
    _read_then(ctx, "f.txt")
    ok(call(ctx, "edit_file", path="f.txt", old_string="remove me\nplease\n", new_string=""))
    assert f.read_text() == "keep\nkeep too\n"


def test_edit_requires_a_prior_read(ctx, proj):
    f = write(proj, "f.txt", "a\n")
    result = fails(call(ctx, "edit_file", path="f.txt", old_string="a", new_string="b"), "not_read")
    assert "read_file" in result.output
    assert f.read_text() == "a\n"


def test_edit_refuses_when_the_file_changed_since_the_read(ctx, proj):
    f = write(proj, "f.txt", "hello\n")
    _read_then(ctx, "f.txt")
    f.write_text("hellp\n")  # same size, different content
    result = fails(call(ctx, "edit_file", path="f.txt", old_string="hellp", new_string="x"), "changed_since_read")
    assert "read" in result.output
    assert f.read_text() == "hellp\n"


def test_after_an_edit_the_file_counts_as_read(ctx, proj):
    f = write(proj, "f.txt", "a b c\n")
    _read_then(ctx, "f.txt")
    ok(call(ctx, "edit_file", path="f.txt", old_string="a", new_string="A"))
    ok(call(ctx, "edit_file", path="f.txt", old_string="c", new_string="C"))
    assert f.read_text() == "A b C\n"


def test_edit_preserves_mode_and_line_endings_and_unicode(ctx, proj):
    f = write(proj, "run.sh", "echo héllo\r\nexit 0\r\n", mode=0o750)
    _read_then(ctx, "run.sh")
    ok(call(ctx, "edit_file", path="run.sh", old_string="exit 0", new_string="exit 1"))
    assert f.read_bytes() == "echo héllo\r\nexit 1\r\n".encode("utf-8")
    assert mode_of(f) == 0o750


def test_edit_missing_file(ctx):
    fails(call(ctx, "edit_file", path="nope.txt", old_string="a", new_string="b"), "not_found")


def test_edit_directory(ctx, proj):
    (proj / "d").mkdir()
    fails(call(ctx, "edit_file", path="d", old_string="a", new_string="b"), "is_a_directory")


def test_edit_refuses_a_symlink_even_one_that_stays_inside(ctx, proj):
    real = write(proj, "real.txt", "a\n")
    (proj / "alias.txt").symlink_to("real.txt")
    ok(call(ctx, "read_file", path="alias.txt"))
    fails(call(ctx, "edit_file", path="alias.txt", old_string="a", new_string="b"), "symlink_refused")
    assert real.read_text() == "a\n"
    assert (proj / "alias.txt").is_symlink()


def test_diff_is_capped(proj):
    context = ToolContext(proj, caps=Caps(diff_max_chars=300))
    f = write(proj, "f.txt", "".join(f"line {i}\n" for i in range(200)))
    ok(call(context, "read_file", path="f.txt"))
    result = call(context, "edit_file", path="f.txt", old_string="line", new_string="LINE", replace_all=True)
    assert result.ok and len(result.output) < 700 and "diff truncated" in result.output
    assert f.read_text().startswith("LINE 0\n")  # the edit itself was complete


# ==========================================================================
# write_file
# ==========================================================================
def test_write_creates_a_new_file_without_a_prior_read(ctx, proj):
    out = ok(call(ctx, "write_file", path="new.txt", content="hello\n"))
    assert (proj / "new.txt").read_text() == "hello\n"
    assert "Created" in out and "6 bytes" in out


def test_write_creates_missing_parent_directories_inside_the_project(ctx, proj):
    ok(call(ctx, "write_file", path="a/b/c/new.txt", content="x"))
    assert (proj / "a" / "b" / "c" / "new.txt").read_text() == "x"


def test_write_overwrite_requires_a_prior_read(ctx, proj):
    f = write(proj, "f.txt", "old\n")
    result = fails(call(ctx, "write_file", path="f.txt", content="new\n"), "not_read")
    assert "read_file" in result.output
    assert f.read_text() == "old\n"


def test_write_overwrite_after_a_read(ctx, proj):
    f = write(proj, "f.txt", "old\n")
    _read_then(ctx, "f.txt")
    assert "Overwrote" in ok(call(ctx, "write_file", path="f.txt", content="new\n"))
    assert f.read_text() == "new\n"


def test_a_ranged_read_is_enough_to_overwrite(ctx, proj):
    f = write(proj, "f.txt", "a\nb\nc\n")
    ok(call(ctx, "read_file", path="f.txt", start_line=1, end_line=1))
    ok(call(ctx, "write_file", path="f.txt", content="replaced\n"))
    assert f.read_text() == "replaced\n"


def test_write_refuses_when_the_file_changed_after_the_read(ctx, proj):
    f = write(proj, "f.txt", "hello\n")
    _read_then(ctx, "f.txt")
    f.write_text("hellp\n")
    result = fails(call(ctx, "write_file", path="f.txt", content="mine\n"), "changed_since_read")
    assert "changed" in result.output
    assert f.read_text() == "hellp\n"  # the other writer's content survives


def test_only_touching_the_file_is_not_a_change(ctx, proj):
    f = write(proj, "f.txt", "same\n")
    _read_then(ctx, "f.txt")
    later = time.time() + 100
    os.utime(f, (later, later))
    ok(call(ctx, "write_file", path="f.txt", content="mine\n"))
    assert f.read_text() == "mine\n"


def test_after_a_write_the_file_counts_as_read(ctx, proj):
    f = write(proj, "f.txt", "a\n")
    _read_then(ctx, "f.txt")
    ok(call(ctx, "write_file", path="f.txt", content="b\n"))
    ok(call(ctx, "write_file", path="f.txt", content="c\n"))
    assert f.read_text() == "c\n"


def test_a_file_created_by_write_can_be_overwritten_without_a_read(ctx, proj):
    ok(call(ctx, "write_file", path="n.txt", content="1\n"))
    ok(call(ctx, "write_file", path="n.txt", content="2\n"))
    assert (proj / "n.txt").read_text() == "2\n"


def test_write_preserves_the_mode_of_an_existing_file(ctx, proj):
    script = write(proj, "run.sh", "#!/bin/sh\n", mode=0o755)
    private = write(proj, "notes.txt", "x\n", mode=0o600)
    group = write(proj, "g.txt", "x\n", mode=0o640)
    for name in ("run.sh", "notes.txt", "g.txt"):
        _read_then(ctx, name)
        ok(call(ctx, "write_file", path=name, content="changed\n"))
    assert (mode_of(script), mode_of(private), mode_of(group)) == (0o755, 0o600, 0o640)


@pytest.mark.parametrize("umask,expected", [(0o022, 0o644), (0o077, 0o600), (0o002, 0o664)])
def test_a_new_file_gets_the_modes_the_users_umask_allows(ctx, proj, umask, expected):
    old = os.umask(umask)
    try:
        ok(call(ctx, "write_file", path="n.txt", content="x"))
    finally:
        os.umask(old)
    assert mode_of(proj / "n.txt") == expected


def test_write_rejects_bad_content(ctx, proj):
    fails(call(ctx, "write_file", path="a.txt", content="has\0nul"), "invalid_args")
    fails(call(ctx, "write_file", path="b.txt", content="lone \ud800 surrogate"), "invalid_args")
    assert not (proj / "a.txt").exists() and not (proj / "b.txt").exists()


def test_write_size_cap(proj):
    context = ToolContext(proj, caps=Caps(max_write_bytes=100))
    fails(call(context, "write_file", path="big.txt", content="x" * 101), "content_too_large")
    ok(call(context, "write_file", path="ok.txt", content="x" * 100))
    assert not (proj / "big.txt").exists()


def test_write_to_a_directory_is_refused(ctx, proj):
    (proj / "d").mkdir()
    fails(call(ctx, "write_file", path="d", content="x"), "is_a_directory")


def test_write_where_a_parent_is_a_file(ctx, proj):
    write(proj, "f.txt")
    fails(call(ctx, "write_file", path="f.txt/inner.txt", content="x"), "not_a_directory")


@pytest.mark.parametrize("target", ["inside", "outside", "dangling"])
def test_write_never_follows_a_final_symlink(ctx, proj, outside, target):
    real = write(proj, "real.txt", "keep\n")
    dest = {"inside": real, "outside": outside / "secret.txt", "dangling": outside / "new.txt"}[target]
    (proj / "link.txt").symlink_to(dest)
    ok(call(ctx, "read_file", path="real.txt"))
    fails(call(ctx, "write_file", path="link.txt", content="PWNED\n"), "symlink_refused")
    assert (proj / "link.txt").is_symlink()
    assert real.read_text() == "keep\n"
    assert (outside / "secret.txt").read_text() == f"{SECRET} outside\n"
    assert not (outside / "new.txt").exists()


def test_write_through_a_symlinked_parent_that_leaves_the_project(ctx, proj, outside):
    (proj / "esc").symlink_to(outside)
    fails(call(ctx, "write_file", path="esc/new.txt", content="x"), "outside_root")
    assert not (outside / "new.txt").exists()


def test_write_path_traversal(ctx, proj, tmp_path):
    fails(call(ctx, "write_file", path="../escape.txt", content="x"), "outside_root")
    fails(call(ctx, "write_file", path=str(tmp_path / "abs.txt"), content="x"), "outside_root")
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "abs.txt").exists()


def test_write_is_atomic_in_shape_no_temp_left_and_same_directory(ctx, proj, monkeypatch):
    seen_names = []
    real_replace = os.replace

    def spy(src, dst, *a, **k):
        seen_names.append((src, dst))
        return real_replace(src, dst, *a, **k)

    f = write(proj, "sub/f.txt", "old\n")
    _read_then(ctx, "sub/f.txt")
    monkeypatch.setattr(os, "replace", spy)
    ok(call(ctx, "write_file", path="sub/f.txt", content="new\n"))
    (src, dst), = seen_names
    assert src.startswith(".m2c-write.") and src.endswith(".tmp") and dst == "f.txt"
    assert temp_files(proj / "sub") == []
    assert f.read_text() == "new\n"


def test_temp_file_is_private_while_it_exists(ctx, proj, monkeypatch):
    """The temp file of a 0600 file must never be group/world readable, even
    briefly, whatever the umask."""
    f = write(proj, "secret-notes.txt", "x\n", mode=0o600)
    _read_then(ctx, "secret-notes.txt")
    modes = []
    real_fsync = os.fsync

    def look(fd):
        # called for the temp file and again for the directory after the rename
        temps = [p for p in proj.iterdir() if p.name.startswith(".m2c-write.")]
        if temps:
            modes.append(mode_of(temps[0]))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", look)
    old = os.umask(0)
    try:
        ok(call(ctx, "write_file", path="secret-notes.txt", content="y\n"))
    finally:
        os.umask(old)
    assert modes and modes[0] & 0o077 == 0
    assert mode_of(f) == 0o600


# ==========================================================================
# Interrupted writes
# ==========================================================================
def test_failure_at_the_rename_leaves_the_original_and_no_temp(ctx, proj, monkeypatch):
    f = write(proj, "f.txt", "original\n")
    _read_then(ctx, "f.txt")

    def boom(*a, **k):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(os, "replace", boom)
    result = fails(call(ctx, "write_file", path="f.txt", content="NEW\n"), "io_error")
    assert "NEW" not in result.output
    assert f.read_text() == "original\n"
    assert temp_files(proj) == []


def test_failure_at_fsync_leaves_the_original_and_no_temp(ctx, proj, monkeypatch):
    f = write(proj, "f.txt", "original\n")
    _read_then(ctx, "f.txt")
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(OSError(5, "Input/output error")))
    fails(call(ctx, "write_file", path="f.txt", content="NEW\n"), "io_error")
    assert f.read_text() == "original\n"
    assert temp_files(proj) == []


def test_disk_full_while_writing_the_temp(ctx, proj, monkeypatch):
    f = write(proj, "f.txt", "original\n")
    _read_then(ctx, "f.txt")
    monkeypatch.setattr(os, "write", lambda fd, data: (_ for _ in ()).throw(OSError(28, "No space left on device")))
    result = fails(call(ctx, "write_file", path="f.txt", content="NEW\n"), "io_error")
    assert "space" in result.output.lower()
    assert f.read_text() == "original\n"
    assert temp_files(proj) == []


def test_failure_of_a_brand_new_file_leaves_nothing_behind(ctx, proj, monkeypatch):
    monkeypatch.setattr(os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError(5, "I/O")))
    fails(call(ctx, "write_file", path="new/dir/f.txt", content="x"), "io_error")
    assert not (proj / "new" / "dir" / "f.txt").exists()
    assert temp_files(proj / "new" / "dir") == []


def test_an_interrupt_during_the_write_is_cleaned_up_and_propagates(ctx, proj, monkeypatch):
    f = write(proj, "f.txt", "original\n")
    _read_then(ctx, "f.txt")
    monkeypatch.setattr(os, "fsync", lambda fd: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        call(ctx, "write_file", path="f.txt", content="NEW\n")
    assert f.read_text() == "original\n"
    assert temp_files(proj) == []


CHILD_KILLED_AT_RENAME = """
import os, signal, sys
from pathlib import Path
from m2c.tools import ToolContext, run_tool

ctx = ToolContext(Path(sys.argv[1]))
assert run_tool(ctx, "read_file", {"path": "target.txt"}).ok

def die(*args, **kwargs):          # the process is killed right where the rename would happen
    os.kill(os.getpid(), signal.SIGKILL)

os.replace = die
run_tool(ctx, "write_file", {"path": "target.txt", "content": "NEW CONTENT THAT MUST NOT APPEAR\\n"})
sys.exit(3)                        # unreachable if the kill worked
"""


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="SIGKILL is POSIX-only")
def test_process_killed_between_the_temp_write_and_the_rename(ctx, proj):
    """The real thing: a child process is SIGKILLed at the moment of the
    rename (no finally, no atexit).  The original file must be untouched, the
    leftover temp file must carry the dead child's pid, and the next write in
    that directory must clean it up."""
    target = write(proj, "target.txt", "ORIGINAL CONTENT\n", mode=0o640)
    env = dict(os.environ, PYTHONPATH=str(SRC_ROOT))
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD_KILLED_AT_RENAME, str(proj)],
        env=env,
        cwd=str(proj.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    _out, err = child.communicate(timeout=60)
    assert child.returncode == -signal.SIGKILL, err.decode()

    assert target.read_text() == "ORIGINAL CONTENT\n"
    assert mode_of(target) == 0o640
    left = temp_files(proj)
    assert len(left) == 1
    assert re.fullmatch(rf"\.m2c-write\.{child.pid}\.[0-9a-f]{{16}}\.tmp", left[0])
    # the temp holds the complete new content: the crash happened after the temp was durable
    assert (proj / left[0]).read_text() == "NEW CONTENT THAT MUST NOT APPEAR\n"

    ok(call(ctx, "read_file", path="target.txt"))
    ok(call(ctx, "write_file", path="target.txt", content="after recovery\n"))
    assert target.read_text() == "after recovery\n"
    assert temp_files(proj) == []


def test_changes_made_by_someone_else_during_the_write_are_not_clobbered(ctx, proj, monkeypatch):
    f = write(proj, "f.txt", "original\n")
    _read_then(ctx, "f.txt")
    real_fsync = os.fsync
    fired = []

    def meddle(fd):
        if not fired:
            fired.append(1)
            f.write_text("someone else wrote a longer line while we were busy\n")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", meddle)
    fails(call(ctx, "write_file", path="f.txt", content="mine\n"), "changed_since_read")
    assert f.read_text() == "someone else wrote a longer line while we were busy\n"
    assert temp_files(proj) == []


def test_the_file_is_swapped_for_a_symlink_just_before_the_rename(ctx, proj, outside, monkeypatch):
    f = write(proj, "f.txt", "original\n")
    _read_then(ctx, "f.txt")
    real_fsync = os.fsync
    fired = []

    def swap(fd):
        if not fired:
            fired.append(1)
            f.unlink()
            f.symlink_to(outside / "secret.txt")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", swap)
    result = call(ctx, "write_file", path="f.txt", content="mine\n")
    assert not result.ok
    assert (outside / "secret.txt").read_text() == f"{SECRET} outside\n"
    assert temp_files(proj) == []


# --- stale temp files ------------------------------------------------------
def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait()
    return p.pid


def test_stale_temp_of_a_dead_process_is_removed_by_the_next_write(ctx, proj):
    stale = proj / f".m2c-write.{_dead_pid()}.0123456789abcdef.tmp"
    stale.write_text("abandoned")
    ok(call(ctx, "write_file", path="f.txt", content="x"))
    assert not stale.exists()


def test_a_young_temp_of_a_live_process_is_left_alone(ctx, proj):
    live = proj / f".m2c-write.{os.getpid()}.0123456789abcdef.tmp"
    live.write_text("in progress")
    ok(call(ctx, "write_file", path="f.txt", content="x"))
    assert live.exists()


def test_a_very_old_temp_is_removed_even_if_its_pid_is_alive(ctx, proj):
    old = proj / f".m2c-write.{os.getpid()}.0123456789abcdef.tmp"
    old.write_text("ancient")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (long_ago, long_ago))
    ok(call(ctx, "write_file", path="f.txt", content="x"))
    assert not old.exists()


def test_sweeping_only_touches_files_that_match_the_temp_name_exactly(ctx, proj):
    pid = _dead_pid()
    keepers = [
        proj / ".m2c-write.notapid.0123456789abcdef.tmp",
        proj / f".m2c-write.{pid}.short.tmp",
        proj / f".m2c-write.{pid}.0123456789abcdef.txt",
        proj / f"x.m2c-write.{pid}.0123456789abcdef.tmp",
    ]
    for k in keepers:
        k.write_text("mine")
    link = proj / f".m2c-write.{pid}.fedcba9876543210.tmp"
    link.symlink_to(keepers[0])
    d = proj / f".m2c-write.{pid}.aaaaaaaaaaaaaaaa.tmp"
    d.mkdir()
    ok(call(ctx, "write_file", path="f.txt", content="x"))
    assert all(k.exists() for k in keepers)
    assert link.is_symlink() and d.is_dir()


def test_sweep_stale_temps_is_public_and_reports_a_count(ctx, proj):
    pid = _dead_pid()
    for i in range(3):
        (proj / f".m2c-write.{pid}.{i:016x}.tmp").write_text("x")
    assert tool_files.sweep_stale_temps(ctx, ()) == 3
    assert temp_files(proj) == []


# ==========================================================================
# Races between the path check and the open
# ==========================================================================
def test_read_target_swapped_for_a_symlink_after_the_check_is_not_followed(ctx, proj, outside, monkeypatch):
    f = write(proj, "f.txt", "inside\n")
    real_open = os.open

    def swap_then_open(name, flags, *args, **kwargs):
        if name == "f.txt" and "dir_fd" in kwargs and f.exists() and not f.is_symlink():
            f.unlink()
            f.symlink_to(outside / "secret.txt")
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    result = call(ctx, "read_file", path="f.txt")
    assert not result.ok and result.code == "symlink_refused"
    assert SECRET not in result.output


def test_a_directory_swapped_for_a_symlink_after_the_check_is_not_followed(ctx, proj, outside, monkeypatch):
    (proj / "d").mkdir()
    real_open = os.open
    fired = []

    def swap_then_open(name, flags, *args, **kwargs):
        if name == "d" and "dir_fd" in kwargs and not fired:
            fired.append(1)
            (proj / "d").rmdir()
            (proj / "d").symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    result = call(ctx, "write_file", path="d/new.txt", content="PWNED")
    assert not result.ok and result.code == "symlink_refused"
    assert not (outside / "new.txt").exists()


# ==========================================================================
# delete_path
# ==========================================================================
def test_delete_a_file(ctx, proj):
    f = write(proj, "f.txt")
    assert "Deleted" in ok(call(ctx, "delete_path", path="f.txt"))
    assert not f.exists()


def test_delete_an_empty_directory(ctx, proj):
    (proj / "empty").mkdir()
    ok(call(ctx, "delete_path", path="empty"))
    assert not (proj / "empty").exists()


def test_delete_a_non_empty_directory_is_refused_by_default(ctx, proj):
    write(proj, "d/inner.txt")
    result = fails(call(ctx, "delete_path", path="d"), "directory_not_empty")
    assert "not empty" in result.output
    assert (proj / "d" / "inner.txt").exists()


def test_the_model_cannot_ask_for_recursive_deletion(ctx, proj):
    write(proj, "d/inner.txt")
    fails(call(ctx, "delete_path", path="d", recursive=True), "invalid_args")
    assert (proj / "d" / "inner.txt").exists()


def test_recursive_delete_is_available_to_trusted_callers_only_by_explicit_argument(ctx, proj):
    write(proj, "d/a/b.txt")
    write(proj, "d/c.txt")
    out = tool_files.delete_path(ctx, "d", recursive=True)[0]
    assert "Deleted" in out and not (proj / "d").exists()


def test_recursive_delete_refuses_a_tree_that_contains_a_protected_file(ctx, proj):
    write(proj, "d/ok.txt")
    write(proj, "d/sub/.env", SECRET)
    with pytest.raises(tool_files.ToolError) as ei:
        tool_files.delete_path(ctx, "d", recursive=True)
    assert ei.value.code == "protected_path"
    assert (proj / "d" / "ok.txt").exists() and (proj / "d" / "sub" / ".env").exists()


def test_recursive_delete_refuses_a_tree_that_contains_a_git_directory(ctx, proj):
    write(proj, "vendor/lib/.git/HEAD")
    write(proj, "vendor/lib/a.py")
    with pytest.raises(tool_files.ToolError) as ei:
        tool_files.delete_path(ctx, "vendor", recursive=True)
    assert ei.value.code == "refused"
    assert (proj / "vendor" / "lib" / "a.py").exists()


def test_recursive_delete_unlinks_symlinks_and_never_follows_them(ctx, proj, outside):
    write(proj, "d/a.txt")
    (proj / "d" / "dirlink").symlink_to(outside)
    (proj / "d" / "filelink").symlink_to(outside / "secret.txt")
    tool_files.delete_path(ctx, "d", recursive=True)
    assert not (proj / "d").exists()
    assert (outside / "secret.txt").read_text() == f"{SECRET} outside\n"


def test_recursive_delete_size_cap(proj):
    context = ToolContext(proj, caps=Caps(max_delete_entries=5))
    for i in range(10):
        write(proj, f"d/f{i}.txt")
    with pytest.raises(tool_files.ToolError) as ei:
        tool_files.delete_path(context, "d", recursive=True)
    assert ei.value.code == "refused"
    assert len(list((proj / "d").iterdir())) == 10


def test_the_project_root_cannot_be_deleted(ctx, proj):
    write(proj, "f.txt")
    for raw in (".", "", str(proj), "sub/..", "./"):
        result = call(ctx, "delete_path", path=raw)
        assert not result.ok and result.code in ("refused", "invalid_path")
    with pytest.raises(tool_files.ToolError):
        tool_files.delete_path(ctx, ".", recursive=True)
    assert proj.exists() and (proj / "f.txt").exists()


@pytest.mark.parametrize("rel", [".git", ".git/config", ".git/hooks/pre-commit", "sub/.git", "sub/.git/HEAD"])
def test_git_cannot_be_deleted(ctx, proj, rel):
    write(proj, ".git/config")
    write(proj, ".git/hooks/pre-commit")
    write(proj, "sub/.git/HEAD")
    fails(call(ctx, "delete_path", path=rel), "refused")
    assert (proj / ".git" / "config").exists() and (proj / "sub" / ".git" / "HEAD").exists()


def test_delete_missing_path(ctx):
    fails(call(ctx, "delete_path", path="nope"), "not_found")


def test_deleting_a_symlink_removes_the_link_and_not_its_target(ctx, proj):
    real = write(proj, "real.txt", "keep\n")
    (proj / "alias.txt").symlink_to("real.txt")
    ok(call(ctx, "delete_path", path="alias.txt"))
    assert not (proj / "alias.txt").exists() and not (proj / "alias.txt").is_symlink()
    assert real.read_text() == "keep\n"


def test_deleting_a_symlink_to_a_directory_removes_only_the_link(ctx, proj):
    write(proj, "real/inner.txt")
    (proj / "alias").symlink_to("real")
    ok(call(ctx, "delete_path", path="alias"))
    assert not (proj / "alias").is_symlink()
    assert (proj / "real" / "inner.txt").exists()


def test_delete_refuses_a_symlink_whose_target_is_outside(ctx, proj, outside):
    (proj / "leak.txt").symlink_to(outside / "secret.txt")
    fails(call(ctx, "delete_path", path="leak.txt"), "outside_root")
    assert (proj / "leak.txt").is_symlink()
    assert (outside / "secret.txt").exists()


def test_delete_path_traversal_and_absolute_paths(ctx, proj, outside):
    fails(call(ctx, "delete_path", path="../outside/secret.txt"), "outside_root")
    fails(call(ctx, "delete_path", path=str(outside / "secret.txt")), "outside_root")
    assert (outside / "secret.txt").exists()


def test_delete_forgets_the_seen_record(ctx, proj):
    f = write(proj, "f.txt", "a\n")
    _read_then(ctx, "f.txt")
    assert ctx.seen.get(f) is not None
    ok(call(ctx, "delete_path", path="f.txt"))
    assert ctx.seen.get(f) is None
    ok(call(ctx, "write_file", path="f.txt", content="again\n"))  # new file again: no read needed
