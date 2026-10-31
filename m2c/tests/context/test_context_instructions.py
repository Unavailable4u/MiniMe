"""
m2c/tests/context/test_context_instructions.py -- Patch C12: loading M2C.md,
AGENTS.md and CLAUDE.md hierarchically, with ``@path`` imports.

Written before the implementation (tests first).  Groups:

* precedence      global -> repo root -> each directory down to the cwd, and
                  M2C.md -> AGENTS.md -> CLAUDE.md inside one directory
* imports         relative, nested, fenced/inline code, cycles, depth limit,
                  outside the repository, symlink escape, non-text files,
                  missing files, duplicates, the import budget
* oversize        per-file cap (marker, line boundary, UTF-8 safety), total cap
                  (least specific content goes first, most specific stays)
* missing         no files, no directories, a FIFO, a directory named like a file
* hygiene         no absolute paths in the output, nothing logged
"""
from __future__ import annotations

import io
import logging
import os
import threading

import pytest
from context_helpers import make_world, note_codes, put
from m2c.context import instructions as ins
from m2c.context.instructions import load_instructions

from m2c import logging_setup

G = "GLOBAL-RULE-7731"
R = "REPO-RULE-5520"
S = "SUBDIR-RULE-9184"


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def load(world, cwd=None, **limits):
    return load_instructions(world.root, cwd or world.root, world.home, **limits)


# ---------------------------------------------------------------------------
# precedence
# ---------------------------------------------------------------------------
def test_global_then_repo_then_each_directory_down_to_the_cwd(world):
    put(world.home / "M2C.md", G)
    put(world.root / "M2C.md", R)
    put(world.root / "svc" / "M2C.md", "MIDDLE-RULE-1")
    put(world.root / "svc" / "api" / "M2C.md", S)
    result = load(world, world.root / "svc" / "api")
    t = result.text
    assert t.index(G) < t.index(R) < t.index("MIDDLE-RULE-1") < t.index(S)
    assert result.sources == ("global/M2C.md", "M2C.md", "svc/M2C.md", "svc/api/M2C.md")


def test_inside_one_directory_the_order_is_m2c_then_agents_then_claude(world):
    put(world.root / "CLAUDE.md", "THIRD")
    put(world.root / "AGENTS.md", "SECOND")
    put(world.root / "M2C.md", "FIRST")
    result = load(world)
    assert result.text.index("FIRST") < result.text.index("SECOND") < result.text.index("THIRD")
    assert result.sources == ("M2C.md", "AGENTS.md", "CLAUDE.md")


def test_only_some_of_the_three_files_may_exist(world):
    put(world.root / "CLAUDE.md", "only claude")
    put(world.root / "sub" / "AGENTS.md", "only agents")
    result = load(world, world.root / "sub")
    assert result.sources == ("CLAUDE.md", "sub/AGENTS.md")


def test_only_the_global_m2c_md_is_read_from_the_global_directory(world):
    put(world.home / "M2C.md", G)
    put(world.home / "AGENTS.md", "NOT-GLOBAL-1")
    put(world.home / "CLAUDE.md", "NOT-GLOBAL-2")
    result = load(world)
    assert G in result.text and "NOT-GLOBAL" not in result.text


def test_directories_not_on_the_way_to_the_cwd_are_not_read(world):
    put(world.root / "a" / "M2C.md", "SIBLING-A")
    put(world.root / "b" / "M2C.md", "CWD-B")
    put(world.root / "b" / "deeper" / "M2C.md", "BELOW-THE-CWD")
    result = load(world, world.root / "b")
    assert "CWD-B" in result.text
    assert "SIBLING-A" not in result.text and "BELOW-THE-CWD" not in result.text


def test_cwd_equal_to_root_reads_the_root_files_once(world):
    put(world.root / "M2C.md", R)
    result = load(world)
    assert result.text.count(R) == 1 and result.sources == ("M2C.md",)


def test_cwd_outside_the_root_is_rejected(world):
    with pytest.raises(ValueError):
        load(world, world.outside)


def test_cwd_given_through_a_symlink_into_the_root_is_accepted(world, tmp_path):
    put(world.root / "sub" / "M2C.md", S)
    alias = tmp_path / "alias"
    alias.symlink_to(world.root / "sub")
    result = load(world, alias)
    assert S in result.text


