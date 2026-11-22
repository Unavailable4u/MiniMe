#!/usr/bin/env python3
"""
.github/scripts/check_audit.py -- Patch SX-09: fail CI on a dependency-audit finding nobody has accepted.

Why this exists.  ``pip-audit`` and ``npm audit`` print a list of known
vulnerabilities and exit non-zero when they find any, so a job that runs them
is either always red (and gets ignored) or silenced with ``|| true`` (and
checks nothing).  This script is the gate between those two: the audit
commands write JSON reports, and this script compares the reports with the
allow list in ``.github/audit-allowlist.txt``.  A finding is fine only if
somebody wrote it down, with a reason and an end date.

Usage (patch SX-10 wires it into CI; the audit commands keep ``|| true``, this
script is the one that decides):

    python .github/scripts/check_audit.py --pip pip.json --frontend fe.json \\
        --promptfoo pr.json --allowlist .github/audit-allowlist.txt

Reports (give at least one; only the scopes you give are checked):
    --pip FILE         ``pip-audit -f json``           scope "python"
    --frontend FILE    ``npm audit --json``            scope "frontend"
    --promptfoo FILE   ``npm audit --json``            scope "promptfoo"
    --allowlist FILE   default: .github/audit-allowlist.txt

Allow-list format (one entry per line; blank lines and lines starting with
``#`` are ignored; a reason cannot contain ``|``):

    ID | scope | reason | expires=YYYY-MM-DD

``ID`` is any advisory id or alias (GHSA-..., PYSEC-..., CVE-...), case
insensitive.  An entry covers a finding of the same scope that has that id or
that alias.  The entry is valid through its ``expires`` date.

What fails the job (one line per problem, exit status 1):
    * a finding that no entry covers;
    * an entry whose expiry date has passed (it still covers its finding, so
      the problem is reported once, as an expired entry);
    * a stale entry: it covers no current finding in a scope that was checked
      (so fixing a dependency forces its line to be removed);
    * a malformed allow-list line, or an allow list that cannot be read;
    * a report that cannot be read or is not a report of the expected kind,
      including the JSON npm writes when the audit itself failed.  Fails
      closed: if the script cannot prove the dependencies are fine, it is red.
Exit status 0 means none of the above.  (A usage mistake, such as giving no
report at all, exits 2 through argparse.)

What counts as a finding:
    * pip-audit: every vulnerability (its report carries no severity).  The same
      advisory listed twice for one package version is one finding that has all
      the aliases of both listings (pip-audit really does this).
    * npm audit: every advisory whose severity is ``high`` or ``critical``.
      Severity is read per advisory (the object entries of ``via``); the id
      comes from the advisory ``url`` (it ends in ``GHSA-...``).  ``via``
      entries that are plain strings only point at another package, which has
      its own entry.  A severity npm did not give, or one this script does not
      know, counts (fail closed).

Packages that pip-audit skipped (``skip_reason``) were not audited; they are
printed as a notice and do not fail the job.

Text from the reports is shown only after control characters are removed, so
a hostile package name cannot forge a CI log command.

Standard library only.  Tests: test_check_audit.py (same directory).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SCOPES = ("python", "frontend", "promptfoo")
DEFAULT_ALLOWLIST = Path(__file__).resolve().parent.parent / "audit-allowlist.txt"

EXIT_OK, EXIT_PROBLEM = 0, 1

# npm severities that do NOT fail the gate.  Anything else (high, critical, or a
# value this script has never seen) counts.
NPM_IGNORED_SEVERITIES = frozenset({"info", "low", "moderate"})

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_GHSA_RE = re.compile(r"GHSA-[0-9a-z]{4}-[0-9a-z]{4}-[0-9a-z]{4}", re.IGNORECASE)


class ReportError(Exception):
    """A report cannot be used (unreadable, or not the expected kind)."""


@dataclass(frozen=True)
class Entry:
    line: int
    id: str  # upper case
    scope: str
    reason: str
    expires: dt.date


@dataclass(frozen=True)
class Finding:
    scope: str
    package: str
    ids: frozenset  # upper-case ids and aliases
    text: str  # one readable line, safe to print


def _clean(value: object) -> str:
    """Printable ASCII only and short: report text must not be able to forge a log command."""
    return re.sub(r"[^\x20-\x7e]", "?", str(value))[:200]


# --------------------------------------------------------------------------
# the allow list
# --------------------------------------------------------------------------
def load_allowlist(path: Path) -> tuple[list[Entry], list[str]]:
    """Return (entries, problems). Every bad line is a problem, none is skipped silently."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        return [], [f"cannot read the allow list {_clean(path)}: {_clean(exc)}"]
    entries: list[Entry] = []
    problems: list[str] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{_clean(path)}:{number}"
        fields = [f.strip() for f in line.split("|")]
        if len(fields) != 4:
            problems.append(f"{where}: expected 'ID | scope | reason | expires=YYYY-MM-DD', got {_clean(raw.strip())!r}")
            continue
        entry_id, scope, reason, expires = fields
        if not _ID_RE.match(entry_id):
            problems.append(f"{where}: {_clean(entry_id)!r} is not an advisory id (GHSA-..., PYSEC-..., CVE-...)")
        elif scope not in SCOPES:
            problems.append(f"{where}: scope must be one of {', '.join(SCOPES)}, got {_clean(scope)!r}")
        elif not reason:
            problems.append(f"{where}: the reason is required (say why it is allowed and what removes it)")
        else:
            key, _, value = expires.partition("=")
            date = None
            if key.strip() == "expires" and _DATE_RE.match(value.strip()):
                try:
                    date = dt.date.fromisoformat(value.strip())
                except ValueError:
                    date = None
            if date is None:
                problems.append(f"{where}: the last field must be 'expires=YYYY-MM-DD' (a real date), got {_clean(expires)!r}")
            else:
                entries.append(Entry(line=number, id=entry_id.upper(), scope=scope, reason=reason, expires=date))
    return entries, problems


