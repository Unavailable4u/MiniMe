"""
m2c/tests/context/test_context_rules.py -- Patch C12: path-scoped rules from
``.m2c/rules/*.md`` (and ``<M2C home>/rules/*.md``).

Written before the implementation.  Groups: loading and ids, the front-matter
mini-parser (valid and malformed), glob matching, selecting the rules that
apply to the files in play, size caps, and containment (a symlinked rule file
cannot pull in a file from outside the repository).
"""
from __future__ import annotations

import time

import pytest
from context_helpers import make_world, note_codes, put
from m2c.context import rules as rl
from m2c.context.rules import (
    FrontMatterError,
    glob_match,
    load_rules,
    parse_front_matter,
    rules_for_run_start,
    select_rules,
)


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def rules_dir(world):
    return world.root / ".m2c" / "rules"


def by_id(result):
    return {r.id: r for r in result.rules}


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------
def test_a_rule_without_front_matter_applies_everywhere(world):
    put(rules_dir(world) / "style.md", "Use four spaces.\n")
    result = load_rules(world.root, world.home)
    rule = by_id(result)["project/style"]
    assert rule.paths == () and rule.text == "Use four spaces."
    assert result.notes == ()


def test_a_rule_with_paths_records_its_globs(world):
    put(rules_dir(world) / "py.md", "---\npaths:\n  - 'src/**/*.py'\n  - tests/**\n---\nPython rules.\n")
    rule = by_id(load_rules(world.root, world.home))["project/py"]
    assert rule.paths == ("src/**/*.py", "tests/**") and rule.text == "Python rules."


def test_global_rules_come_first_then_project_rules_each_sorted_by_name(world):
    put(world.home / "rules" / "b-global.md", "G-B")
    put(world.home / "rules" / "a-global.md", "G-A")
    put(rules_dir(world) / "z-proj.md", "P-Z")
    put(rules_dir(world) / "c-proj.md", "P-C")
    ids = [r.id for r in load_rules(world.root, world.home).rules]
    assert ids == ["global/a-global", "global/b-global", "project/c-proj", "project/z-proj"]


def test_the_home_directory_defaults_from_m2c_home(world, monkeypatch):
    put(world.home / "rules" / "g.md", "G")
    monkeypatch.setenv("M2C_HOME", str(world.home))
    assert [r.id for r in load_rules(world.root).rules] == ["global/g"]


def test_missing_directories_give_an_empty_result(world, tmp_path):
    result = load_rules(world.root, tmp_path / "no-home")
    assert result.rules == () and result.notes == ()


def test_a_rules_path_that_is_a_file_is_ignored(world):
    put(world.root / ".m2c" / "rules", "not a directory")
    assert load_rules(world.root, world.home).rules == ()


def test_only_md_files_directly_inside_the_directory_are_rules(world):
    put(rules_dir(world) / "yes.md", "YES")
    put(rules_dir(world) / "notes.txt", "NO-1")
    put(rules_dir(world) / ".hidden.md", "NO-2")
    put(rules_dir(world) / "sub" / "nested.md", "NO-3")
    (rules_dir(world) / "adir.md").mkdir()
    ids = [r.id for r in load_rules(world.root, world.home).rules]
    assert ids == ["project/yes"]


def test_an_empty_rule_is_skipped_with_a_note(world):
    put(rules_dir(world) / "blank.md", "---\npaths: ['a/**']\n---\n\n  \n")
    put(rules_dir(world) / "ok.md", "OK")
    result = load_rules(world.root, world.home)
    assert [r.id for r in result.rules] == ["project/ok"] and "empty_rule" in note_codes(result)


def test_crlf_files_and_a_byte_order_mark_are_handled(world):
    put(rules_dir(world) / "crlf.md", data=b"\xef\xbb\xbf---\r\npaths:\r\n  - src/**\r\n---\r\nBody\r\n")
    rule = by_id(load_rules(world.root, world.home))["project/crlf"]
    assert rule.paths == ("src/**",) and rule.text == "Body"


def test_a_rule_file_with_nul_bytes_is_skipped(world):
    put(rules_dir(world) / "bin.md", data=b"abc\x00def")
    put(rules_dir(world) / "ok.md", "OK")
    result = load_rules(world.root, world.home)
    assert [r.id for r in result.rules] == ["project/ok"] and "not_text" in note_codes(result)