def test_global_directory_follows_m2c_home_when_home_is_not_passed(world, monkeypatch):
    put(world.home / "M2C.md", G)
    monkeypatch.setenv("M2C_HOME", str(world.home))
    result = load_instructions(world.root, world.root)
    assert G in result.text


def test_global_directory_defaults_to_dot_m2c_in_the_home_directory(world, monkeypatch, tmp_path):
    monkeypatch.delenv("M2C_HOME", raising=False)
    fake_home = tmp_path / "fakehome"
    put(fake_home / ".m2c" / "M2C.md", G)
    monkeypatch.setenv("HOME", str(fake_home))
    result = load_instructions(world.root, world.root)
    assert G in result.text


def test_a_relative_m2c_home_fails_closed_instead_of_guessing(world, monkeypatch):
    from m2c.paths import ConfigDirError

    monkeypatch.setenv("M2C_HOME", "relative/dir")
    with pytest.raises(ConfigDirError):
        load_instructions(world.root, world.root)


def test_each_file_is_introduced_by_its_label(world):
    put(world.root / "M2C.md", R)
    put(world.root / "sub" / "AGENTS.md", S)
    text = load(world, world.root / "sub").text
    assert "<!-- m2c: instructions from M2C.md -->" in text
    assert "<!-- m2c: instructions from sub/AGENTS.md -->" in text


def test_crlf_and_a_byte_order_mark_are_normalised(world):
    put(world.root / "M2C.md", data=b"\xef\xbb\xbfline one\r\nline two\r\n")
    text = load(world).text
    assert "\r" not in text and "\ufeff" not in text and "line one\nline two" in text


# ---------------------------------------------------------------------------
# missing files and directories
# ---------------------------------------------------------------------------
def test_a_repository_without_any_instruction_files_gives_empty_text_and_no_notes(world):
    result = load(world)
    assert result.text == "" and result.sources == () and result.notes == ()


def test_a_missing_global_directory_is_not_an_error(world, tmp_path):
    put(world.root / "M2C.md", R)
    result = load_instructions(world.root, world.root, tmp_path / "does-not-exist")
    assert R in result.text and result.notes == ()


def test_a_missing_repository_root_gives_empty_text(tmp_path):
    result = load_instructions(tmp_path / "nope", tmp_path / "nope", tmp_path / "also-nope")
    assert result.text == "" and result.sources == ()


def test_a_cwd_that_does_not_exist_still_loads_the_directories_above_it(world):
    put(world.root / "M2C.md", R)
    result = load(world, world.root / "not" / "created" / "yet")
    assert R in result.text


def test_empty_and_whitespace_only_files_contribute_nothing(world):
    put(world.root / "M2C.md", "   \n\n\t\n")
    put(world.root / "AGENTS.md", "")
    result = load(world)
    assert result.text == "" and result.sources == ()


def test_a_directory_named_like_an_instruction_file_is_skipped_without_a_crash(world):
    (world.root / "M2C.md").mkdir()
    put(world.root / "AGENTS.md", R)
    result = load(world)
    assert R in result.text and "unreadable" in note_codes(result)


def test_a_fifo_named_like_an_instruction_file_does_not_hang_the_loader(world):
    os.mkfifo(world.root / "M2C.md")
    put(world.root / "AGENTS.md", R)
    box: dict = {}

    def run():
        box["result"] = load(world)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(10)
    assert not thread.is_alive(), "loading blocked on a FIFO"
    assert R in box["result"].text and "unreadable" in note_codes(box["result"])


def test_a_file_with_nul_bytes_is_not_text_and_is_skipped(world):
    put(world.root / "M2C.md", data=b"binary\x00junk")
    put(world.root / "AGENTS.md", R)
    result = load(world)
    assert "binary" not in result.text and R in result.text
    assert "not_text" in note_codes(result)


def test_a_broken_symlink_named_like_an_instruction_file_is_skipped(world):
    (world.root / "M2C.md").symlink_to(world.root / "gone.md")
    put(world.root / "AGENTS.md", R)
    result = load(world)
    assert R in result.text and result.notes


# ---------------------------------------------------------------------------
# imports: the happy paths
# ---------------------------------------------------------------------------
def test_a_relative_import_pulls_the_file_in_where_the_line_is(world):
    put(world.root / "docs" / "style.md", "STYLE-BODY")
    put(world.root / "M2C.md", "before\n@docs/style.md\nafter")
    text = load(world).text
    assert text.index("before") < text.index("STYLE-BODY") < text.index("after")
    assert "<!-- m2c: begin import docs/style.md -->" in text
    assert "<!-- m2c: end import docs/style.md -->" in text


