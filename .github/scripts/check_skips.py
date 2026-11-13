#!/usr/bin/env python3
"""
.github/scripts/check_skips.py -- Patch Q01: fail a CI job on an unexpected skip.

Why this exists.  pytest reports a skipped test as neither a pass nor a
failure, so a suite can go green while a whole group of tests quietly stops
running: the m2c job used to skip 3 schema-validation tests because
``jsonschema`` was not installed, and would have skipped about 22 grep tests
on any runner without ripgrep, and nobody would have been told.  This script
reads the JUnit XML report pytest writes (``--junitxml``), prints every skip
reason with a count, and exits non-zero unless every reason is on the allow
list in ``skip-allowlist.txt`` (same directory).

Usage (from a job, after pytest has written its report; the job must still
run this step when pytest failed or skipped, so use ``if: ${{ !cancelled() }}``):

    python .github/scripts/check_skips.py --scope m2c report.xml [report2.xml ...]

Options:
    --scope NAME       which allow-list entries apply: "m2c", "backend", or any
                       name.  Entries whose scope is "*" always apply.
    --allowlist FILE   default: skip-allowlist.txt next to this script.

Allow-list format (one entry per line, ``#`` starts a comment):

    scope | text that must appear in the skip reason [| when=ENV_VAR]

``when=ENV_VAR`` makes the entry active only while that environment variable is
exactly "1".  The m2c job uses it for the sandbox entry: the job probes whether
bubblewrap can create a user namespace on the runner and sets the variable only
when it cannot, so a sandbox test that skips on a runner where bubblewrap
works is still a failure.

Exit status:
    0  every skip is on the list (they are still printed)
    1  a skip is not on the list, or no test ran at all
    2  the gate itself could not do its job: a report is missing or is not
       XML, the allow list is missing or has a line this script does not
       understand.  Fails closed: if it cannot prove the skips are fine, the
       job is red.

What counts as a skip: a ``<skipped>`` element of type ``pytest.skip`` (or one
with no type).  An expected failure (``pytest.xfail``) is also written by
pytest as ``<skipped>``; it is a deliberate marker, not a test that did not
run, so it is not counted here.

Standard library only.  Tests: test_check_skips.py (same directory).
"""
from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ALLOWLIST = Path(__file__).resolve().parent / "skip-allowlist.txt"

EXIT_OK, EXIT_UNEXPECTED, EXIT_BROKEN = 0, 1, 2


class GateError(Exception):
    """The gate cannot do its job (exit 2); the message says what to fix."""


@dataclass(frozen=True)
class Entry:
    scope: str
    match: str
    when: str | None = None


def load_allowlist(path: Path) -> list[Entry]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise GateError(f"cannot read the allow list {path}: {exc}") from exc
    entries: list[Entry] = []
    for number, raw in enumerate(lines, start=1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        where = f"{path}:{number}"
        fields = [f.strip() for f in line.split("|")]
        if len(fields) not in (2, 3):
            raise GateError(f"{where}: expected 'scope | reason text [| when=ENV_VAR]', got {raw.strip()!r}")
        scope, match = fields[0], fields[1]
        if not scope or not match:
            raise GateError(f"{where}: both the scope and the reason text are required")
        when = None
        if len(fields) == 3:
            key, _, var = fields[2].partition("=")
            if key != "when" or not var.strip():
                raise GateError(f"{where}: the optional third field must be 'when=ENV_VAR', got {fields[2]!r}")
            when = var.strip()
        entries.append(Entry(scope=scope, match=match, when=when))
    return entries


def read_skips(report: Path) -> tuple[int, Counter]:
    """Return (number of test cases, Counter of skip reasons) for one report."""
    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError) as exc:
        raise GateError(f"cannot read the JUnit report {report}: {exc}") from exc
    total = 0
    reasons: Counter = Counter()
    for case in root.iter("testcase"):
        total += 1
        for skipped in case.findall("skipped"):
            if skipped.get("type", "pytest.skip") == "pytest.xfail":
                continue
            reasons[(skipped.get("message") or (skipped.text or "")).strip() or "(no reason given)"] += 1
    return total, reasons


def is_allowed(reason: str, entries: Sequence[Entry], scope: str, environ: Mapping[str, str]) -> Entry | None:
    for entry in entries:
        if entry.scope not in ("*", scope):
            continue
        if entry.when is not None and environ.get(entry.when) != "1":
            continue
        if entry.match in reason:
            return entry
    return None


def main(argv: Sequence[str] | None = None, environ: Mapping[str, str] | None = None) -> int:
    environ = os.environ if environ is None else environ
    ap = argparse.ArgumentParser(description="Fail when pytest skipped a test for a reason that is not allow-listed.")
    ap.add_argument("reports", nargs="+", type=Path, help="JUnit XML file(s) written by pytest --junitxml")
    ap.add_argument("--scope", required=True, help='allow-list scope for this job, e.g. "m2c" or "backend"')
    ap.add_argument("--allowlist", type=Path, default=DEFAULT_ALLOWLIST)
    args = ap.parse_args(argv)

    try:
        entries = load_allowlist(args.allowlist)
        total, reasons = 0, Counter()
        for report in args.reports:
            n, r = read_skips(report)
            total += n
            reasons.update(r)
    except GateError as exc:
        print(f"::error title=skip gate::{exc}")
        print(f"skip gate: {exc}")
        return EXIT_BROKEN

    skipped = sum(reasons.values())
    print(f"skip gate [{args.scope}]: {total} test cases, {skipped} skipped")
    if total == 0:
        print("::error title=skip gate::no tests ran: pytest collected nothing, so nothing was checked")
        print("skip gate: no tests ran at all")
        return EXIT_UNEXPECTED

    unexpected = 0
    for reason, count in reasons.most_common():
        entry = is_allowed(reason, entries, args.scope, environ)
        if entry is not None:
            print(f"  allowed     {count} x {reason}")
        else:
            unexpected += count
            print(f"  NOT ALLOWED {count} x {reason}")
            # One annotation per distinct reason (not per test) so the run summary stays readable.
            print(f"::error title=unexpected skip::{count} test(s) skipped: {reason}")
    if unexpected:
        print(
            f"skip gate: {unexpected} test(s) were skipped for a reason that is not allow-listed. "
            f"Fix the cause (install the tool, set the variable), or, if the skip is legitimate here, add the reason "
            f"to {args.allowlist.name} with a comment saying why."
        )
        return EXIT_UNEXPECTED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
