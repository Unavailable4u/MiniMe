"""
m2c/src/m2c/cli.py -- Patch C01: the ``m2c`` command (Patch C03: login/logout).

    m2c --version
    m2c login [--device-name N]   sign in with a code you approve in the web app
    m2c logout                    delete this device's local credential
    m2c doctor [--project P]      check that this machine can run m2c

Built on argparse only (runtime dependencies are the standard library until
a work package says otherwise).  ``main()`` returns an exit status instead of
calling ``sys.exit`` itself, so tests can call it directly; the console
script generated from ``[project.scripts]`` does ``sys.exit(main())``.

Exit statuses (stable, scripts may rely on them):

    0   success (for doctor: every hard requirement met; warnings allowed)
    1   doctor found a hard requirement missing; or login/logout hit a problem
        with the config directory or an unexpected error (only its type is shown)
    2   usage error (unknown command, bad option, --project not a directory,
        M2C_SERVER_URL missing or unsafe, unusable --device-name)
    3   reserved: C01 used it for "not implemented yet"; nothing returns it now
    4   sign-in failed: login was denied, expired or could not reach the server,
        or logout could not delete the credential (the message says which)
    130 login was cancelled with Ctrl+C

Side effects, deliberately minimal: ``--version`` and ``--help`` do not touch
the filesystem, and neither does ``logout`` when there is nothing to delete.
``doctor`` and ``login`` create the config directory (``~/.m2c``, mode 0700,
or ``$M2C_HOME``): doctor because proving it can be created and written is one
of its checks, login because the credential goes there.  The report goes to
stdout, and so do the sign-in code and address; log lines and error messages
go to stderr.  Neither a credential, a device code nor a token is ever printed
or logged (see auth/credentials.py).

Place this file at: m2c/src/m2c/cli.py
"""
from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__, doctor
from .auth import login as login_mod
from .auth.credentials import AuthError
from .config import ServerUrlError, log_level_from_env, server_url_from_env
from .logging_setup import configure_logging, log_event
from .paths import ConfigDirError

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_NOT_IMPLEMENTED = 3  # reserved: C01's stubs used it; nothing returns it since C03
EXIT_AUTH = 4
EXIT_INTERRUPTED = 130  # the shell convention for "ended by Ctrl+C"

logger = logging.getLogger("m2c.cli")

_EPILOG = """\
environment variables:
  M2C_HOME        directory for m2c's local files (default: ~/.m2c). Must be an
                  absolute path; created with mode 0700. Tests and second
                  profiles use this to avoid touching the real home directory.
  M2C_LOG_LEVEL   DEBUG, INFO, WARNING (default), ERROR or CRITICAL. Logs go to
                  stderr and never contain message content.
  M2C_SERVER_URL  address of your MiniMe server (https://..., or http:// for
                  localhost). Needed by `m2c login`; afterwards it is remembered
                  with the credential.

exit status: 0 ok, 1 doctor found a missing hard requirement (or an unexpected
error), 2 usage error, 4 sign-in failed (login or logout), 130 login cancelled.
"""

_LOGIN_DESCRIPTION = """\
Sign in without typing a password here. m2c shows a short code and a web
address; open the address in a browser (on any device), sign in to your
account and enter the code. m2c waits, then stores a credential for this
device: in your OS keyring if the optional 'keyring' package is installed and
usable, otherwise in a file (mode 0600) in the m2c config directory.

Needs M2C_SERVER_URL set to your MiniMe server (https://...).
"""

_LOGOUT_DESCRIPTION = """\
Delete this device's credential from this machine (keyring and file).

This does not contact the server: the device stays registered with your
account until you revoke it on the Devices page of the web app.
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
    login_parser = sub.add_parser(
        "login",
        help="sign in with a code you approve in the web app",
        description=_LOGIN_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    login_parser.add_argument(
        "--device-name",
        metavar="NAME",
        help="how this device appears on the web Devices page (default: this machine's host name)",
    )
    sub.add_parser(
        "logout",
        help="delete this device's local credential (revoke the device on the web Devices page)",
        description=_LOGOUT_DESCRIPTION,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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


def _unexpected(command: str, exc: Exception) -> int:
    """Whatever went wrong inside, the terminal gets the exception's *type*:
    its text can echo a request, and a traceback prints local variables."""
    log_event(logger, logging.ERROR, "cli.unexpected_error", exc_type=type(exc).__name__)
    print(f"m2c {command}: unexpected error ({type(exc).__name__}).", file=sys.stderr)
    return EXIT_FAILURE


def _run_login(device_name: str | None) -> int:
    # Everything that can be checked without the network is checked first, so
    # a typo never starts a pairing.
    try:
        name = None if device_name is None else login_mod.normalize_device_name(device_name)
    except ValueError as exc:
        print(f"m2c login: --device-name: {exc}.", file=sys.stderr)
        return EXIT_USAGE
    try:
        server_url = server_url_from_env()
    except ServerUrlError as exc:
        print(f"m2c login: {exc}.", file=sys.stderr)
        return EXIT_USAGE
    if server_url is None:
        print(
            "m2c login: set M2C_SERVER_URL to the address of your MiniMe server "
            "(for example https://api.example.com), then run it again.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        login_mod.login(server_url, device_name=name)
    except KeyboardInterrupt:
        print("\nm2c login: Cancelled. Nothing was stored.", file=sys.stderr)
        return EXIT_INTERRUPTED
    except AuthError as exc:  # messages are fixed text (auth/credentials.py)
        print(f"m2c login: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except ConfigDirError as exc:
        print(f"m2c login: {exc}.", file=sys.stderr)
        return EXIT_FAILURE
    except Exception as exc:
        return _unexpected("login", exc)
    return EXIT_OK


def _run_logout() -> int:
    try:
        removed = login_mod.logout()
    except AuthError as exc:
        print(f"m2c logout: {exc}", file=sys.stderr)
        return EXIT_AUTH
    except ConfigDirError as exc:
        print(f"m2c logout: {exc}.", file=sys.stderr)
        return EXIT_FAILURE
    except Exception as exc:
        return _unexpected("logout", exc)
    if removed:
        print("Signed out: the local credential was deleted from this machine.")
        print(
            "This device stays registered with your account until you revoke it "
            "on the Devices page of the web app."
        )
    else:
        print("Not signed in; there was no local credential to delete.")
    return EXIT_OK


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

    if args.command == "login":
        return _run_login(args.device_name)
    if args.command == "logout":
        return _run_logout()
    if args.command == "doctor":
        return _run_doctor(args.project)
    parser.error(f"unknown command {args.command!r}")  # unreachable: argparse validates choices
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