def test_the_import_is_resolved_relative_to_the_importing_file_not_the_cwd_or_root(world):
    put(world.root / "sub" / "extra.md", "SUB-EXTRA")
    put(world.root / "extra.md", "ROOT-EXTRA")
    put(world.root / "sub" / "M2C.md", "@extra.md")
    result = load(world, world.root / "sub")
    assert "SUB-EXTRA" in result.text and "ROOT-EXTRA" not in result.text


def test_dot_slash_and_parent_directory_imports_inside_the_repo_work(world):
    put(world.root / "shared.md", "SHARED")
    put(world.root / "sub" / "local.md", "LOCAL")
    put(world.root / "sub" / "M2C.md", "@./local.md\n@../shared.md")
    result = load(world, world.root / "sub")
    assert "LOCAL" in result.text and "SHARED" in result.text
    assert result.imports == ("sub/local.md", "shared.md")


def test_nested_imports_resolve_relative_to_each_importer(world):
    put(world.root / "docs" / "a.md", "A-START\n@sub/b.md\nA-END")
    put(world.root / "docs" / "sub" / "b.md", "B-START\n@c.md\nB-END")
    put(world.root / "docs" / "sub" / "c.md", "C-BODY")
    put(world.root / "M2C.md", "@docs/a.md")
    t = load(world).text
    order = [t.index(x) for x in ("A-START", "B-START", "C-BODY", "B-END", "A-END")]
    assert order == sorted(order)


def test_an_import_in_the_middle_of_a_sentence_keeps_the_surrounding_words(world):
    put(world.root / "style.md", "STYLE-BODY")
    put(world.root / "M2C.md", "Please follow @style.md carefully.")
    t = load(world).text
    assert "Please follow" in t and "carefully." in t and "STYLE-BODY" in t
    assert t.index("Please follow") < t.index("STYLE-BODY") < t.index("carefully.")


def test_trailing_sentence_punctuation_is_not_part_of_the_path(world):
    put(world.root / "style.md", "STYLE-BODY")
    put(world.root / "M2C.md", "See @style.md.")
    assert "STYLE-BODY" in load(world).text


def test_txt_files_and_uppercase_extensions_can_be_imported(world):
    put(world.root / "notes.txt", "TXT-BODY")
    put(world.root / "UPPER.MD", "UPPER-BODY")
    put(world.root / "M2C.md", "@notes.txt\n@UPPER.MD")
    t = load(world).text
    assert "TXT-BODY" in t and "UPPER-BODY" in t


def test_the_global_file_can_import_files_inside_the_global_directory(world):
    put(world.home / "prefs" / "style.md", "GLOBAL-STYLE")
    put(world.home / "M2C.md", "@prefs/style.md")
    result = load(world)
    assert "GLOBAL-STYLE" in result.text and result.imports == ("global/prefs/style.md",)


def test_the_global_file_cannot_import_from_the_repository(world):
    put(world.root / "secret-plan.md", "REPO-ONLY")
    put(world.home / "M2C.md", f"@{world.root / 'secret-plan.md'}")
    result = load(world)
    assert "REPO-ONLY" not in result.text and "outside_root" in note_codes(result)


# ---------------------------------------------------------------------------
# imports: things that look like imports but are not
# ---------------------------------------------------------------------------
def test_imports_inside_fenced_code_blocks_are_left_alone(world):
    put(world.root / "x.md", "SHOULD-NOT-APPEAR")
    put(world.root / "M2C.md", "```\n@x.md\n```\n~~~\n@x.md\n~~~")
    result = load(world)
    assert "SHOULD-NOT-APPEAR" not in result.text and result.notes == ()
    assert result.text.count("@x.md") == 2


def test_imports_inside_inline_code_spans_are_left_alone(world):
    put(world.root / "x.md", "SHOULD-NOT-APPEAR")
    put(world.root / "M2C.md", "Write `@x.md` to import a file.")
    result = load(world)
    assert "SHOULD-NOT-APPEAR" not in result.text and result.notes == ()


def test_email_addresses_and_plain_mentions_are_not_imports(world):
    put(world.root / "M2C.md", "Ask someone@example.com or @alice about it.")
    result = load(world)
    assert "someone@example.com" in result.text and "@alice" in result.text
    assert result.notes == ()