def test_rule_ids_stay_within_the_protocol_name_limit(world):
    put(rules_dir(world) / (("long-" * 40) + ".md"), "LONG")       # 203 chars: a legal file name
    rule = load_rules(world.root, world.home).rules[0]
    assert 1 <= len(rule.id) <= 128


# ---------------------------------------------------------------------------
# front matter
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "block, expected",
    [
        ("paths:\n  - src/**\n  - tests/**", ("src/**", "tests/**")),
        ("paths:\n- src/**\n- docs/*.md", ("src/**", "docs/*.md")),             # unindented dashes
        ("paths: [src/**, 'tests/**', \"docs/**\"]", ("src/**", "tests/**", "docs/**")),
        ("paths: src/**/*.py", ("src/**/*.py",)),                                # a single pattern
        ("paths: \"**/*.{ts,tsx}\"", ("**/*.{ts,tsx}",)),                        # braces keep their comma
        ("paths: [\"a/{b,c}/**\", d]", ("a/{b,c}/**", "d")),
        ("# a comment\npaths:\n  # another\n  - a/**\n\n  - b/**", ("a/**", "b/**")),
        ("description: Python style\npaths:\n  - a/**\ntags:\n  - x\n  - y", ("a/**",)),
    ],
)
def test_front_matter_forms_that_are_accepted(block, expected):
    meta, body = parse_front_matter(f"---\n{block}\n---\nBODY\n")
    assert tuple(meta["paths"]) == expected and body == "BODY\n"


def test_text_without_front_matter_is_all_body():
    meta, body = parse_front_matter("# Just a rule\n\nText.\n")
    assert meta == {} and body == "# Just a rule\n\nText.\n"


def test_front_matter_without_paths_means_everywhere(world):
    put(rules_dir(world) / "d.md", "---\ndescription: hello\n---\nBODY")
    assert by_id(load_rules(world.root, world.home))["project/d"].paths == ()


@pytest.mark.parametrize(
    "text",
    [
        "---\npaths:\n  - a/**\nno closing marker\n",                 # never closed
        "---\npaths:\n---\nBODY",                                      # empty paths
        "---\npaths: []\n---\nBODY",                                   # empty flow list
        "---\npaths: [a/**\n---\nBODY",                                 # unterminated flow list
        "---\npaths: ['a/**]\n---\nBODY",                              # unterminated quote
        "---\npaths:\n  - a/**\npaths:\n  - b/**\n---\nBODY",          # duplicate key
        "---\njust some words\n---\nBODY",                             # not key: value
        "---\npaths:\n    nested: map\n---\nBODY",                      # a mapping where a list belongs
        "---\n  - orphan item\n---\nBODY",                             # list item with no key
        "---\npaths:\n  - ''\n---\nBODY",                              # empty pattern
        "---\npaths: [" + ", ".join(f"p{i}/**" for i in range(40)) + "]\n---\nBODY",     # too many patterns
        "---\npaths:\n  - " + "a" * 300 + "\n---\nBODY",                                 # pattern too long
        "---\npaths:\n  - 'a\x01b'\n---\nBODY",                        # control character
        "---\npaths: a/{1,2}{3,4}{5,6}{7,8}{9,10}{1,2}\n---\nBODY",    # brace explosion
    ],
)
def test_malformed_front_matter_is_rejected_by_the_parser(text):
    with pytest.raises(FrontMatterError):
        parse_front_matter(text)


def test_malformed_front_matter_skips_only_that_rule_with_a_note_and_never_crashes(world):
    put(rules_dir(world) / "bad.md", "---\npaths: [oops\n---\nBAD-BODY")
    put(rules_dir(world) / "worse.md", "---\n\x00\x00\n---\nWORSE")
    put(rules_dir(world) / "good.md", "---\npaths: [a/**]\n---\nGOOD-BODY")
    result = load_rules(world.root, world.home)
    assert [r.id for r in result.rules] == ["project/good"]
    assert note_codes(result).count("bad_front_matter") >= 1
    assert all("BAD-BODY" not in n.message for n in result.notes)


def test_front_matter_error_messages_do_not_echo_the_file_content(world):
    put(rules_dir(world) / "bad.md", "---\npaths: [UNIQUE-SECRET-TEXT\n---\nBODY")
    result = load_rules(world.root, world.home)
    assert result.notes and all("UNIQUE-SECRET-TEXT" not in n.message for n in result.notes)