# --------------------------------------------------------------------------
# reading the reports
# --------------------------------------------------------------------------
def _load_json(path: Path, what: str) -> object:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:  # ValueError covers bad JSON and bad UTF-8
        raise ReportError(f"cannot read the {what} report {_clean(path)}: {_clean(exc)}") from exc


def parse_pip(data: object) -> tuple[list[Finding], list[str]]:
    """Findings from a ``pip-audit -f json`` report, and the names of packages it skipped."""
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(deps, list):
        raise ReportError("not a pip-audit JSON report (it has no 'dependencies' list)")
    found: dict[tuple[str, str, str], Finding] = {}
    skipped: list[str] = []
    for dep in deps:
        if not isinstance(dep, dict) or not isinstance(dep.get("name"), str):
            raise ReportError("pip-audit report: a dependency entry has no name")
        name, version = dep["name"], dep.get("version")
        if "skip_reason" in dep:
            skipped.append(_clean(name))
            continue
        vulns = dep.get("vulns", [])
        if not isinstance(vulns, list):
            raise ReportError(f"pip-audit report: 'vulns' of {_clean(name)} is not a list")
        for vuln in vulns:
            if not isinstance(vuln, dict) or not isinstance(vuln.get("id"), str) or not vuln["id"]:
                raise ReportError(f"pip-audit report: a vulnerability of {_clean(name)} has no id")
            aliases = vuln.get("aliases") or []
            fixes = vuln.get("fix_versions") or []
            if not isinstance(aliases, list) or not isinstance(fixes, list):
                raise ReportError(f"pip-audit report: malformed aliases or fix_versions for {_clean(vuln['id'])}")
            text = f"{_clean(name)} {_clean(version)}: {_clean(vuln['id'])}"
            if aliases:
                text += f" (also {', '.join(_clean(a) for a in aliases)})"
            text += f"; fixed in {', '.join(_clean(f) for f in fixes)}" if fixes else "; no fix released"
            ids = frozenset(str(i).upper() for i in [vuln["id"], *aliases])
            key = (name, str(version), vuln["id"])
            if key in found:
                # pip-audit can list one advisory twice, with different alias sets: keep every alias
                ids |= found[key].ids
                text = found[key].text
            found[key] = Finding("python", str(name), ids, text)
    return list(found.values()), skipped


