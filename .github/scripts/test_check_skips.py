"""
.github/scripts/test_check_skips.py -- Patch Q01: tests for the skip gate.

check_skips.py is the CI step that turns "a test was skipped" from something a
person has to notice in a log into something that fails the job unless the
reason is on the allow list kept next to it (skip-allowlist.txt).  A gate that
can never fail is worse than no gate, so these tests pin the behaviour that
matters: an unlisted reason FAILS, and so does every input that would let a
problem pass quietly (a missing or unreadable report, a run in which no test
ran at all, an allow-list line the script cannot understand).

Standard library only, so it runs under the plain interpreter of any CI job:

    python -m pytest .github/scripts -q          (from the repository root)

Nothing here builds a secret-shaped string; every value is plain prose.
"""
from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("check_skips", HERE / "check_skips.py")
check_skips = importlib.util.module_from_spec(_spec)
sys.modules["check_skips"] = check_skips
_spec.loader.exec_module(check_skips)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def junit(tmp_path: Path, cases: list[tuple[str, str | None, str]], name: str = "report.xml") -> Path:
    """Write a JUnit report shaped like pytest's.

    ``cases`` is [(test name, skip reason or None, skip type)]; a reason of
    None means the test passed.  Skip type is pytest's own: "pytest.skip" for a
    real skip, "pytest.xfail" for an expected failure (which pytest also writes
    as a <skipped> element).
    """
    rows = []
    for test, reason, kind in cases:
        if reason is None:
            rows.append(f'<testcase classname="t" name="{test}"/>')
        else:
            rows.append(
                f'<testcase classname="t" name="{test}">'
                f'<skipped type="{kind}" message="{reason}">tests/x.py:1: {reason}</skipped>'
                f"</testcase>"
            )
    path = tmp_path / name
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(rows)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return path


