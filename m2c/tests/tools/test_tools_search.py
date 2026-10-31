"""
m2c/tests/tools/test_tools_search.py -- Patch C05: glob and grep
(m2c.tools.search).

glob is pure Python and is tested directly.  grep is a ripgrep wrapper, so it
is tested at three levels, because CI's m2c job has no ripgrep installed
(.github/workflows/ci.yml says so) and the security properties must not depend
on it being there:

1. ``build_rg_argv`` as a pure function: whatever text the model supplies, the
   pattern is the one token after ``-e``, the path is the one token after
   ``--``, and every other token comes from a fixed allowlist.  No ``--pre``,
   no ``--pre-glob``, no ``-f``, no shell.
2. A *fake* ``rg`` (a small Python script) that records the argument list,
   working directory and environment it was started with and prints crafted
   output: this proves the wrapper's post-filtering of protected files, its
   output caps, timeout, process cleanup and error handling without a real
   ripgrep.
3. The *real* ``rg``, skipped loudly when it is missing: flag-shaped patterns
   are searched as text and nothing is executed, ignore files and hidden files
   behave, symlinks are not followed.

Run: cd m2c && pytest tests/tools/test_tools_search.py -v
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from tools_fixtures import (
    SECRET,
    call,
    fails,
    fake_rg_seen,
    make_fake_rg,
    needs_rg,
    ok,
    plant_protected,
    write,
)

from m2c.tools import Caps, ToolContext
from m2c.tools import search as tool_search


# ==========================================================================
# glob
# ==========================================================================
@pytest.fixture
def tree(proj):
    for rel in (
        "a.py", "b.txt", "src/app.py", "src/util.py", "src/pkg/mod.py", "src/pkg/deep/x.py",
        "docs/readme.md", ".github/workflows/ci.yml", ".gitignore", "Z.py",
    ):
        write(proj, rel)
    (proj / "emptydir").mkdir()
    return proj


def names(result):
    return [ln for ln in result.output.splitlines() if ln and not ln.startswith("[")]


def test_glob_returns_sorted_files_only(ctx, tree):
    got = names(call(ctx, "glob", pattern="**/*.py"))
    assert got == ["Z.py", "a.py", "src/app.py", "src/pkg/deep/x.py", "src/pkg/mod.py", "src/util.py"]
    assert "emptydir" not in "".join(got)


def test_glob_is_deterministic(ctx, tree):
    assert call(ctx, "glob", pattern="**/*").output == call(ctx, "glob", pattern="**/*").output


def test_glob_without_a_slash_matches_the_top_level_only(ctx, tree):
    assert names(call(ctx, "glob", pattern="*.py")) == ["Z.py", "a.py"]


@pytest.mark.parametrize(
    "pattern,expected",
    [
        ("src/*.py", ["src/app.py", "src/util.py"]),
        ("src/**/*.py", ["src/app.py", "src/pkg/deep/x.py", "src/pkg/mod.py", "src/util.py"]),
        ("**/deep/*.py", ["src/pkg/deep/x.py"]),
        ("src/pkg/**", ["src/pkg/deep/x.py", "src/pkg/mod.py"]),
        ("src/ap?.py", ["src/app.py"]),
        ("src/[au]*.py", ["src/app.py", "src/util.py"]),
        ("src/[!a]*.py", ["src/util.py"]),
        ("docs/readme.md", ["docs/readme.md"]),
        ("**/ci.yml", [".github/workflows/ci.yml"]),
    ],
)
def test_glob_patterns(ctx, tree, pattern, expected):
    assert names(call(ctx, "glob", pattern=pattern)) == expected


def test_glob_star_matches_dotfiles(ctx, tree):
    assert ".gitignore" in names(call(ctx, "glob", pattern="*"))
    assert names(call(ctx, "glob", pattern=".github/**")) == [".github/workflows/ci.yml"]


def test_glob_with_a_base_path_is_relative_to_it(ctx, tree):
    assert names(call(ctx, "glob", pattern="*.py", path="src")) == ["src/app.py", "src/util.py"]


def test_glob_no_match_says_so_and_hints_at_the_top_level_rule(ctx, tree):
    result = call(ctx, "glob", pattern="*.rs")
    assert result.ok and "no files matched" in result.output
    assert "**/" in result.output
    assert "hint" not in call(ctx, "glob", pattern="**/*.rs").output


def test_glob_cap_with_marker(ctx, tree):
    result = call(ctx, "glob", pattern="**/*.py", max_results=3)
    assert result.ok and result.truncated
    assert names(result) == ["Z.py", "a.py", "src/app.py"]  # the first three in sorted order
    assert "truncated" in result.output and "3" in result.output


def test_glob_walk_cap(proj):
    context = ToolContext(proj, caps=Caps(glob_max_walk_entries=10))
    for i in range(100):
        write(proj, f"f{i:03d}.txt")
    result = call(context, "glob", pattern="**/*.nomatch")
    assert result.ok and result.truncated and "stopped" in result.output


def test_glob_time_limit(proj):
    context = ToolContext(proj, caps=Caps(glob_timeout_s=1e-9))
    for i in range(50):
        write(proj, f"d{i}/f.txt")
    result = call(context, "glob", pattern="**/*.nomatch")
    assert result.ok and result.truncated and "time limit" in result.output


def test_glob_skips_git_protected_and_unsafe_symlinks(ctx, proj, outside):
    plant_protected(proj)
    write(proj, ".git/config", SECRET)
    write(proj, "ok.txt")
    (proj / "leak.txt").symlink_to(outside / "secret.txt")
    (proj / "dirlink").symlink_to(outside)
    (proj / "toenv.txt").symlink_to(".env")
    (proj / "dangling.txt").symlink_to("nowhere")
    got = names(call(ctx, "glob", pattern="**/*"))
    assert "ok.txt" in got and "README.md" in got
    joined = "\n".join(got)
    for bad in (".git/", ".env", "id_rsa", ".ssh", ".aws", ".pem", ".key", ".p12", ".netrc", "history",
                ".kube", ".docker", "gcloud", "leak.txt", "dirlink", "toenv.txt", "dangling.txt", "secret.txt"):
        assert bad not in joined, bad


def test_glob_includes_a_symlink_to_a_safe_file(ctx, proj):
    write(proj, "real.txt")
    (proj / "alias.txt").symlink_to("real.txt")
    assert names(call(ctx, "glob", pattern="*.txt")) == ["alias.txt", "real.txt"]


def test_glob_does_not_descend_symlinked_directories_even_inside(ctx, proj):
    write(proj, "real/inner.txt")
    (proj / "alias").symlink_to("real")
    assert names(call(ctx, "glob", pattern="**/*.txt")) == ["real/inner.txt"]


def test_glob_user_denylist(proj):
    context = ToolContext(proj, user_denylist=["*.secret", "/private/"])
    for rel in ("a.secret", "d/b.secret", "private/x.txt", "pub.txt"):
        write(proj, rel)
    assert names(call(context, "glob", pattern="**/*")) == ["pub.txt"]


@pytest.mark.parametrize("pattern", ["", "a\0b", "/etc/*", "../*", "a/../b", "x/..", "y" * 600])
def test_glob_rejects_bad_patterns(ctx, pattern):
    assert call(ctx, "glob", pattern=pattern).code == "invalid_args"


def test_glob_base_path_errors(ctx, proj, outside):
    write(proj, "f.txt")
    (proj / "esc").symlink_to(outside)
    plant_protected(proj)
    fails(call(ctx, "glob", pattern="*", path="../outside"), "outside_root")
    fails(call(ctx, "glob", pattern="*", path="esc"), "outside_root")
    fails(call(ctx, "glob", pattern="*", path="missing"), "not_found")
    fails(call(ctx, "glob", pattern="*", path="f.txt"), "not_a_directory")
    fails(call(ctx, "glob", pattern="*", path=".ssh"), "protected_path")


# ==========================================================================
# grep: the argument list (pure)
# ==========================================================================
ALLOWED_EXACT = {
    "--no-config", "--null", "--line-number", "--with-filename", "--no-heading", "--color=never",
    "--hidden", "--no-follow", "--sort=path", "--max-columns=500", "--max-columns-preview",
    "--max-filesize=20M", "--ignore-case", "--case-sensitive", "-e", "--",
}
ALLOWED_PREFIXES = ("--max-count=", "--context=", "--glob=")
FORBIDDEN = ("--pre", "--pre-glob", "-f", "--file", "--files", "--hostname-bin", "--search-zip", "-z",
             "--follow", "-L", "--ignore-file", "--config-path", "--no-ignore", "-u")

HOSTILE_PATTERNS = [
    "--pre=cmd", "--pre", "--pre-glob=*", "-f /etc/passwd", "-f", "--file=/etc/passwd", "--files", "-e", "--",
    "-", "--hostname-bin=x", "-z", "--search-zip", "--follow", "\n--pre=x", "$(touch pwned)", "; rm -rf /",
    "`id`", "-uuu", "--ignore-file=/etc/passwd", "--config-path", "--no-config", "-e -e", "--glob=!*",
    "ünïcode", "a" * 1000,
]


def _build(pattern="needle", search="./src", **kw):
    args = dict(pattern=pattern, search_arg=search, ignore_case=False, context_lines=0, glob=None, max_matches=200)
    args.update(kw)
    return tool_search.build_rg_argv("/usr/bin/rg", **args)


def _assert_shape(argv, pattern, search_arg):
    assert argv[0] == "/usr/bin/rg"
    i = argv.index("-e")  # the first -e is the flag: everything before it is fixed text
    assert argv[i + 1] == pattern
    assert argv[i + 2] == "--"
    assert argv[i + 3] == search_arg and i + 4 == len(argv)
    for tok in argv[1:i]:
        assert tok in ALLOWED_EXACT or tok.startswith(ALLOWED_PREFIXES), tok
        assert tok not in FORBIDDEN
        assert not tok.startswith(("--pre", "--hostname", "--file", "--ignore-file", "--config-path"))


@pytest.mark.parametrize("pattern", HOSTILE_PATTERNS)
def test_hostile_patterns_are_only_ever_the_token_after_dash_e(pattern):
    _assert_shape(_build(pattern=pattern), pattern, "./src")


@pytest.mark.parametrize("search", ["-rf", "--pre=x", ".", "./dir with space", "./-x", "./a:b"])
def test_the_path_is_only_ever_the_token_after_double_dash(search):
    _assert_shape(_build(search=search), "needle", search)


@pytest.mark.parametrize("glob", ["--pre=cmd", "-f", "*.py", "!x", "--files", "a b", "-e"])
def test_the_glob_filter_is_glued_to_its_flag(glob):
    argv = _build(glob=glob)
    assert argv.count(f"--glob={glob}") >= 1
    _assert_shape(argv, "needle", "./src")
    # the glob is never a token of its own (the one bare "-e" in the list is the pattern flag)
    assert argv.count(glob) == (1 if glob == "-e" else 0)


def test_flags_follow_the_validated_parameters():
    argv = _build(ignore_case=True, context_lines=3, max_matches=7)
    assert "--ignore-case" in argv and "--case-sensitive" not in argv
    assert "--context=3" in argv and "--max-count=7" in argv
    argv = _build()
    assert "--case-sensitive" in argv and "--ignore-case" not in argv
    assert not any(t.startswith("--context") for t in argv)


def test_fixed_safety_flags_are_always_present():
    argv = _build()
    for flag in ("--no-config", "--no-follow", "--null", "--hidden", "--color=never", "--glob=!.git"):
        assert flag in argv


def test_exclusions_for_the_hard_list_come_after_the_models_glob():
    argv = _build(glob="*")
    model = argv.index("--glob=*")
    excludes = [i for i, t in enumerate(argv) if t.startswith("--glob=!")]
    assert excludes and all(i > model for i in excludes)
    for needle in ("--glob=!.env", "--glob=!.env.*", "--glob=!id_rsa*", "--glob=!*.pem", "--glob=!.ssh"):
        assert needle in argv


def test_builder_refuses_what_validation_should_already_have_caught():
    for bad in ("", "a\0b"):
        with pytest.raises(ValueError):
            _build(pattern=bad)
    with pytest.raises(ValueError):
        _build(search="a\0b")
    with pytest.raises(ValueError):
        _build(context_lines=-1)
    with pytest.raises(ValueError):
        _build(context_lines=True)
    with pytest.raises(ValueError):
        _build(max_matches=0)
    with pytest.raises(ValueError):
        _build(glob="a\0b")


def test_no_shell_is_used_anywhere_in_the_search_module():
    src = Path(tool_search.__file__).read_text()
    assert "shell=True" not in src and "os.system" not in src and "os.popen" not in src


# ==========================================================================
# grep: with a fake rg
# ==========================================================================
@pytest.fixture
def fake(tmp_path):
    """Factory: (body) -> ToolContext whose rg is a fake script with that body."""
    counter = {"n": 0}

    def make(proj, body, **caps):
        counter["n"] += 1
        bindir = tmp_path / f"fakebin{counter['n']}"
        script = make_fake_rg(bindir, body)
        context = ToolContext(proj, caps=Caps(**caps) if caps else None, rg_path=str(script))
        return context, bindir

    return make


def test_rg_is_started_with_a_fixed_argument_list_a_clean_environment_and_the_root_as_cwd(proj, fake, monkeypatch):
    monkeypatch.setenv("RIPGREP_CONFIG_PATH", "/tmp/evil-config")
    monkeypatch.setenv("SOME_API_KEY", "x" * 40)
    context, bindir = fake(proj, "sys.exit(1)\n")
    write(proj, "src/a.txt")
    result = call(context, "grep", pattern="--pre=cmd", path="src", glob="--pre=x", case_insensitive=True, context_lines=2)
    assert result.ok and "no matches" in result.output
    seen = fake_rg_seen(bindir)
    argv = ["/usr/bin/rg"] + seen["argv"]
    _assert_shape(argv, "--pre=cmd", "./src")
    assert "--glob=--pre=x" in argv and "--ignore-case" in argv and "--context=2" in argv
    assert seen["cwd"] == str(proj)
    assert "RIPGREP_CONFIG_PATH" not in seen["env"]
    assert "SOME_API_KEY" not in seen["env"]
    assert "M2C_HOME" not in seen["env"] and "HOME" not in seen["env"]


def test_protected_files_are_dropped_from_the_results_even_if_rg_returns_them(proj, fake):
    body = r"""
