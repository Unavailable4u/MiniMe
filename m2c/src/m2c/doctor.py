"""
m2c/src/m2c/doctor.py -- Patch C01: a minimal ``m2c doctor``.

Answers one question before anything else is built on top of it: *can this
machine run M2C at all?*  It is the "doctor-lite" of build plan §5 (C01); the
full doctor (sandbox probes, proxy, clock skew, keyring, update status,
diagnostics bundle) is C21.

Checks, in report order, and what a failure means:

    Operating system   HARD  Linux or WSL2.  WSL1, native Windows, macOS and
                             anything else is rejected with a message that
                             says what to do instead (build plan §O3, §11.1).
    Python             HARD  3.11 or newer (decision D1).
    git                HARD  must be on PATH.
    ripgrep (rg)       HARD  must be on PATH.
    bubblewrap (bwrap) WARN  must be on PATH for the sandbox.  Only a warning
                             *for now*: nothing in C01 needs the sandbox, and
                             the later work package that does (C11) refuses
                             to run sandboxed features without it.  It is
                             not a hard requirement here so a developer can
                             install and test the client before installing
                             bubblewrap.  The check only looks for the
                             program; actually running it to see whether
                             unprivileged user namespaces work (§11.1) is C21.
    Project location   WARN  the project must not live under /mnt/c (slow,
                             different permission semantics, defeats the
                             sandbox's assumptions).  Warning only, per the
                             work package; C11 will refuse to sandbox it.
    Config directory   HARD  ~/.m2c (or $M2C_HOME) can be created with mode
                             0700 and written to.

"Hard" means a FAIL result and a non-zero exit status; "WARN" is printed
prominently but leaves the exit status at 0.  If a check itself crashes, a
hard check reports FAIL (fail closed) and a soft check reports WARN.

WSL detection follows §11.1: the kernel release string.  A WSL2 release looks
like ``5.15.153.1-microsoft-standard-WSL2``; WSL1 reports something like
``4.4.0-19041-Microsoft`` -- "microsoft" without "WSL2".  Known limit: a
user-built WSL2 kernel whose release string contains neither word is treated
as ordinary Linux, which is also supported, so the outcome is the same.

Testability: the doctor never reads the machine directly.  Everything it
needs arrives in one ``Environment`` value (OS name, kernel release, Python
version, a ``which`` function, the project path, a config-directory probe).
``Environment.detect()`` fills it from the real machine; tests build one by
hand so each check is mocked on its own.

The report is plain ASCII with no colour codes, so it reads the same in a
terminal, a CI log or a pasted bug report.  Log output never contains
paths or messages (see logging_setup.py).

Place this file at: m2c/src/m2c/doctor.py
"""
from __future__ import annotations

import enum
import logging
import os
import platform
import shutil
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import paths
from .logging_setup import log_event

logger = logging.getLogger("m2c.doctor")

MIN_PYTHON = (3, 11)
REPORT_WIDTH = 100


class Status(str, enum.Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: Status
    detail: str
    hint: str = ""


@dataclass(frozen=True)
class Environment:
    """Everything the checks need to know, as plain values and callables."""

    system: str  # platform.system(): "Linux", "Windows", "Darwin", ...
    release: str  # platform.release(): the kernel release string
    python_version: tuple[int, int, int]
    which: Callable[[str], str | None]
    project_path: Path | None  # as given (or the cwd); None if unknown
    resolved_project_path: Path | None  # after resolving symlinks
    prepare_config_dir: Callable[[], Path]  # create 0700 + write test; raises ConfigDirError

    @classmethod
    def detect(cls, project: Path | None = None) -> Environment:
        """Read the real machine.  ``project`` defaults to the current directory."""
        if project is None:
            try:
                project = Path.cwd()
            except OSError:  # the working directory was deleted
                project = None
        resolved: Path | None = None
        if project is not None:
            try:
                resolved = project.resolve()
            except (OSError, RuntimeError):  # unreadable component or symlink loop
                resolved = project
        return cls(
            system=platform.system(),
            release=platform.release(),
            python_version=tuple(sys.version_info[:3]),
            which=shutil.which,
            project_path=project,
            resolved_project_path=resolved,
            prepare_config_dir=paths.prepare_config_dir,
        )


@dataclass(frozen=True)
class Report:
    results: tuple[CheckResult, ...]

    @property
    def ok(self) -> bool:
        """True when no hard requirement failed (warnings are allowed)."""
        return not any(r.status is Status.FAIL for r in self.results)

    def _count(self, status: Status) -> int:
        return sum(1 for r in self.results if r.status is status)

    def render(self, width: int = REPORT_WIDTH) -> str:
        """Plain-text report, wrapped to ``width`` columns (default 100).

        A hint may span several lines (``\\n``).  Lines that start with two
        spaces are kept verbatim (they are commands to copy); the rest are
        word-wrapped.  Words are never split, so a long path stays intact.
        """
        label = {Status.OK: "ok  ", Status.WARN: "WARN", Status.FAIL: "FAIL"}
        name_width = max(len(r.name) for r in self.results)
        lines = ["m2c doctor", "==========", ""]
        for r in self.results:
            prefix = f"  [{label[r.status]}] {r.name.ljust(name_width)}  "
            pad = " " * len(prefix)
            room = max(20, width - len(prefix))
            detail = _wrap(r.detail, room) or [""]
            lines.append(prefix + detail[0])
            lines += [pad + extra for extra in detail[1:]]
            if r.hint:
                for number, hint_line in enumerate(r.hint.split("\n")):
                    if hint_line.startswith("  "):
                        lines.append(pad + "   " + hint_line)
                        continue
                    wrapped = _wrap(hint_line, room - 3)
                    for index, piece in enumerate(wrapped):
                        lead = "-> " if (number == 0 and index == 0) else "   "
                        lines.append(pad + lead + piece)
        passed, warned, failed = self._count(Status.OK), self._count(Status.WARN), self._count(Status.FAIL)
        lines += [
            "",
            f"Result: {passed} passed, {warned} {'warning' if warned == 1 else 'warnings'}, {failed} failed.",
        ]
        if failed:
            lines += _wrap(
                "Hard requirements are not met: fix the FAIL items above and run 'm2c doctor' again.", width
            )
        else:
            lines.append("All hard requirements are met.")
            if warned:
                lines += _wrap(
                    "Warnings do not stop m2c from installing, but features that depend on them "
                    "(such as the command sandbox) will refuse to run until they are fixed.",
                    width,
                )
        return "\n".join(lines) + "\n"


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width=width, break_long_words=False, break_on_hyphens=False)


