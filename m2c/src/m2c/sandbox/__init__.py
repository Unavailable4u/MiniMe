"""
m2c/src/m2c/sandbox -- Patch C11a: the command sandbox (bubblewrap).

SECURITY-CRITICAL.  Four small modules, each with its own long docstring:

``profile``  builds the bubblewrap argument list: what is mounted, read-only or writable, which
             namespaces are new, what environment the command gets.  A pure function.
``bwrap``    finds bubblewrap and a shell and builds the full command, ``[bwrap, *profile, "--",
             shell, "-c", command]``.  ``tools/shell.py`` runs it like it ran ``[shell, "-c", command]``.
``probe``    really tests the sandbox (a throw-away project and home with canaries, eleven
             escape attempts) and returns a structured result.
``__init__`` (this file) the three questions the rest of m2c asks:

* ``get_status()``  is a working sandbox available?  Answers from the probe, run once per session per
  settings and remembered.  ``available`` is True only when EVERY check passed; nothing else may
  call the sandbox active.
* ``is_available()``  the same as a bool.
* ``fresh_probe()``  a new probe that ignores the cache (the doctor's).

The rule everything here follows: the sandbox is never assumed.  If it was not seen to hold, the
status says so, with a fixed reason, and callers must say "full privileges" and mean it.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import bwrap, probe, profile
from .bwrap import find_bwrap, find_shell, signal_from_exit_code, wrap_command
from .probe import CHECK_NAMES, CheckOutcome, ProbeResult, reset_session_cache, run_probe, session_probe
from .profile import DEFAULT_HOME_TOOLCHAIN_DIRS, ProfileError, build_profile

__all__ = [
    "CHECK_NAMES", "CheckOutcome", "DEFAULT_HOME_TOOLCHAIN_DIRS", "ProbeResult", "ProfileError", "SandboxStatus",
    "build_profile", "bwrap", "default_denied", "find_bwrap", "find_shell", "fresh_probe", "get_status",
    "is_available", "probe", "profile", "reset", "run_probe", "signal_from_exit_code", "wrap_command",
]


@dataclass(frozen=True)
class SandboxStatus:
    """Whether a working sandbox is available, and if not exactly why.

    ``available``    True only when the probe ran every check and every check passed.
    ``reason_code``  ``ok`` or a fixed word (``bwrap_missing``, ``userns_unavailable``, ``check_failed`` ...).
    ``reason``       fixed human text; for a failed check it names the checks.  Never a path or a command.
    ``probe``        the full ``ProbeResult``.
    """

    available: bool
    reason_code: str
    reason: str
    probe: ProbeResult | None = None


def default_denied() -> tuple[str, ...]:
    """Paths no sandbox mount may expose: m2c's own config directory (wherever ``M2C_HOME`` puts it)."""
    try:
        from ..paths import config_dir

        return (str(config_dir()),)
    except Exception:  # noqa: BLE001 -- no config directory is simply nothing extra to deny
        return ()


def _status(result: ProbeResult) -> SandboxStatus:
    return SandboxStatus(result.works, result.reason_code, result.describe(), result)


def get_status(
    *,
    extra_ro: Sequence[str] = (),
    toolchain_dirs: Sequence[str] = DEFAULT_HOME_TOOLCHAIN_DIRS,
    denied: Sequence[str] | None = None,
) -> SandboxStatus:
    """The cached answer to "is a working sandbox available?" for these settings (the probe runs the first time)."""
    deny = default_denied() if denied is None else tuple(denied)
    return _status(session_probe(toolchain_dirs=tuple(toolchain_dirs), extra_ro=tuple(extra_ro), denied=deny))


def is_available(**kwargs) -> bool:
    """``get_status(...).available``."""
    return get_status(**kwargs).available


def fresh_probe(
    *,
    extra_ro: Sequence[str] = (),
    toolchain_dirs: Sequence[str] = DEFAULT_HOME_TOOLCHAIN_DIRS,
    denied: Sequence[str] | None = None,
) -> ProbeResult:
    """A new probe, never the cached one (the doctor uses this)."""
    deny = default_denied() if denied is None else tuple(denied)
    return run_probe(toolchain_dirs=tuple(toolchain_dirs), extra_ro=tuple(extra_ro), denied=deny)


def reset() -> None:
    """Forget the remembered probe results (tests)."""
    reset_session_cache()