# ---------------------------------------------------------------------------
# glob matching
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "pattern, path, expected",
    [
        ("*.py", "a.py", True),
        ("*.py", "src/deep/a.py", True),                  # no slash: any depth
        ("*.py", "a.pyc", False),
        ("src/**/*.py", "src/a.py", True),
        ("src/**/*.py", "src/x/y/z/a.py", True),
        ("src/**/*.py", "lib/a.py", False),
        ("src/**/*.py", "other/src/a.py", False),         # a slash anchors at the root
        ("src/**", "src/a.py", True),
        ("src/**", "src/x/y.txt", True),
        ("src/**", "src", False),
        ("**/test_*.py", "tests/test_a.py", True),
        ("**/test_*.py", "test_a.py", True),
        ("**/test_*.py", "tests/a_test.py", False),
        ("docs/", "docs/a.md", True),                     # trailing slash: a directory
        ("docs/", "src/docs/a.md", True),                 # ... at any depth (no inner slash)
        ("/README.md", "README.md", True),                # leading slash anchors
        ("/README.md", "sub/README.md", False),
        ("README.md", "sub/README.md", True),
        ("./src/*.py", "src/a.py", True),
        ("src/*.py", "src/x/a.py", False),                # * does not cross a slash
        ("**/*.{ts,tsx}", "web/app/page.tsx", True),
        ("**/*.{ts,tsx}", "web/app/page.js", False),
        ("{src,lib}/**", "lib/a/b.c", True),
        ("{src,lib}/**", "bin/a.c", False),
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file12.txt", False),
        ("[ab].md", "a.md", True),
        ("[ab].md", "c.md", False),
        ("[!ab].md", "c.md", True),
        ("*.PY", "a.py", False),                          # case-sensitive
        ("**", "a", True),
        ("**", "a/b/c", True),
        ("src/../secret.md", "secret.md", False),         # no path trickery
        ("*.md", "../outside.md", False),
        ("*.md", "/abs/outside.md", False),
        ("", "a.md", False),
    ],
)
def test_glob_match_table(pattern, path, expected):
    assert glob_match(pattern, path) is expected


def test_a_pathological_pattern_is_fast():
    pattern = "/".join(["**"] * 12 + ["a"])
    path = "/".join(["seg"] * 200 + ["b"])
    t0 = time.perf_counter()
    assert glob_match(pattern, path) is False
    assert time.perf_counter() - t0 < 1.0


def test_a_pathological_segment_pattern_is_fast():
    t0 = time.perf_counter()
    assert glob_match("*a*a*a*a*a*a*a*a*a*a*b", "a" * 200) is False
    assert time.perf_counter() - t0 < 1.0


# ---------------------------------------------------------------------------
# selecting the rules that apply
# ---------------------------------------------------------------------------
def test_select_rules_keeps_unconditional_rules_and_matching_scoped_ones(world):
    put(rules_dir(world) / "always.md", "ALWAYS")
    put(rules_dir(world) / "py.md", "---\npaths: ['**/*.py']\n---\nPY")
    put(rules_dir(world) / "js.md", "---\npaths: ['**/*.js']\n---\nJS")
    rules = load_rules(world.root, world.home).rules
    chosen = select_rules(rules, ["src/app.py"], world.root)
    assert [r.id for r in chosen] == ["project/always", "project/py"]


def test_with_no_files_in_play_only_unconditional_rules_apply(world):
    put(rules_dir(world) / "always.md", "ALWAYS")
    put(rules_dir(world) / "py.md", "---\npaths: ['**/*.py']\n---\nPY")
    rules = load_rules(world.root, world.home).rules
    assert [r.id for r in select_rules(rules, [], world.root)] == ["project/always"]


def test_a_rule_applies_when_any_one_of_its_globs_matches_any_one_file(world):
    put(rules_dir(world) / "multi.md", "---\npaths: ['docs/**', '*.toml']\n---\nMULTI")
    rules = load_rules(world.root, world.home).rules
    assert select_rules(rules, ["x/y.rs", "pyproject.toml"], world.root)
    assert not select_rules(rules, ["x/y.rs", "src/z.py"], world.root)