def test_a_url_with_an_at_sign_is_not_an_import(world):
    put(world.root / "M2C.md", "See https://example.com/@user/page.md for details.")
    result = load(world)
    assert result.notes == () and "https://example.com/@user/page.md" in result.text


# ---------------------------------------------------------------------------
# imports: refusals (each one a note, never a crash)
# ---------------------------------------------------------------------------
def test_a_missing_import_is_a_note_and_everything_else_still_loads(world):
    put(world.root / "M2C.md", "before\n@docs/absent.md\nafter")
    result = load(world)
    assert "before" in result.text and "after" in result.text
    assert note_codes(result) == ["missing"]
    assert "@docs/absent.md" in result.text and "import skipped" in result.text


def test_a_cycle_between_two_files_is_cut_and_each_file_appears_once(world):
    put(world.root / "a.md", "A-BODY\n@b.md")
    put(world.root / "b.md", "B-BODY\n@a.md")
    put(world.root / "M2C.md", "@a.md")
    result = load(world)
    assert result.text.count("A-BODY") == 1 and result.text.count("B-BODY") == 1
    assert "cycle" in note_codes(result)


def test_a_file_importing_itself_is_a_cycle(world):
    put(world.root / "M2C.md", "SELF-BODY\n@M2C.md")
    result = load(world)
    assert result.text.count("SELF-BODY") == 1 and "cycle" in note_codes(result)


def test_the_depth_limit_stops_a_long_chain(world):
    for i in range(1, 8):
        put(world.root / f"c{i}.md", f"LEVEL-{i}\n@c{i + 1}.md")
    put(world.root / "M2C.md", "LEVEL-0\n@c1.md")
    result = load(world)
    for i in range(6):                       # M2C.md plus five hops
        assert f"LEVEL-{i}" in result.text
    assert "LEVEL-6" not in result.text and "LEVEL-7" not in result.text
    assert "depth_limit" in note_codes(result)


def test_the_depth_limit_is_configurable(world):
    put(world.root / "a.md", "LEVEL-A\n@b.md")
    put(world.root / "b.md", "LEVEL-B")
    put(world.root / "M2C.md", "@a.md")
    result = load(world, max_import_depth=1)
    assert "LEVEL-A" in result.text and "LEVEL-B" not in result.text
    assert "depth_limit" in note_codes(result)


def test_the_default_depth_limit_is_five():
    assert ins.MAX_IMPORT_DEPTH == 5


def test_an_import_that_climbs_out_of_the_repository_is_refused(world):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    put(world.root / "M2C.md", "@../outside/private.md")
    result = load(world)
    assert "OUTSIDE-SECRET" not in result.text and "outside_root" in note_codes(result)


def test_an_absolute_import_outside_the_repository_is_refused(world):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    put(world.root / "M2C.md", f"@{world.outside / 'private.md'}")
    result = load(world)
    assert "OUTSIDE-SECRET" not in result.text and "outside_root" in note_codes(result)


def test_an_import_of_a_symlink_that_points_out_of_the_repository_is_refused(world):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    (world.root / "link.md").symlink_to(world.outside / "private.md")
    put(world.root / "M2C.md", "@link.md")
    result = load(world)
    assert "OUTSIDE-SECRET" not in result.text and "symlink_escape" in note_codes(result)


def test_an_import_through_a_symlinked_directory_that_leaves_the_repository_is_refused(world):
    put(world.outside / "docs" / "x.md", "OUTSIDE-SECRET")
    (world.root / "docs").symlink_to(world.outside / "docs", target_is_directory=True)
    put(world.root / "M2C.md", "@docs/x.md")
    result = load(world)
    assert "OUTSIDE-SECRET" not in result.text and "symlink_escape" in note_codes(result)


def test_a_symlink_that_stays_inside_the_repository_is_followed(world):
    put(world.root / "real" / "style.md", "REAL-STYLE")
    (world.root / "alias.md").symlink_to(world.root / "real" / "style.md")
    put(world.root / "M2C.md", "@alias.md")
    assert "REAL-STYLE" in load(world).text


def test_a_md_symlink_whose_target_is_not_text_is_refused(world):
    put(world.root / "tool.py", "print('PY-BODY')")
    (world.root / "innocent.md").symlink_to(world.root / "tool.py")
    put(world.root / "M2C.md", "@innocent.md")
    result = load(world)
    assert "PY-BODY" not in result.text and "not_text" in note_codes(result)