def parse_npm(data: object, scope: str) -> list[Finding]:
    """High and critical advisories from an ``npm audit --json`` report (auditReportVersion 2)."""
    vulns = data.get("vulnerabilities") if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get("auditReportVersion") != 2 or not isinstance(vulns, dict):
        hint = ""
        if isinstance(data, dict) and (data.get("message") or data.get("error")):
            hint = f" (npm said: {_clean(data.get('message') or data.get('error'))})"
        raise ReportError(f"not an npm audit JSON report (version 2 with a 'vulnerabilities' object){hint}")
    found: dict[tuple[str, str], Finding] = {}
    for key, vuln in vulns.items():
        if not isinstance(vuln, dict) or not isinstance(vuln.get("via", []), list):
            raise ReportError(f"npm audit report: the entry for {_clean(key)} is malformed")
        package = vuln["name"] if isinstance(vuln.get("name"), str) else key
        for advisory in vuln.get("via", []):
            if isinstance(advisory, str):
                continue  # "vulnerable because package X is": X has its own entry
            if not isinstance(advisory, dict):
                raise ReportError(f"npm audit report: an advisory of {_clean(package)} is malformed")
            severity = str(advisory.get("severity") or vuln.get("severity") or "unknown").lower()
            if severity in NPM_IGNORED_SEVERITIES:
                continue
            url, source = advisory.get("url"), advisory.get("source")
            ghsa = [g.upper() for g in _GHSA_RE.findall(url)] if isinstance(url, str) else []
            ids = list(ghsa)
            if source not in (None, ""):
                ids.append(f"NPM-{source}".upper())
            if not ids:
                raise ReportError(f"npm audit report: an advisory of {_clean(package)} has neither a GHSA url nor a source id")
            primary = ids[0]
            text = f"{_clean(package)} [{_clean(severity)}]: {_clean(primary)}"
            found.setdefault((package, primary), Finding(scope, str(package), frozenset(ids), text))
    return list(found.values())


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------
def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    today: dt.date | None = None,
) -> int:
    environ = os.environ if environ is None else environ
    today = today or dt.datetime.now(dt.timezone.utc).date()
    ap = argparse.ArgumentParser(description="Fail when a dependency audit reports a finding that is not on the allow list.")
    ap.add_argument("--pip", type=Path, metavar="FILE", help="pip-audit JSON report (scope python)")
    ap.add_argument("--frontend", type=Path, metavar="FILE", help="npm audit JSON report for frontend/ (scope frontend)")
    ap.add_argument("--promptfoo", type=Path, metavar="FILE", help="npm audit JSON report for backend/evals/promptfoo/")
    ap.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST, help="default: .github/audit-allowlist.txt")
    args = ap.parse_args(argv)
    reports = {"python": args.pip, "frontend": args.frontend, "promptfoo": args.promptfoo}
    reports = {scope: path for scope, path in reports.items() if path is not None}
    if not reports:
        ap.error("give at least one report: --pip, --frontend or --promptfoo")

    github = environ.get("GITHUB_ACTIONS") == "true"
    notes: list[str] = []
    problems: list[str] = []

    def problem(text: str) -> None:
        problems.append(text)

    entries, allow_problems = load_allowlist(args.allowlist)
    for text in allow_problems:
        problem(text)

    if not allow_problems:
        # Entries that passed their date still cover their finding; they are reported once, as expired.
        for entry in entries:
            if entry.expires < today:
                problem(
                    f"EXPIRED [{entry.scope}] {entry.id} expired on {entry.expires} "
                    f"({_clean(args.allowlist.name)}:{entry.line}). Fix the dependency, or renew it on purpose with a new reason and date."
                )
        used: set[int] = set()
        checked_ok: set[str] = set()
        for scope, path in reports.items():
            try:
                data = _load_json(path, "pip-audit" if scope == "python" else "npm audit")
                if scope == "python":
                    findings, skipped = parse_pip(data)
                    if skipped:
                        notes.append(f"notice: pip-audit skipped {len(skipped)} package(s) it could not audit: {', '.join(skipped)}")
                else:
                    findings = parse_npm(data, scope)
            except ReportError as exc:
                problem(f"REPORT [{scope}] {exc}")
                continue
            checked_ok.add(scope)
            unlisted = 0
            for finding in findings:
                covering = [i for i, e in enumerate(entries) if e.scope == scope and e.id in finding.ids]
                if covering:
                    used.update(covering)
                else:
                    unlisted += 1
                    problem(f"NOT ALLOWED [{scope}] {finding.text}")
            packages = len({f.package for f in findings})
            notes.append(
                f"{scope}: {len(findings)} finding(s) in {packages} package(s): "
                f"{len(findings) - unlisted} allow-listed, {unlisted} not"
            )
        for index, entry in enumerate(entries):
            if entry.scope in checked_ok and entry.expires >= today and index not in used:
                problem(
                    f"STALE [{entry.scope}] {entry.id} matches no current finding "
                    f"({_clean(args.allowlist.name)}:{entry.line}). The dependency is fixed (or gone): delete the line."
                )

    print(f"dependency audit gate (date {today}, {len(entries)} allow-list entr{'y' if len(entries) == 1 else 'ies'})")
    for note in notes:
        print(f"  {note}")
    for text in problems:
        print(f"::error title=dependency audit::{text}" if github else f"  {text}")
    if problems:
        print(f"dependency audit gate: FAILED, {len(problems)} problem(s)")
        return EXIT_PROBLEM
    print("dependency audit gate: OK")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
