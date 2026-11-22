"""
.github/scripts/test_check_audit.py -- Patch SX-09: tests for the dependency-audit gate.

check_audit.py turns "the audit found something" from a wall of text nobody reads
into a job that fails unless the finding is written down (with a reason and an end
date) in .github/audit-allowlist.txt.  A gate that can never fail is worse than
none, so these tests pin the cases that must FAIL: an unlisted finding, an expired
entry, a stale entry, a malformed allow-list line, and a report that cannot be read
(including the JSON npm writes when its own audit failed).  The cases that must
PASS are pinned too, so the gate cannot drift into crying wolf.

The reports below have the shape that pip-audit 2.10.1 and npm 10 really write
(checked on 2026-11-22).  No network, no real package names that matter, no secrets;
nothing here skips.

Standard library only (plus pytest), so it runs under the plain interpreter of any CI job:

    python -m pytest .github/scripts -q          (from the repository root)
    python .github/scripts/test_check_audit.py   (same tests, through pytest)
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("check_audit", HERE / "check_audit.py")
check_audit = importlib.util.module_from_spec(_spec)
sys.modules["check_audit"] = check_audit
_spec.loader.exec_module(check_audit)

TODAY = dt.date(2026, 11, 23)
FUTURE = "2026-12-31"


# --------------------------------------------------------------------------
# helpers: reports shaped like the real tools write them
# --------------------------------------------------------------------------
def pip_vuln(vuln_id: str, aliases=(), fixes=("9.9.9",)) -> dict:
    return {"id": vuln_id, "fix_versions": list(fixes), "aliases": list(aliases), "description": "text"}


def pip_report(*deps) -> dict:
    """deps: (name, version, [vulns]) tuples."""
    return {"dependencies": [{"name": n, "version": v, "vulns": vs} for n, v, vs in deps], "fixes": []}


def advisory(ghsa: str, severity: str, source: int = 1000) -> dict:
    return {
        "source": source,
        "name": "pkg",
        "dependency": "pkg",
        "title": "a title",
        "url": f"https://github.com/advisories/{ghsa}",
        "severity": severity,
        "range": "<2.0.0",
    }


def npm_report(**packages) -> dict:
    """packages: name=(package severity, [via entries])."""
    vulns = {
        name: {"name": name, "severity": sev, "isDirect": True, "via": via, "effects": [], "range": "*", "nodes": []}
        for name, (sev, via) in packages.items()
    }
    return {"auditReportVersion": 2, "vulnerabilities": vulns, "metadata": {"vulnerabilities": {"total": len(vulns)}}}


def write_json(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def allowlist(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "allow.txt"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def run(tmp_path, capsys, *, pip=None, frontend=None, promptfoo=None, allow="", env=None, today=TODAY):
    """Run the gate on in-memory reports; return (exit status, printed text)."""
    argv = ["--allowlist", str(allowlist(tmp_path, allow))]
    for flag, data, name in (("--pip", pip, "pip.json"), ("--frontend", frontend, "fe.json"), ("--promptfoo", promptfoo, "pr.json")):
        if data is not None:
            argv += [flag, str(write_json(tmp_path, name, data))]
    code = check_audit.main(argv, environ=env or {}, today=today)
    return code, capsys.readouterr().out


ONE_PIP = pip_report(("aaa", "1.0", [pip_vuln("PYSEC-2026-1", ["CVE-2026-1", "GHSA-aaaa-bbbb-cccc"])]))
CLEAN_PIP = pip_report(("aaa", "1.0", []), ("bbb", "2.0", []))
CLEAN_NPM = npm_report()


# --------------------------------------------------------------------------
# findings that must pass or fail
# --------------------------------------------------------------------------
def test_clean_reports_and_an_empty_allow_list_pass(tmp_path, capsys):
    code, out = run(tmp_path, capsys, pip=CLEAN_PIP, frontend=CLEAN_NPM, promptfoo=CLEAN_NPM)
    assert code == 0
    assert "OK" in out


def test_an_unlisted_finding_fails_and_names_package_version_and_ids(tmp_path, capsys):
    code, out = run(tmp_path, capsys, pip=ONE_PIP)
    assert code == 1
    assert "NOT ALLOWED [python]" in out
    for text in ("aaa 1.0", "PYSEC-2026-1", "CVE-2026-1", "GHSA-aaaa-bbbb-cccc", "9.9.9"):
        assert text in out


def test_an_allow_listed_finding_passes(tmp_path, capsys):
    allow = f"PYSEC-2026-1 | python | baseline; fixed by SX-11 | expires={FUTURE}\n"
    code, out = run(tmp_path, capsys, pip=ONE_PIP, allow=allow)
    assert code == 0
    assert "1 allow-listed, 0 not" in out


@pytest.mark.parametrize("entry_id", ["CVE-2026-1", "ghsa-AAAA-bbbb-CCCC", "PYSEC-2026-1"])
def test_an_alias_or_any_letter_case_matches(tmp_path, capsys, entry_id):
    code, _ = run(tmp_path, capsys, pip=ONE_PIP, allow=f"{entry_id} | python | reason | expires={FUTURE}\n")
    assert code == 0


def test_one_advisory_listed_twice_is_one_finding_with_the_aliases_of_both(tmp_path, capsys):
    # pip-audit really does this: same id twice for one package, the second listing lacking an alias
    report = pip_report(
        ("aaa", "1.0", [pip_vuln("PYSEC-1", ["CVE-2026-1", "BIT-aaa-2026-1"]), pip_vuln("PYSEC-1", ["CVE-2026-1"])]),
    )
    code, out = run(tmp_path, capsys, pip=report)
    assert code == 1 and out.count("NOT ALLOWED") == 1
    for alias in ("BIT-aaa-2026-1", "CVE-2026-1", "PYSEC-1"):
        code, _ = run(tmp_path, capsys, pip=report, allow=f"{alias} | python | reason | expires={FUTURE}\n")
        assert code == 0, alias
    # and in the other order, so the alias is found whichever listing carries it
    report["dependencies"][0]["vulns"].reverse()
    code, _ = run(tmp_path, capsys, pip=report, allow=f"BIT-aaa-2026-1 | python | reason | expires={FUTURE}\n")
    assert code == 0


def test_one_unlisted_finding_among_listed_ones_still_fails(tmp_path, capsys):
    report = pip_report(("aaa", "1.0", [pip_vuln("PYSEC-1"), pip_vuln("PYSEC-2")]))
    code, out = run(tmp_path, capsys, pip=report, allow=f"PYSEC-1 | python | reason | expires={FUTURE}\n")
    assert code == 1
    assert "PYSEC-2" in out
    assert out.count("NOT ALLOWED") == 1


def test_an_entry_only_covers_its_own_scope(tmp_path, capsys):
    report = npm_report(next=("high", [advisory("GHSA-1111-2222-3333", "high")]))
    code, out = run(tmp_path, capsys, frontend=report, allow=f"GHSA-1111-2222-3333 | promptfoo | reason | expires={FUTURE}\n")
    assert code == 1
    assert "NOT ALLOWED [frontend]" in out


def test_a_package_without_vulnerabilities_is_not_a_finding(tmp_path, capsys):
    code, out = run(tmp_path, capsys, pip=CLEAN_PIP)
    assert code == 0
    assert "0 finding(s)" in out


# --------------------------------------------------------------------------
# expiry and stale entries
# --------------------------------------------------------------------------
def test_an_expired_entry_fails_even_though_the_finding_is_still_there(tmp_path, capsys):
    code, out = run(tmp_path, capsys, pip=ONE_PIP, allow="PYSEC-2026-1 | python | reason | expires=2026-11-22\n")
    assert code == 1
    assert "EXPIRED [python] PYSEC-2026-1" in out
    assert out.count("::error") == 0 and "FAILED, 1 problem(s)" in out  # reported once, not as expired AND not allowed


def test_an_entry_is_valid_through_its_last_day(tmp_path, capsys):
    code, _ = run(tmp_path, capsys, pip=ONE_PIP, allow="PYSEC-2026-1 | python | reason | expires=2026-11-23\n")
    assert code == 0


def test_an_expired_entry_fails_even_when_its_scope_is_not_checked_in_this_run(tmp_path, capsys):
    code, out = run(tmp_path, capsys, pip=CLEAN_PIP, allow="GHSA-1111-2222-3333 | frontend | reason | expires=2020-01-01\n")
    assert code == 1
    assert "EXPIRED [frontend]" in out


def test_a_stale_entry_fails(tmp_path, capsys):
    code, out = run(tmp_path, capsys, pip=CLEAN_PIP, allow=f"PYSEC-2026-1 | python | reason | expires={FUTURE}\n")
    assert code == 1
    assert "STALE [python] PYSEC-2026-1" in out


def test_a_stale_entry_is_judged_only_in_scopes_that_were_checked(tmp_path, capsys):
    allow = f"GHSA-1111-2222-3333 | frontend | reason | expires={FUTURE}\n"
    code, _ = run(tmp_path, capsys, pip=CLEAN_PIP, allow=allow)  # no --frontend report in this run
    assert code == 0
    code, out = run(tmp_path, capsys, pip=CLEAN_PIP, frontend=CLEAN_NPM, allow=allow)
    assert code == 1
    assert "STALE [frontend]" in out


def test_an_entry_that_matches_a_finding_in_another_scope_is_stale_in_its_own(tmp_path, capsys):
    report = npm_report(next=("high", [advisory("GHSA-1111-2222-3333", "high")]))
    allow = (
        f"GHSA-1111-2222-3333 | frontend | reason | expires={FUTURE}\n"
        f"GHSA-1111-2222-3333 | promptfoo | reason | expires={FUTURE}\n"
    )
    code, out = run(tmp_path, capsys, frontend=report, promptfoo=CLEAN_NPM, allow=allow)
    assert code == 1
    assert "STALE [promptfoo]" in out
    assert "STALE [frontend]" not in out


# --------------------------------------------------------------------------
# the allow list itself
# --------------------------------------------------------------------------
def test_comments_blank_lines_and_indentation_are_ignored(tmp_path, capsys):
    allow = f"""
        # a comment

           # an indented comment
        PYSEC-2026-1 | python | reason with spaces and a # sign | expires={FUTURE}
    """
    code, _ = run(tmp_path, capsys, pip=ONE_PIP, allow=allow)
    assert code == 0


@pytest.mark.parametrize(
    "line",
    [
        "PYSEC-2026-1 | python | reason",  # no expiry
        f"PYSEC-2026-1 | python | reason | extra | expires={FUTURE}",  # five fields
        f"PYSEC-2026-1 | pip | reason | expires={FUTURE}",  # unknown scope
        f"PYSEC-2026-1 | python |  | expires={FUTURE}",  # empty reason
        f" | python | reason | expires={FUTURE}",  # empty id
        f"not an id | python | reason | expires={FUTURE}",  # id with spaces
        "PYSEC-2026-1 | python | reason | expires=2026-13-40",  # impossible date
        "PYSEC-2026-1 | python | reason | expires=20261231",  # wrong date format
        "PYSEC-2026-1 | python | reason | until=2026-12-31",  # wrong key
        "PYSEC-2026-1 | python | reason | 2026-12-31",  # no key
        "PYSEC-2026-1 python reason",  # not separated at all
    ],
)
def test_a_malformed_allow_list_line_fails_and_names_its_line_number(tmp_path, capsys, line):
    allow = f"# header\n\n{line}\n"
    code, out = run(tmp_path, capsys, pip=CLEAN_PIP, allow=allow)
    assert code == 1
    assert ":3:" in out


def test_every_bad_line_is_reported_not_just_the_first(tmp_path, capsys):
    allow = f"bad one\nPYSEC-1 | python | ok | expires={FUTURE}\nbad two\n"
    code, out = run(tmp_path, capsys, pip=CLEAN_PIP, allow=allow)
    assert code == 1
    assert ":1:" in out and ":3:" in out


def test_a_missing_allow_list_fails(tmp_path, capsys):
    code = check_audit.main(["--pip", str(write_json(tmp_path, "p.json", CLEAN_PIP)), "--allowlist", str(tmp_path / "nope.txt")], environ={}, today=TODAY)
    assert code == 1
    assert "cannot read the allow list" in capsys.readouterr().out


def test_the_shipped_allow_list_parses():
    entries, problems = check_audit.load_allowlist(HERE.parent / "audit-allowlist.txt")
    assert problems == []
    for entry in entries:  # empty in SX-09; SX-10 fills it, and every line must stay well formed
        assert entry.reason and entry.scope in check_audit.SCOPES


# --------------------------------------------------------------------------
# npm severity and ids
# --------------------------------------------------------------------------
@pytest.mark.parametrize("severity", ["high", "critical"])
def test_npm_high_and_critical_advisories_count(tmp_path, capsys, severity):
    report = npm_report(next=(severity, [advisory("GHSA-1111-2222-3333", severity)]))
    code, out = run(tmp_path, capsys, frontend=report)
    assert code == 1
    assert "NOT ALLOWED [frontend] next" in out and "GHSA-1111-2222-3333" in out


@pytest.mark.parametrize("severity", ["moderate", "low", "info"])
def test_npm_lower_severities_do_not_count(tmp_path, capsys, severity):
    report = npm_report(mermaid=(severity, [advisory("GHSA-1111-2222-3333", severity)]))
    code, out = run(tmp_path, capsys, frontend=report)
    assert code == 0
    assert "0 finding(s)" in out


def test_npm_severity_is_read_per_advisory_not_per_package(tmp_path, capsys):
    # the package is "high" because of one advisory; the moderate one next to it must not need an entry
    report = npm_report(
        browserslist=("high", [advisory("GHSA-aaaa-aaaa-aaaa", "high", 1), advisory("GHSA-bbbb-bbbb-bbbb", "moderate", 2)])
    )
    code, out = run(tmp_path, capsys, frontend=report, allow=f"GHSA-aaaa-aaaa-aaaa | frontend | reason | expires={FUTURE}\n")
    assert code == 0
    assert "1 finding(s)" in out


def test_npm_string_entries_in_via_are_not_findings(tmp_path, capsys):
    # "next is high because postcss is": postcss has its own entry, next's string entry adds nothing
    report = npm_report(
        next=("high", ["postcss"]),
        postcss=("high", [advisory("GHSA-1111-2222-3333", "high")]),
    )
    code, out = run(tmp_path, capsys, frontend=report, allow=f"GHSA-1111-2222-3333 | frontend | reason | expires={FUTURE}\n")
    assert code == 0
    assert "1 finding(s) in 1 package(s)" in out


def test_npm_an_advisory_without_severity_uses_the_package_severity(tmp_path, capsys):
    adv = advisory("GHSA-1111-2222-3333", "x")
    del adv["severity"]
    code, _ = run(tmp_path, capsys, frontend=npm_report(next=("high", [adv])))
    assert code == 1
    code, _ = run(tmp_path, capsys, frontend=npm_report(next=("moderate", [adv])))
    assert code == 0


def test_npm_a_severity_this_script_does_not_know_counts(tmp_path, capsys):
    code, _ = run(tmp_path, capsys, frontend=npm_report(next=("severe", [advisory("GHSA-1111-2222-3333", "severe")])))
    assert code == 1


def test_npm_an_advisory_with_no_ghsa_url_falls_back_to_its_source_number(tmp_path, capsys):
    adv = advisory("GHSA-x", "high", source=4242)
    adv["url"] = "https://example.test/advisories/4242"
    report = npm_report(old=("high", [adv]))
    code, out = run(tmp_path, capsys, frontend=report)
    assert code == 1 and "NPM-4242" in out
    code, _ = run(tmp_path, capsys, frontend=report, allow=f"NPM-4242 | frontend | reason | expires={FUTURE}\n")
    assert code == 0


def test_npm_an_advisory_that_cannot_be_identified_fails_the_report(tmp_path, capsys):
    adv = {"severity": "high", "title": "no url, no source"}
    code, out = run(tmp_path, capsys, frontend=npm_report(next=("high", [adv])))
    assert code == 1
    assert "REPORT [frontend]" in out


def test_frontend_and_promptfoo_are_separate_scopes(tmp_path, capsys):
    report = npm_report(next=("critical", [advisory("GHSA-1111-2222-3333", "critical")]))
    allow = f"GHSA-1111-2222-3333 | frontend | reason | expires={FUTURE}\n"
    code, _ = run(tmp_path, capsys, frontend=report, promptfoo=CLEAN_NPM, allow=allow)
    assert code == 0
    code, out = run(tmp_path, capsys, frontend=CLEAN_NPM, promptfoo=report, allow=allow)
    assert code == 1
    assert "NOT ALLOWED [promptfoo]" in out


# --------------------------------------------------------------------------
# reports that cannot be trusted: fail closed
# --------------------------------------------------------------------------
@pytest.mark.parametrize("flag", ["--pip", "--frontend", "--promptfoo"])
def test_a_missing_report_fails(tmp_path, capsys, flag):
    code = check_audit.main([flag, str(tmp_path / "nope.json"), "--allowlist", str(allowlist(tmp_path, ""))], environ={}, today=TODAY)
    assert code == 1
    assert "cannot read" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["", "not json", "{", "\x00\x01"])
def test_a_report_that_is_not_json_fails(tmp_path, capsys, content):
    path = tmp_path / "pip.json"
    path.write_text(content, encoding="utf-8")
    code = check_audit.main(["--pip", str(path), "--allowlist", str(allowlist(tmp_path, ""))], environ={}, today=TODAY)
    assert code == 1
    assert "cannot read the pip-audit report" in capsys.readouterr().out


def test_a_report_that_is_not_utf8_fails(tmp_path, capsys):
    path = tmp_path / "pip.json"
    path.write_bytes(b"\xff\xfe\x00{}")
    code = check_audit.main(["--pip", str(path), "--allowlist", str(allowlist(tmp_path, ""))], environ={}, today=TODAY)
    assert code == 1


@pytest.mark.parametrize(
    "report",
    [{}, [], "text", 7, {"dependencies": "x"}, {"dependencies": [1]}, {"dependencies": [{"version": "1"}]}],
)
def test_a_pip_report_of_the_wrong_shape_fails(tmp_path, capsys, report):
    code, out = run(tmp_path, capsys, pip=report)
    assert code == 1
    assert "REPORT [python]" in out


def test_a_report_that_is_the_json_null_fails(tmp_path, capsys):
    path = tmp_path / "pip.json"
    path.write_text("null", encoding="utf-8")
    code = check_audit.main(["--pip", str(path), "--allowlist", str(allowlist(tmp_path, ""))], environ={}, today=TODAY)
    assert code == 1
    assert "REPORT [python]" in capsys.readouterr().out


def test_a_pip_vulnerability_without_an_id_fails(tmp_path, capsys):
    code, _ = run(tmp_path, capsys, pip=pip_report(("aaa", "1.0", [{"fix_versions": []}])))
    assert code == 1


def test_the_json_npm_writes_when_its_own_audit_failed_fails_the_gate(tmp_path, capsys):
    broken = {"message": "request to the registry failed, reason: connect ECONNREFUSED", "error": {"summary": "", "detail": ""}}
    code, out = run(tmp_path, capsys, frontend=broken)
    assert code == 1
    assert "ECONNREFUSED" in out  # the reason is shown, so the log says why


@pytest.mark.parametrize("report", [{}, [], {"vulnerabilities": {}}, {"auditReportVersion": 1, "vulnerabilities": {}}, {"auditReportVersion": 2}])
def test_an_npm_report_of_the_wrong_shape_fails(tmp_path, capsys, report):
    code, out = run(tmp_path, capsys, promptfoo=report)
    assert code == 1
    assert "REPORT [promptfoo]" in out


def test_one_unreadable_report_does_not_hide_a_finding_in_another(tmp_path, capsys):
    code, out = run(tmp_path, capsys, pip=ONE_PIP, frontend={})
    assert code == 1
    assert "NOT ALLOWED [python]" in out and "REPORT [frontend]" in out


def test_a_scope_whose_report_failed_is_not_also_reported_stale(tmp_path, capsys):
    code, out = run(tmp_path, capsys, frontend={}, allow=f"GHSA-1111-2222-3333 | frontend | reason | expires={FUTURE}\n")
    assert code == 1
    assert "STALE" not in out


def test_pip_audit_skipped_packages_are_a_notice_not_a_failure(tmp_path, capsys):
    report = {"dependencies": [{"name": "aaa", "version": "1.0", "vulns": []}, {"name": "local-thing", "skip_reason": "not on PyPI"}]}
    code, out = run(tmp_path, capsys, pip=report)
    assert code == 0
    assert "skipped 1 package" in out and "local-thing" in out


def test_report_text_cannot_forge_a_log_command(tmp_path, capsys):
    report = pip_report(("evil\n::set-output name=x::y", "1\r\n::error::fake", [pip_vuln("PYSEC-1\n::warning::hi")]))
    code, out = run(tmp_path, capsys, pip=report, env={"GITHUB_ACTIONS": "true"})
    assert code == 1
    for line in out.splitlines():
        assert line.startswith(("::error title=dependency audit::", "  ", "dependency audit gate")), line


# --------------------------------------------------------------------------
# output and exit status
# --------------------------------------------------------------------------
def test_each_problem_is_one_line(tmp_path, capsys):
    report = pip_report(("aaa", "1.0", [pip_vuln("PYSEC-1"), pip_vuln("PYSEC-2")]), ("bbb", "2.0", [pip_vuln("PYSEC-3")]))
    code, out = run(tmp_path, capsys, pip=report, allow=f"PYSEC-9 | python | reason | expires={FUTURE}\n")
    assert code == 1
    assert out.count("NOT ALLOWED") == 3
    assert out.count("STALE") == 1
    assert "FAILED, 4 problem(s)" in out


def test_inside_github_actions_each_problem_is_an_error_annotation(tmp_path, capsys):
    _, out = run(tmp_path, capsys, pip=ONE_PIP, env={"GITHUB_ACTIONS": "true"})
    assert "::error title=dependency audit::NOT ALLOWED [python] aaa 1.0" in out
    _, out = run(tmp_path, capsys, pip=ONE_PIP, env={})
    assert "::error" not in out


def test_giving_no_report_is_a_usage_error(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        check_audit.main(["--allowlist", str(allowlist(tmp_path, ""))], environ={}, today=TODAY)
    assert exc.value.code == 2


def test_the_command_line_exit_status(tmp_path):
    """Run the script the way CI will: as a program, reading real files."""
    script = HERE / "check_audit.py"
    allow = allowlist(tmp_path, "")
    ok = write_json(tmp_path, "ok.json", CLEAN_PIP)
    bad = write_json(tmp_path, "bad.json", ONE_PIP)
    good = subprocess.run([sys.executable, str(script), "--pip", str(ok), "--allowlist", str(allow)], capture_output=True, text=True)
    assert good.returncode == 0, good.stdout + good.stderr
    failed = subprocess.run([sys.executable, str(script), "--pip", str(bad), "--allowlist", str(allow)], capture_output=True, text=True)
    assert failed.returncode == 1
    assert "NOT ALLOWED" in failed.stdout


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