@pytest.mark.parametrize("name", ["script.py", "image.png", "data.json", "Makefile.txt.bak", "dir/run.sh"])
def test_non_text_files_cannot_be_imported(world, name):
    put(world.root / name, "NON-TEXT-BODY")
    put(world.root / "M2C.md", f"@{name}")
    result = load(world)
    assert "NON-TEXT-BODY" not in result.text and note_codes(result) == ["not_text"]


def test_a_dotenv_file_cannot_be_imported_even_inside_the_repo(world):
    put(world.root / ".env", "TOKEN_VALUE_FOR_TEST=1")
    put(world.root / "M2C.md", "@.env")
    result = load(world)
    assert "TOKEN_VALUE_FOR_TEST" not in result.text and "not_text" in note_codes(result)


def test_an_imported_file_with_nul_bytes_is_not_text(world):
    put(world.root / "bin.md", data=b"abc\x00def")
    put(world.root / "M2C.md", "@bin.md")
    assert "not_text" in note_codes(load(world))


def test_importing_the_same_file_twice_includes_it_once(world):
    put(world.root / "common.md", "COMMON-BODY")
    put(world.root / "a.md", "@common.md")
    put(world.root / "b.md", "@common.md")
    put(world.root / "M2C.md", "@a.md\n@b.md")
    result = load(world)
    assert result.text.count("COMMON-BODY") == 1 and "duplicate" in note_codes(result)


def test_a_symlinked_agents_md_pointing_at_claude_md_is_loaded_once(world):
    put(world.root / "CLAUDE.md", "SHARED-GUIDE")
    (world.root / "AGENTS.md").symlink_to(world.root / "CLAUDE.md")
    result = load(world)
    assert result.text.count("SHARED-GUIDE") == 1 and "duplicate" in note_codes(result)


def test_a_discovered_instruction_file_that_is_a_symlink_out_of_the_repo_is_skipped(world):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    (world.root / "CLAUDE.md").symlink_to(world.outside / "private.md")
    put(world.root / "M2C.md", R)
    result = load(world)
    assert "OUTSIDE-SECRET" not in result.text and R in result.text
    assert "symlink_escape" in note_codes(result)


def test_a_symlinked_subdirectory_instruction_file_cannot_leak_either(world):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    (world.root / "sub").mkdir()
    (world.root / "sub" / "AGENTS.md").symlink_to(world.outside / "private.md")
    result = load(world, world.root / "sub")
    assert "OUTSIDE-SECRET" not in result.text


def test_a_huge_fan_out_of_imports_is_bounded_by_the_budget(world):
    for i in range(60):
        put(world.root / f"f{i}.md", f"FILE-{i} " + "x" * 90)
    put(world.root / "M2C.md", "\n".join(f"@f{i}.md" for i in range(60)))
    result = load(world, max_total_bytes=1500)
    assert len(result.text.encode()) <= 1500
    assert "FILE-59" not in result.text
    assert {"import_budget", "total_cap"} & set(note_codes(result))


def test_a_billion_laughs_style_import_tree_stays_small(world):
    for level in range(4):                       # M2C.md -> l0 -> l1 -> l2 -> l3 -> l4: five hops, the limit
        put(world.root / f"l{level}.md", f"@l{level + 1}.md\n@l{level + 1}.md\n@l{level + 1}.md")
    put(world.root / "l4.md", "LEAF " * 100)
    put(world.root / "M2C.md", "@l0.md")
    result = load(world)
    assert result.text.count("LEAF") == 100          # the leaf is included once, not 3**4 times
    assert "depth_limit" not in note_codes(result) and "duplicate" in note_codes(result)


# ---------------------------------------------------------------------------
# oversize files
# ---------------------------------------------------------------------------
def test_an_oversize_file_is_truncated_with_a_marker_and_a_note(world):
    lines = [f"line {i:04d} " + "y" * 40 for i in range(400)]
    put(world.root / "M2C.md", "\n".join(lines))
    result = load(world, max_file_bytes=2048)
    assert "truncated" in note_codes(result)
    assert "<!-- m2c: truncated: M2C.md" in result.text
    body = result.text.split("-->\n", 1)[1]
    assert len(body.encode()) < 2048 + 200
    assert "line 0399" not in result.text and "line 0000" in result.text