# --------------------------------------------------------------------------
# Individual checks.  Each takes the Environment and returns one CheckResult.
# --------------------------------------------------------------------------
def classify_linux(release: str) -> str:
    """'wsl2', 'wsl1' or 'linux' from a kernel release string (§11.1)."""
    lowered = release.lower()
    if "microsoft" in lowered:
        return "wsl2" if "wsl2" in lowered else "wsl1"
    return "linux"


def check_os(env: Environment) -> CheckResult:
    name = "Operating system"
    system = env.system
    if system == "Linux":
        kind = classify_linux(env.release)
        if kind == "wsl1":
            return CheckResult(
                name,
                Status.FAIL,
                f"WSL1 detected (kernel release {env.release!r}). WSL1 is not supported.",
                "Convert this distribution to WSL2. In Windows PowerShell run:\n"
                "  wsl --set-version <distro-name> 2\n"
                "You can list your distributions with:\n"
                "  wsl -l -v",
            )
        if kind == "wsl2":
            return CheckResult(name, Status.OK, f"Linux under WSL2 (kernel {env.release})")
        return CheckResult(name, Status.OK, f"Linux (kernel {env.release})")
    if system == "Windows" or system.upper().startswith(("CYGWIN", "MSYS", "MINGW")):
        return CheckResult(
            name,
            Status.FAIL,
            "Native Windows is not supported.",
            "Install WSL2 with an Ubuntu distribution. In PowerShell run:\n"
            "  wsl --install -d Ubuntu\n"
            "Then install and run m2c inside it, keeping your projects in the Linux filesystem.",
        )
    if system == "Darwin":
        return CheckResult(
            name,
            Status.FAIL,
            "macOS is not supported yet (planned for a later release).",
            "Use a Linux machine, or a Linux VM, for now.",
        )
    return CheckResult(
        name,
        Status.FAIL,
        f"Unsupported operating system: {system or 'unknown'}.",
        "m2c currently supports Linux and Windows via WSL2.",
    )


def check_python(env: Environment) -> CheckResult:
    version = ".".join(str(p) for p in env.python_version)
    if tuple(env.python_version[:2]) >= MIN_PYTHON:
        return CheckResult("Python", Status.OK, f"Python {version}")
    return CheckResult(
        "Python",
        Status.FAIL,
        f"Python {version} is too old: m2c needs Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.",
        "Install a newer Python (for example: uv python install 3.11) and reinstall m2c with it.",
    )


def _tool_check(
    env: Environment,
    *,
    name: str,
    binary: str,
    missing_status: Status,
    missing_detail: str,
    hint: str,
) -> CheckResult:
    found = env.which(binary)
    if found:
        return CheckResult(name, Status.OK, str(found))
    return CheckResult(name, missing_status, missing_detail, hint)


def _install_hint(package: str) -> str:
    return (
        "Install it with your package manager:\n"
        f"  Debian/Ubuntu: sudo apt install {package}\n"
        f"  Fedora:        sudo dnf install {package}\n"
        f"  Arch:          sudo pacman -S {package}"
    )


