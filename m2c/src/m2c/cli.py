"""
m2c/src/m2c/cli.py -- Patch C01: the ``m2c`` command.

    m2c --version
    m2c login                 stub: says it is not implemented yet, exits 3
    m2c logout                stub: same
    m2c doctor [--project P]  check that this machine can run m2c

Built on argparse only (runtime dependencies are the standard library until
a work package says otherwise).  ``main()`` returns an exit status instead of
calling ``sys.exit`` itself, so tests can call it directly; the console
script generated from ``[project.scripts]`` does ``sys.exit(main())``.

Exit statuses (stable, scripts may rely on them):

    0   success (for doctor: every hard requirement met; warnings allowed)
    1   doctor found a hard requirement missing
    2   usage error (unknown command, bad option, --project not a directory)
    3   the command exists but is not implemented yet (login, logout)

Side effects, deliberately minimal: ``--version``, ``--help`` and the stubs do
not touch the filesystem.  Only ``doctor`` creates the config directory
(``~/.m2c``, mode 0700, or ``$M2C_HOME``), because proving it can be created
and written is one of its checks.  The report goes to stdout; log lines and
error messages go to stderr.

Place this file at: m2c/src/m2c/cli.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, doctor
from .config import log_level_from_env
from .logging_setup import configure_logging, log_event

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3

logger = logging.getLogger("m2c.cli")

_EPILOG = """\
environment variables:
  M2C_HOME        directory for m2c's local files (default: ~/.m2c). Must be an
                  absolute path; created with mode 0700. Tests and second
                  profiles use this to avoid touching the real home directory.
  M2C_LOG_LEVEL   DEBUG, INFO, WARNING (default), ERROR or CRITICAL. Logs go to
                  stderr and never contain message content.

exit status: 0 ok, 1 doctor found a missing hard requirement, 2 usage error,
3 command not implemented yet.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="m2c",
        description="M2C (Minime to Code): a local-first coding agent client.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"m2c {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    sub.add_parser("login", help="sign in to your account (not implemented yet)")
    sub.add_parser("logout", help="sign out and revoke this device (not implemented yet)")
    doctor_parser = sub.add_parser(
        "doctor",
        help="check that this machine can run m2c",
        description="Check the operating system, Python, git, ripgrep, bubblewrap, "
        "the project location and the config directory.",
    )
    doctor_parser.add_argument(
        "--project",
        metavar="PATH",
        type=Path,
        help="project folder to check (default: the current directory)",
    )
    return parser


def _not_implemented(command: str) -> int:
    print(f"m2c {command}: not implemented yet.", file=sys.stderr)
    return EXIT_NOT_IMPLEMENTED


def _run_doctor(project: Path | None) -> int:
    if project is not None and not project.is_dir():
        print(f"m2c doctor: --project: {project} is not a directory.", file=sys.stderr)
        return EXIT_USAGE
    report = doctor.run_doctor(env=doctor.Environment.detect(project))
    sys.stdout.write(report.render())
    return EXIT_OK if report.ok else EXIT_FAILURE


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --version, --help and usage errors
        if exc.code is None:
            return EXIT_OK
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    configure_logging(log_level_from_env())
    log_event(logger, logging.DEBUG, "cli.start", version=__version__)

    if args.command in ("login", "logout"):
        return _not_implemented(args.command)
    if args.command == "doctor":
        return _run_doctor(args.project)
    parser.error(f"unknown command {args.command!r}")  # unreachable: argparse validates choices
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