def test_truncation_happens_on_a_line_boundary(world):
    put(world.root / "M2C.md", "\n".join(f"line {i:03d}" for i in range(500)))
    result = load(world, max_file_bytes=1000)
    kept = [ln for ln in result.text.splitlines() if ln.startswith("line ")]
    assert kept and all(len(ln) == len("line 000") for ln in kept)


def test_truncation_never_splits_a_multibyte_character(world):
    put(world.root / "M2C.md", "é" * 5000)                 # 2 bytes each, one long line
    result = load(world, max_file_bytes=1001)
    assert "�" not in result.text and "truncated" in note_codes(result)


def test_an_oversize_imported_file_is_truncated_too(world):
    put(world.root / "big.md", "\n".join(f"big {i:04d}" for i in range(2000)))
    put(world.root / "M2C.md", "@big.md")
    result = load(world, max_file_bytes=1024)
    assert "<!-- m2c: truncated: big.md" in result.text and "big 1999" not in result.text


def test_a_file_exactly_at_the_cap_is_not_truncated(world):
    put(world.root / "M2C.md", "a" * 1000)
    result = load(world, max_file_bytes=1000)
    assert "truncated" not in note_codes(result) and "a" * 1000 in result.text


def test_default_caps_are_sane():
    assert ins.MAX_FILE_BYTES == 32 * 1024
    assert ins.MAX_TOTAL_BYTES <= 200_000          # protocol bulk_text ceiling


def test_the_total_cap_drops_the_least_specific_files_first(world):
    put(world.home / "M2C.md", "GLOBAL-" + "g" * 400)
    put(world.root / "M2C.md", "ROOT-" + "r" * 400)
    put(world.root / "sub" / "M2C.md", "SUBDIR-" + "s" * 400)
    result = load(world, world.root / "sub", max_total_bytes=700)
    assert len(result.text.encode()) <= 700
    assert "SUBDIR-" in result.text                       # the most specific survives
    assert "GLOBAL-" not in result.text
    codes = note_codes(result)
    assert codes.count("total_cap") >= 1
    assert "left out (size limit)" in result.text          # the model is told too
    assert result.sources[-1] == "sub/M2C.md"


def test_the_total_cap_truncates_a_single_oversize_survivor(world):
    put(world.root / "M2C.md", "\n".join(f"row {i:03d} " + "z" * 30 for i in range(200)))
    result = load(world, max_total_bytes=800, max_file_bytes=100_000)
    assert len(result.text.encode()) <= 800 and "row 000" in result.text
    assert "truncated" in note_codes(result)


def test_the_assembled_text_never_exceeds_the_cap_even_with_many_markers(world):
    for d in range(6):
        put(world.root.joinpath(*[f"d{i}" for i in range(d)]) / "M2C.md", f"DIR-{d} " + "w" * 300)
    deepest = world.root.joinpath(*[f"d{i}" for i in range(5)])
    result = load(world, deepest, max_total_bytes=900)
    assert len(result.text.encode()) <= 900


# ---------------------------------------------------------------------------
# hygiene: no absolute paths in the output, nothing in the logs
# ---------------------------------------------------------------------------
def test_the_output_and_notes_never_contain_an_absolute_path(world, tmp_path):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    put(world.root / "docs" / "ok.md", "OK")
    put(world.root / "M2C.md", "@docs/ok.md\n@docs/gone.md\n@../outside/private.md\n@x.py")
    (world.root / "CLAUDE.md").symlink_to(world.outside / "private.md")
    result = load(world)
    blob = result.text + "\n".join(f"{n.code} {n.source} {n.message}" for n in result.notes)
    assert str(tmp_path) not in blob and str(world.root) not in blob


def test_nothing_from_the_files_or_their_paths_is_logged(world, tmp_path):
    secret_text = "UNIQUE-FILE-CONTENT-48151623"
    put(world.root / "M2C.md", f"{secret_text}\n@missing-import.md")
    put(world.root / "docs" / "x.md", secret_text)
    stream = io.StringIO()
    logging_setup.configure_logging(logging.DEBUG, stream)
    try:
        load(world)
    finally:
        logging.getLogger("m2c").handlers.clear()
    out = stream.getvalue()
    assert secret_text not in out and "missing-import" not in out and str(tmp_path) not in out


def test_notes_have_the_documented_shape(world):
    put(world.root / "M2C.md", "@absent.md")
    note = load(world).notes[0]
    assert (note.code, note.source) == ("missing", "M2C.md")
    assert isinstance(note.message, str) and note.message
    assert "absent.md" in note.message