def check_git(env: Environment) -> CheckResult:
    return _tool_check(
        env,
        name="git",
        binary="git",
        missing_status=Status.FAIL,
        missing_detail="'git' was not found on PATH.",
        hint=_install_hint("git"),
    )


def check_rg(env: Environment) -> CheckResult:
    return _tool_check(
        env,
        name="ripgrep (rg)",
        binary="rg",
        missing_status=Status.FAIL,
        missing_detail="'rg' (ripgrep) was not found on PATH.",
        hint=_install_hint("ripgrep"),
    )


def check_bwrap(env: Environment) -> CheckResult:
    return _tool_check(
        env,
        name="bubblewrap (bwrap)",
        binary="bwrap",
        missing_status=Status.WARN,
        missing_detail=(
            "'bwrap' (bubblewrap) was not found on PATH. The command sandbox will refuse to run "
            "until it is installed."
        ),
        hint=_install_hint("bubblewrap")
        + "\nThis check only looks for the program; it does not yet test that user namespaces work.",
    )


def _under_mnt_c(path: Path) -> bool:
    # Lexical normalisation first so '..' cannot hide the prefix; compare whole
    # path components so '/mnt/cat' or '/mnt/c2' are not mistaken for '/mnt/c'.
    return Path(os.path.normpath(path)).parts[:3] == ("/", "mnt", "c")


def check_project_location(env: Environment) -> CheckResult:
    name = "Project location"
    given, resolved = env.project_path, env.resolved_project_path
    if given is None and resolved is None:
        return CheckResult(
            name,
            Status.WARN,
            "Could not determine the project folder (is the current directory deleted?).",
            "Run m2c from inside your project, or pass --project PATH.",
        )
    candidates = [p for p in (given, resolved) if p is not None]
    if any(_under_mnt_c(p) for p in candidates):
        shown = given if given is not None else resolved
        detail = (
            f"{shown} is under /mnt/c, the Windows filesystem. That is slow, has different "
            "permission behaviour, and the sandbox will refuse to run there."
        )
        if resolved is not None and given is not None and resolved != given:
            detail = (
                f"{given} resolves to {resolved}, under /mnt/c, the Windows filesystem. That is slow, "
                "has different permission behaviour, and the sandbox will refuse to run there."
            )
        return CheckResult(
            name,
            Status.WARN,
            detail,
            "Move or clone the project into the Linux filesystem, for example ~/code/<project>, "
            "and run m2c from there.",
        )
    return CheckResult(name, Status.OK, f"{given if given is not None else resolved} (not under /mnt/c)")


def check_config_dir(env: Environment) -> CheckResult:
    name = "Config directory"
    try:
        path = env.prepare_config_dir()
    except paths.ConfigDirError as exc:
        return CheckResult(
            name,
            Status.FAIL,
            str(exc),
            f"Set {paths.ENV_HOME} to a directory you own and can write to, "
            "or fix the permissions on the path above.",
        )
    return CheckResult(name, Status.OK, f"{path} (mode 0700, writable)")


# (stable id for logs, check function, status to report if the check itself crashes)
_CHECKS: tuple[tuple[str, Callable[[Environment], CheckResult], Status], ...] = (
    ("os", check_os, Status.FAIL),
    ("python", check_python, Status.FAIL),
    ("git", check_git, Status.FAIL),
    ("rg", check_rg, Status.FAIL),
    ("bwrap", check_bwrap, Status.WARN),
    ("project", check_project_location, Status.WARN),
    ("config_dir", check_config_dir, Status.FAIL),
)

_DISPLAY_NAMES = {
    "os": "Operating system",
    "python": "Python",
    "git": "git",
    "rg": "ripgrep (rg)",
    "bwrap": "bubblewrap (bwrap)",
    "project": "Project location",
    "config_dir": "Config directory",
}


def run_doctor(project: Path | None = None, env: Environment | None = None) -> Report:
    """Run every check and return the Report.  Never raises for a check bug."""
    if env is None:
        env = Environment.detect(project)
    results: list[CheckResult] = []
    for check_id, check, crash_status in _CHECKS:
        try:
            result = check(env)
        except Exception as exc:  # a broken probe must not take the report down
            log_event(logger, logging.WARNING, "doctor.check_crashed", check=check_id, exc_type=type(exc).__name__)
            result = CheckResult(
                _DISPLAY_NAMES[check_id],
                crash_status,
                f"This check could not run ({type(exc).__name__}).",
                "This is probably a bug in m2c doctor or an unusual system; please report it.",
            )
        log_event(logger, logging.DEBUG, "doctor.check", check=check_id, status=result.status.value)
        results.append(result)
    return Report(tuple(results))