def test_absolute_paths_inside_the_root_are_made_relative_and_others_never_match(world):
    put(rules_dir(world) / "all-md.md", "---\npaths: ['**/*.md']\n---\nMD")
    rules = load_rules(world.root, world.home).rules
    assert select_rules(rules, [str(world.root / "docs" / "a.md")], world.root)
    assert not select_rules(rules, [str(world.outside / "a.md")], world.root)
    assert not select_rules(rules, ["../outside/a.md"], world.root)


def test_the_wire_shape_has_exactly_name_and_text(world):
    put(rules_dir(world) / "py.md", "---\npaths: ['**/*.py']\n---\nPY-TEXT")
    put(world.home / "rules" / "g.md", "G-TEXT")
    rules = load_rules(world.root, world.home).rules
    wire = rules_for_run_start(rules)
    assert wire == [{"name": "global/g", "text": "G-TEXT"}, {"name": "project/py", "text": "PY-TEXT"}]
    assert all(set(item) == {"name", "text"} for item in wire)


# ---------------------------------------------------------------------------
# size caps
# ---------------------------------------------------------------------------
def test_an_oversize_rule_is_truncated_with_a_marker(world):
    put(rules_dir(world) / "big.md", "\n".join(f"rule line {i:04d}" for i in range(2000)))
    result = load_rules(world.root, world.home, max_rule_bytes=1024)
    rule = result.rules[0]
    assert "truncated" in note_codes(result) and "<!-- m2c: truncated" in rule.text
    assert len(rule.text.encode()) < 1024 + 200 and "rule line 1999" not in rule.text


def test_the_total_cap_drops_global_rules_before_project_rules(world):
    for i in range(3):
        put(world.home / "rules" / f"g{i}.md", f"G{i} " + "g" * 300)
        put(rules_dir(world) / f"p{i}.md", f"P{i} " + "p" * 300)
    result = load_rules(world.root, world.home, max_total_bytes=1100)
    assert sum(len(r.text.encode()) for r in result.rules) <= 1100
    assert [r.id for r in result.rules][-3:] == ["project/p0", "project/p1", "project/p2"]
    assert "total_cap" in note_codes(result)


def test_at_most_64_rules_go_to_the_server(world):
    for i in range(70):
        put(rules_dir(world) / f"r{i:02d}.md", f"R{i}")
    result = load_rules(world.root, world.home)
    assert len(result.rules) == 64 == rl.MAX_RULES
    assert result.rules[-1].id == "project/r69" and "total_cap" in note_codes(result)


def test_default_caps_are_sane():
    assert rl.MAX_RULE_BYTES <= 32 * 1024 and rl.MAX_RULES_TOTAL_BYTES <= 200_000


# ---------------------------------------------------------------------------
# containment
# ---------------------------------------------------------------------------
def test_a_rule_file_that_is_a_symlink_out_of_the_repository_is_skipped(world):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    rules_dir(world).mkdir(parents=True)
    (rules_dir(world) / "leak.md").symlink_to(world.outside / "private.md")
    put(rules_dir(world) / "ok.md", "OK")
    result = load_rules(world.root, world.home)
    assert [r.id for r in result.rules] == ["project/ok"]
    assert "symlink_escape" in note_codes(result)


def test_a_rules_directory_that_is_a_symlink_out_of_the_repository_is_skipped(world):
    put(world.outside / "rules" / "leak.md", "OUTSIDE-SECRET")
    (world.root / ".m2c").mkdir()
    (world.root / ".m2c" / "rules").symlink_to(world.outside / "rules", target_is_directory=True)
    result = load_rules(world.root, world.home)
    assert result.rules == () and "symlink_escape" in note_codes(result)


def test_a_symlinked_rule_inside_the_repository_is_followed(world):
    put(world.root / "docs" / "style.md", "STYLE")
    rules_dir(world).mkdir(parents=True)
    (rules_dir(world) / "alias.md").symlink_to(world.root / "docs" / "style.md")
    assert [r.text for r in load_rules(world.root, world.home).rules] == ["STYLE"]


def test_notes_and_ids_never_contain_absolute_paths(world, tmp_path):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    rules_dir(world).mkdir(parents=True)
    (rules_dir(world) / "leak.md").symlink_to(world.outside / "private.md")
    put(rules_dir(world) / "bad.md", "---\npaths: [x\n---\nB")
    result = load_rules(world.root, world.home)
    blob = "\n".join(f"{n.code} {n.source} {n.message}" for n in result.notes)
    assert str(tmp_path) not in blob