out = sys.stdout.buffer
out.write(b"./a.txt\x001:SECRETWORD a\n")
out.write(b"./.env\x001:SECRETWORD env\n")
out.write(b"./.env\x002-context from env\n")
out.write(b"--\n")
out.write(b"./sub/id_rsa\x005:SECRETWORD key\n")
out.write(b"--\n")
out.write(b"./.aws/credentials\x001:SECRETWORD aws\n")
out.write(b"./sub/x.txt\x003:SECRETWORD x\n")
out.write(b"./.ssh/config\x001:SECRETWORD ssh\n")
out.write(b"./notes.txt\x004:last\n")
"""
    context, _ = fake(proj, body)
    write(proj, "a.txt")
    result = call(context, "grep", pattern="SECRETWORD")
    assert result.ok
    assert result.output.splitlines() == [
        "a.txt:1:SECRETWORD a",
        "--",
        "sub/x.txt:3:SECRETWORD x",
        "notes.txt:4:last",
    ]
    for leaked in ("env", "id_rsa", "aws", "ssh", "context from"):
        assert leaked not in result.output


def test_user_denylist_is_applied_to_grep_results(proj, tmp_path):
    bindir = tmp_path / "ub"
    script = make_fake_rg(bindir, 'sys.stdout.buffer.write(b"./a.secret\\x001:hit\\n./b.txt\\x001:hit\\n")\n')
    context = ToolContext(proj, user_denylist=["*.secret"], rg_path=str(script))
    result = call(context, "grep", pattern="hit")
    assert result.output.splitlines() == ["b.txt:1:hit"]


def test_context_lines_are_reformatted_with_a_dash(proj, fake):
    body = 'sys.stdout.buffer.write(b"./a.txt\\x001-before\\n./a.txt\\x002:match\\n./a.txt\\x003-after\\n")\n'
    context, _ = fake(proj, body)
    assert call(context, "grep", pattern="match", context_lines=1).output.splitlines() == [
        "a.txt-1-before", "a.txt:2:match", "a.txt-3-after",
    ]


def test_match_text_containing_colons_dashes_and_nul_free_binaryish_bytes(proj, fake):
    body = 'sys.stdout.buffer.write(b"./a-b:c.txt\\x0012:x:y-z\\n./bad.txt\\x001:caf\\xe9\\n")\n'
    context, _ = fake(proj, body)
    out = ok(call(context, "grep", pattern="x"))
    assert "a-b:c.txt:12:x:y-z" in out and "bad.txt:1:caf" in out


def test_a_line_without_a_nul_that_is_not_a_separator_is_ignored(proj, fake):
    body = 'sys.stdout.buffer.write(b"garbage line\\n./a.txt\\x001:real\\n")\n'
    context, _ = fake(proj, body)
    assert call(context, "grep", pattern="x").output.splitlines() == ["a.txt:1:real"]


def test_output_is_capped_by_match_count_with_a_marker(proj, fake):
    body = 'for i in range(1, 500): sys.stdout.buffer.write(b"./a.txt\\x00%d:line %d\\n" % (i, i))\n'
    context, _ = fake(proj, body)
    result = call(context, "grep", pattern="line", max_matches=5)
    assert result.ok and result.truncated
    lines = result.output.splitlines()
    assert lines[:5] == [f"a.txt:{i}:line {i}" for i in range(1, 6)]
    assert "truncated" in lines[-1] and "5" in lines[-1]
    assert len(lines) == 6


def test_output_is_capped_by_bytes_with_a_marker(proj, fake):
    body = 'for i in range(1, 500): sys.stdout.buffer.write(b"./a.txt\\x00%d:" % i + b"y" * 100 + b"\\n")\n'
    context, _ = fake(proj, body, grep_output_bytes=1000)
    result = call(context, "grep", pattern="y", max_matches=1000)
    assert result.ok and result.truncated
    assert len(result.output.encode()) < 1400
    assert "truncated" in result.output.splitlines()[-1]


def test_runaway_output_stops_the_process(proj, fake, tmp_path):
    body = (
        'open(%r, "w").write(str(os.getpid()))\n'
        "while True:\n"
        '    sys.stdout.buffer.write(b"./a.txt\\x001:" + b"x" * 1000 + b"\\n")\n'
        "    sys.stdout.buffer.flush()\n"
    ) % str(tmp_path / "pid.txt")
    context, _ = fake(proj, body, grep_max_raw_bytes=20_000, grep_timeout_s=30)
    started = time.monotonic()
    result = call(context, "grep", pattern="x", max_matches=1000)
    assert time.monotonic() - started < 15
    assert result.ok and result.truncated
    _assert_process_gone(int((tmp_path / "pid.txt").read_text()))


def test_timeout_kills_rg_and_reports_an_error_when_nothing_was_found(proj, fake, tmp_path):
    body = 'open(%r, "w").write(str(os.getpid()))\nimport time\ntime.sleep(60)\n' % str(tmp_path / "pid.txt")
    context, _ = fake(proj, body, grep_timeout_s=0.5)
    started = time.monotonic()
    result = call(context, "grep", pattern="x")
    assert time.monotonic() - started < 15
    fails(result, "timeout")
    _assert_process_gone(int((tmp_path / "pid.txt").read_text()))


def test_timeout_with_partial_results_returns_them_marked_truncated(proj, fake):
    body = (
        'sys.stdout.buffer.write(b"./a.txt\\x001:early\\n")\n'
        "sys.stdout.buffer.flush()\n"
        "import time\ntime.sleep(60)\n"
    )
    context, _ = fake(proj, body, grep_timeout_s=0.5)
    result = call(context, "grep", pattern="x")
    assert result.ok and result.truncated
    assert "a.txt:1:early" in result.output and "time limit" in result.output


def test_a_search_failure_reports_the_first_lines_of_stderr(proj, fake):
    context, _ = fake(proj, 'sys.stderr.write("rg: regex parse error:\\n    (?:()\\nerror: unclosed group\\n")\nsys.exit(2)\n')
    result = fails(call(context, "grep", pattern="("), "search_failed")
    assert "regex parse error" in result.output


def test_exit_code_two_with_matches_still_returns_them(proj, fake):
    body = 'sys.stdout.buffer.write(b"./a.txt\\x001:found\\n")\nsys.stderr.write("rg: some/dir: Permission denied\\n")\nsys.exit(2)\n'
    context, _ = fake(proj, body)
    result = call(context, "grep", pattern="found")
    assert result.ok and "a.txt:1:found" in result.output
    assert "could not be searched" in result.output


def test_no_matches_is_not_an_error(proj, fake):
    context, _ = fake(proj, "sys.exit(1)\n")
    result = call(context, "grep", pattern="nothing")
    assert result.ok and "no matches" in result.output and result.truncated is False


def test_all_results_protected_reads_as_no_matches(proj, fake):
    context, _ = fake(proj, 'sys.stdout.buffer.write(b"./.env\\x001:SECRETWORD\\n")\n')
    result = call(context, "grep", pattern="SECRETWORD")
    assert result.ok and "no matches" in result.output and SECRET not in result.output


def test_rg_missing(proj, monkeypatch, tmp_path):
    empty = tmp_path / "emptybin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    fails(call(ToolContext(proj), "grep", pattern="x"), "rg_not_found")
    fails(call(ToolContext(proj, rg_path=str(tmp_path / "nope" / "rg")), "grep", pattern="x"), "rg_not_found")


def test_an_rg_binary_inside_the_project_is_never_run(proj, tmp_path):
    script = make_fake_rg(proj / "bin", 'sys.stdout.buffer.write(b"./a.txt\\x001:pwned\\n")\n')
    result = fails(call(ToolContext(proj, rg_path=str(script)), "grep", pattern="x"), "rg_not_found")
    assert "project" in result.output
    assert not (proj / "bin" / "seen.json").exists()


def test_an_rg_that_is_not_executable_is_reported(proj, tmp_path):
    script = tmp_path / "plain-file"
    script.write_text("not a program")
    script.chmod(0o644)
    assert not call(ToolContext(proj, rg_path=str(script)), "grep", pattern="x").ok


def test_grep_path_handling_with_a_fake_rg(proj, fake, outside):
    context, bindir = fake(proj, "sys.exit(1)\n")
    write(proj, "f.txt")
    (proj / "esc").symlink_to(outside)
    plant_protected(proj)
    fails(call(context, "grep", pattern="x", path="../outside"), "outside_root")
    fails(call(context, "grep", pattern="x", path="esc"), "outside_root")
    fails(call(context, "grep", pattern="x", path="missing"), "not_found")
    fails(call(context, "grep", pattern="x", path=".ssh"), "protected_path")
    fails(call(context, "grep", pattern="x", path=".env"), "protected_path")
    assert not (bindir / "seen.json").exists(), "rg must not even start for a refused path"
    ok(call(context, "grep", pattern="x", path="f.txt"))
    assert fake_rg_seen(bindir)["argv"][-1] == "./f.txt"


@pytest.mark.parametrize(
    "args",
    [
        {"pattern": ""},
        {"pattern": "a\0b"},
        {"pattern": "x" * 5000},
        {"pattern": "x", "glob": ""},
        {"pattern": "x", "glob": "a\0b"},
        {"pattern": "x", "glob": "g" * 600},
    ],
)
def test_grep_rejects_bad_parameters_before_starting_rg(proj, fake, args):
    context, bindir = fake(proj, "sys.exit(1)\n")
    assert call(context, "grep", **args).code == "invalid_args"
    assert not (bindir / "seen.json").exists()


def _assert_process_gone(pid: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        # a zombie that nobody waited for still answers kill(0); check its state
        try:
            state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            return
        if state == "Z":
            return
        time.sleep(0.05)
    pytest.fail(f"process {pid} is still running after the search was cut off")


# ==========================================================================
# grep: the real ripgrep
# ==========================================================================
@pytest.fixture
def rgtree(proj, outside):
    write(proj, "a.txt", f"alpha {SECRET}\nbeta\ngamma {SECRET}\n")
    write(proj, "sub/c.txt", f"{SECRET} in sub\n")
    write(proj, ".github/workflows/ci.yml", f"{SECRET} ci\n")
    write(proj, "b.txt", "x --pre=cmd y\n")
    write(proj, "needs-flag.txt", "this line has --pre=./pre.sh in it\n")
    pre = write(proj, "pre.sh", "#!/bin/sh\ntouch pwned\ncat\n")
    pre.chmod(0o755)
    plant_protected(proj)
    write(proj, ".git/config", f"{SECRET} git\n")
    (proj / "data.bin").write_bytes(f"{SECRET}\0binary".encode())
    (proj / "link").symlink_to(outside)
    return proj


@needs_rg
def test_real_rg_basic_search_and_filtering(rgtree):
    context = ToolContext(rgtree)
    result = call(context, "grep", pattern=SECRET)
    out = ok(result)
    lines = out.splitlines()
    assert "a.txt:1:alpha SECRETWORD" in lines and "a.txt:3:gamma SECRETWORD" in lines
    assert "sub/c.txt:1:SECRETWORD in sub" in lines
    assert ".github/workflows/ci.yml:1:SECRETWORD ci" in lines  # hidden directories are searched
    assert "README.md:1:SECRETWORD is also in the readme" in lines
    for ln in lines:
        path = ln.split(":")[0]
        assert not path.startswith(".git/"), ln
    for bad in (".env", "id_rsa", ".ssh", ".aws", ".pem", ".key", ".p12", ".netrc", "history", ".kube",
                ".docker", "gcloud", "data.bin", "secret.txt", "outside"):
        assert bad not in out, bad
    assert "./" not in out


@needs_rg
@pytest.mark.parametrize("pattern", ["--pre=./pre.sh", "--pre", "-f /etc/passwd", "--files", "-e", "--", "--follow"])
def test_real_rg_treats_flag_shaped_patterns_as_text_and_runs_nothing(rgtree, pattern):
    result = call(ToolContext(rgtree), "grep", pattern=pattern)
    assert result.ok or result.code == "search_failed"  # e.g. "--" alone is a fine regex; none may execute
    assert not (rgtree / "pwned").exists(), "a model-supplied pattern was run as a rg flag"


@needs_rg
def test_real_rg_finds_a_flag_shaped_pattern_in_a_file_as_plain_text(rgtree):
    out = ok(call(ToolContext(rgtree), "grep", pattern="--pre=./pre.sh", path="needs-flag.txt"))
    assert out == "needs-flag.txt:1:this line has --pre=./pre.sh in it"
    assert not (rgtree / "pwned").exists()


@needs_rg
def test_real_rg_a_filter_glob_cannot_bring_protected_files_back(rgtree):
    context = ToolContext(rgtree)
    for glob in ("*", ".env", ".env*", "id_rsa", "**/*", "!a.txt", "*.pem"):
        out = ok(call(context, "grep", pattern=SECRET, glob=glob))
        assert ".env" not in out and "id_rsa" not in out and ".pem" not in out, glob
    only = ok(call(context, "grep", pattern=SECRET, glob="*.yml"))
    assert only == ".github/workflows/ci.yml:1:SECRETWORD ci"


@needs_rg
def test_real_rg_a_glob_that_looks_like_a_flag_is_just_a_glob(rgtree):
    result = call(ToolContext(rgtree), "grep", pattern=SECRET, glob="--pre=./pre.sh")
    assert result.ok and not (rgtree / "pwned").exists()


@needs_rg
def test_real_rg_context_case_and_scoping(rgtree):
    context = ToolContext(rgtree)
    out = ok(call(context, "grep", pattern="beta", context_lines=1, path="a.txt"))
    assert out.splitlines() == [f"a.txt-1-alpha {SECRET}", "a.txt:2:beta", f"a.txt-3-gamma {SECRET}"]
    assert "no matches" in ok(call(context, "grep", pattern="secretword", path="a.txt"))
    assert "a.txt:1:" in ok(call(context, "grep", pattern="secretword", path="a.txt", case_insensitive=True))
    assert ok(call(context, "grep", pattern=SECRET, path="sub")) == "sub/c.txt:1:SECRETWORD in sub"


@needs_rg
def test_real_rg_max_matches(rgtree):
    result = call(ToolContext(rgtree), "grep", pattern=SECRET, max_matches=1)
    assert result.ok and result.truncated
    assert sum(1 for ln in result.output.splitlines() if not ln.startswith("[")) == 1


@needs_rg
def test_real_rg_does_not_follow_symlinks(rgtree, outside):
    out = ok(call(ToolContext(rgtree), "grep", pattern="outside"))
    assert "secret.txt" not in out and "outside" not in out.replace("[no matches]", "")


@needs_rg
def test_real_rg_invalid_regex(rgtree):
    result = fails(call(ToolContext(rgtree), "grep", pattern="("), "search_failed")
    assert "regex" in result.output.lower()


@needs_rg
def test_real_rg_honours_gitignore_inside_a_repository(proj):
    os.mkdir(proj / ".git")  # rg only reads .gitignore inside a git repository
    write(proj, ".gitignore", "ignored.txt\n")
    write(proj, "ignored.txt", "needle\n")
    write(proj, "kept.txt", "needle\n")
    out = ok(call(ToolContext(proj), "grep", pattern="needle"))
    assert out == "kept.txt:1:needle"


@needs_rg
def test_real_rg_refused_paths_never_start_a_search(rgtree):
    context = ToolContext(rgtree)
    fails(call(context, "grep", pattern="x", path=".ssh"), "protected_path")
    fails(call(context, "grep", pattern="x", path="../x"), "outside_root")
    fails(call(context, "grep", pattern="x", path="link"), "outside_root")


@needs_rg
def test_real_rg_accepts_every_exclusion_glob_the_wrapper_generates(proj):
    """If any generated glob were invalid, rg would exit 2 for every search."""
    write(proj, "f.txt", "needle\n")
    assert ok(call(ToolContext(proj), "grep", pattern="needle")) == "f.txt:1:needle"