def allowlist(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "allow.txt"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def run(tmp_path, cases, allow_body, scope="m2c", env=None, capsys=None):
    report = junit(tmp_path, cases)
    allow = allowlist(tmp_path, allow_body)
    argv = ["--scope", scope, "--allowlist", str(allow), str(report)]
    code = check_skips.main(argv, environ=env or {})
    out = capsys.readouterr() if capsys is not None else None
    return code, out


# --------------------------------------------------------------------------
# the gate itself
# --------------------------------------------------------------------------
def test_a_run_with_no_skips_passes(tmp_path, capsys):
    code, out = run(tmp_path, [("a", None, ""), ("b", None, "")], "", capsys=capsys)
    assert code == 0
    assert "0 skipped" in out.out


def test_a_skip_that_is_not_on_the_list_fails_and_names_its_reason(tmp_path, capsys):
    cases = [("a", None, ""), ("b", "ripgrep (rg) is not installed", "pytest.skip")]
    code, out = run(tmp_path, cases, "", capsys=capsys)
    assert code == 1
    assert "ripgrep (rg) is not installed" in out.out
    assert "::error" in out.out  # a GitHub annotation, so it shows on the run summary


def test_a_listed_skip_passes_but_is_still_printed(tmp_path, capsys):
    code, out = run(
        tmp_path,
        [("a", None, ""), ("b", "bubblewrap user namespaces unavailable on the runner", "pytest.skip")],
        "m2c | bubblewrap user namespaces unavailable on the runner\n",
        capsys=capsys,
    )
    assert code == 0
    assert "bubblewrap user namespaces unavailable on the runner" in out.out
    assert "allowed" in out.out


def test_a_listed_reason_is_matched_as_a_substring_of_the_real_one(tmp_path, capsys):
    code, _ = run(
        tmp_path,
        [("a", "/usr/share/foo does not exist here", "pytest.skip")],
        "m2c | does not exist here\n",
        capsys=capsys,
    )
    assert code == 0


def test_one_unlisted_reason_among_listed_ones_still_fails(tmp_path, capsys):
    code, out = run(
        tmp_path,
        [("a", "listed reason", "pytest.skip"), ("b", "some new reason", "pytest.skip")],
        "m2c | listed reason\n",
        capsys=capsys,
    )
    assert code == 1
    assert "some new reason" in out.out


def test_an_expected_failure_is_not_a_skip(tmp_path, capsys):
    code, _ = run(tmp_path, [("a", None, ""), ("b", "known bug", "pytest.xfail")], "", capsys=capsys)
    assert code == 0


def test_several_skips_with_one_reason_are_counted_together(tmp_path, capsys):
    code, out = run(
        tmp_path,
        [("a", "same reason", "pytest.skip"), ("b", "same reason", "pytest.skip"), ("c", None, "")],
        "",
        capsys=capsys,
    )
    assert code == 1
    assert "2 x" in out.out or "2 tests" in out.out


# --------------------------------------------------------------------------
# scope and conditions
# --------------------------------------------------------------------------
def test_an_entry_for_another_scope_does_not_apply(tmp_path, capsys):
    code, _ = run(
        tmp_path,
        [("a", "needs a database", "pytest.skip")],
        "backend | needs a database\n",
        scope="m2c",
        capsys=capsys,
    )
    assert code == 1


def test_a_star_scope_applies_everywhere(tmp_path, capsys):
    cases = [("a", "everywhere reason", "pytest.skip")]
    code, _ = run(tmp_path, cases, "* | everywhere reason\n", scope="m2c", capsys=capsys)
    assert code == 0


def test_a_conditional_entry_is_ignored_when_its_variable_is_not_set(tmp_path, capsys):
    body = "m2c | no user namespaces | when=M2C_CI_NO_USERNS\n"
    code, _ = run(tmp_path, [("a", "no user namespaces", "pytest.skip")], body, env={}, capsys=capsys)
    assert code == 1


def test_a_conditional_entry_applies_when_its_variable_is_1(tmp_path, capsys):
    body = "m2c | no user namespaces | when=M2C_CI_NO_USERNS\n"
    code, _ = run(
        tmp_path, [("a", "no user namespaces", "pytest.skip")], body, env={"M2C_CI_NO_USERNS": "1"}, capsys=capsys
    )
    assert code == 0


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_a_conditional_entry_needs_exactly_1(tmp_path, capsys, value):
    body = "m2c | no user namespaces | when=M2C_CI_NO_USERNS\n"
    code, _ = run(
        tmp_path, [("a", "no user namespaces", "pytest.skip")], body, env={"M2C_CI_NO_USERNS": value}, capsys=capsys
    )
    assert code == 1


# --------------------------------------------------------------------------
# fail closed
# --------------------------------------------------------------------------
def test_a_missing_report_fails(tmp_path, capsys):
    allow = allowlist(tmp_path, "")
    code = check_skips.main(["--scope", "m2c", "--allowlist", str(allow), str(tmp_path / "nope.xml")], environ={})
    assert code == 2
    assert "nope.xml" in capsys.readouterr().out


def test_an_unreadable_report_fails(tmp_path, capsys):
    bad = tmp_path / "bad.xml"
    bad.write_text("this is not xml", encoding="utf-8")
    allow = allowlist(tmp_path, "")
    assert check_skips.main(["--scope", "m2c", "--allowlist", str(allow), str(bad)], environ={}) == 2


def test_a_run_in_which_no_test_ran_fails(tmp_path, capsys):
    code, out = run(tmp_path, [], "", capsys=capsys)
    assert code == 1
    assert "no tests" in out.out.lower()


def test_a_missing_allow_list_fails(tmp_path, capsys):
    report = junit(tmp_path, [("a", None, "")])
    code = check_skips.main(["--scope", "m2c", "--allowlist", str(tmp_path / "nope.txt"), str(report)], environ={})
    assert code == 2


@pytest.mark.parametrize(
    "line",
    [
        "just a reason with no scope",
        "m2c |",
        "m2c |    ",
        " | reason with no scope",
        "m2c | reason | when=",
        "m2c | reason | unless=SOMETHING",
        "m2c | reason | when=A | extra field",
    ],
)
def test_a_malformed_allow_list_line_fails_instead_of_being_ignored(tmp_path, capsys, line):
    report = junit(tmp_path, [("a", None, "")])
    allow = allowlist(tmp_path, line + "\n")
    assert check_skips.main(["--scope", "m2c", "--allowlist", str(allow), str(report)], environ={}) == 2


def test_comments_and_blank_lines_in_the_allow_list_are_ignored(tmp_path, capsys):
    body = """
        # a comment

        m2c | listed reason   # why it is fine
    """
    code, _ = run(tmp_path, [("a", "listed reason", "pytest.skip")], body, capsys=capsys)
    assert code == 0


def test_several_reports_are_read_together(tmp_path, capsys):
    one = junit(tmp_path, [("a", None, "")], name="one.xml")
    two = junit(tmp_path, [("b", "unlisted", "pytest.skip")], name="two.xml")
    allow = allowlist(tmp_path, "")
    code = check_skips.main(["--scope", "m2c", "--allowlist", str(allow), str(one), str(two)], environ={})
    assert code == 1


# --------------------------------------------------------------------------
# the real allow list
# --------------------------------------------------------------------------
def test_the_real_allow_list_parses_and_stays_short():
    entries = check_skips.load_allowlist(HERE / "skip-allowlist.txt")
    assert 1 <= len(entries) <= 6, "the allow list is meant to stay short; justify each entry in its comment"
    for entry in entries:
        assert entry.match.strip()
        assert entry.scope in {"m2c", "backend", "protocol", "*"}


def test_the_real_allow_list_does_not_allow_the_skips_this_patch_exists_to_remove():
    """ripgrep and jsonschema are installed by the job, so skipping for them is a failure."""
    entries = check_skips.load_allowlist(HERE / "skip-allowlist.txt")
    for entry in entries:
        for banned in ("ripgrep", "jsonschema", "protocol/requirements.txt"):
            assert banned not in entry.match
