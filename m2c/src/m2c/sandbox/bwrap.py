"""
m2c/src/m2c/sandbox/bwrap.py -- Patch C11a: finding bubblewrap and building the full command.

SECURITY-CRITICAL.  ``profile.build_profile`` says WHAT the sandbox is; this module says how to
start it.  ``tools/shell.py`` calls ``wrap_command`` and runs what it returns exactly as it ran
``[shell, "-c", command]`` before: the same ``subprocess.Popen`` with ``start_new_session=True``,
so C07's process-group kill, timeout, cancel, streaming and output cap are untouched.  Why that
still works: the process the shell tool signals is bubblewrap itself, the leader of the new
session; bubblewrap is also PID 1's parent, and when it dies (SIGKILL included) the kernel kills
every process in the sandbox's PID namespace.  A command that calls ``setsid`` or traps SIGTERM
therefore cannot outlive the call, which is the gap C07 documented and this patch closes.

The command line is ``[bwrap, *profile, "--", shell, "-c", command]``: a list, the command is ONE
element, nothing is quoted or joined, and the double dash makes sure a command that starts with
``-`` is never read as a bubblewrap option.

``find_bwrap`` looks only in ABSOLUTE ``PATH`` entries.  A relative entry (``.``, an empty
element, ``bin``) would let a file in the project directory be found first; a project is the
model's workspace, so it must never be a place a binary is picked up from.

Exit codes.  When the command is ended by a signal, bubblewrap exits with 128+N (the shell
convention) instead of dying by that signal, so the shell tool sees a plain exit status where it
used to see "-N".  ``signal_from_exit_code`` is the inverse the shell tool uses to keep printing
"(terminated by signal N)".  It cannot tell an explicit ``exit 137`` from a SIGKILL; both are
reported as the signal.  That ambiguity is inherent to the exit code and is documented, not
hidden.

Standard library only.  Place this file at: m2c/src/m2c/sandbox/bwrap.py
"""
from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence

from .profile import (
    DEFAULT_HOME_TOOLCHAIN_DIRS,
    ProfileError,
    SystemLayout,
    build_profile,
    detect_system_layout,
)

__all__ = [
    "BWRAP_NAMES", "SHELL_CANDIDATES", "default_home", "find_bwrap", "find_shell", "outer_environment",
    "signal_from_exit_code", "wrap_command",
]

BWRAP_NAMES: tuple[str, ...] = ("bwrap",)

# Absolute paths INSIDE the sandbox, in order of preference; the first that exists on the machine is used.
SHELL_CANDIDATES: tuple[str, ...] = ("/usr/bin/bash", "/bin/bash", "/usr/bin/sh", "/bin/sh")

_OUTER_PATH = "/usr/local/bin:/usr/bin:/bin"
_SAFE_PATH_FALLBACK = ("/usr/bin", "/bin", "/usr/local/bin")


def _is_executable_file(path: str) -> bool:
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and os.access(path, os.X_OK)


def find_bwrap(path: str | None = None) -> str | None:
    """The absolute path of bubblewrap, or None.  Only absolute PATH entries are looked at (see the module docstring).

    ``path`` replaces the PATH variable (tests); None means the process's own PATH.  When PATH has no usable absolute
    entry the usual system directories are tried, so a stripped environment does not hide an installed bubblewrap.
    """
    raw = os.environ.get("PATH", "") if path is None else path
    dirs = [d for d in raw.split(os.pathsep) if d and os.path.isabs(d) and "\x00" not in d]
    for extra in _SAFE_PATH_FALLBACK:
        if extra not in dirs:
            dirs.append(extra)
    for directory in dirs:
        for name in BWRAP_NAMES:
            candidate = os.path.join(directory, name)
            if _is_executable_file(candidate):
                return candidate
    return None


def find_shell(root: str = "/") -> str | None:
    """The shell to run INSIDE the sandbox, as an absolute path that exists there: bash, else sh.  None if neither."""
    for candidate in SHELL_CANDIDATES:
        host = os.path.join(root, candidate.lstrip("/"))
        if _is_executable_file(host):
            return candidate
    return None


def default_home() -> str | None:
    """The real home directory: ``$HOME`` when it is an absolute path, otherwise the passwd entry; None if unknown."""
    raw = os.environ.get("HOME", "")
    if raw and os.path.isabs(raw) and "\x00" not in raw:
        return os.path.normpath(raw)
    try:
        import pwd

        entry = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return None
    return os.path.normpath(entry) if entry and os.path.isabs(entry) else None


def outer_environment() -> dict[str, str]:
    """The environment bubblewrap itself is started with: nothing but a PATH.  The command's environment is set by
    the profile (``--clearenv`` and ``--setenv``), so the outer one never reaches it; keeping it minimal means it
    cannot leak by a mistake in that logic either."""
    return {"PATH": _OUTER_PATH}


def wrap_command(
    command: str,
    *,
    project: str,
    env: Mapping[str, str],
    home: str | None = None,
    bwrap_path: str | None = None,
    shell_path: str | None = None,
    layout: SystemLayout | None = None,
    toolchain_dirs: Sequence[str] = DEFAULT_HOME_TOOLCHAIN_DIRS,
    extra_ro: Sequence[str] = (),
    denied: Sequence[str] = (),
) -> list[str]:
    """The full argv that runs ``command`` in the sandbox: ``[bwrap, *profile, "--", shell, "-c", command]``.

    Raises ``ProfileError`` (fixed text, never a path) when the sandbox cannot be built for these inputs, or when
    bubblewrap or a shell cannot be found (codes ``bwrap_missing`` and ``no_shell``).  Nothing is started or created.
    """
    if not isinstance(command, str) or "\x00" in command:
        raise ProfileError("command_invalid")
    bwrap = bwrap_path if bwrap_path is not None else find_bwrap()
    if not bwrap:
        raise ProfileError("bwrap_missing")
    resolved_layout = layout if layout is not None else detect_system_layout("/")
    shell = shell_path if shell_path is not None else find_shell(resolved_layout.root)
    if not shell:
        raise ProfileError("no_shell")
    real_home = home if home is not None else default_home()
    if real_home is None:
        raise ProfileError("home_invalid")
    profile = build_profile(
        project=project, home=real_home, env=env, layout=resolved_layout,
        toolchain_dirs=toolchain_dirs, extra_ro=extra_ro, denied=denied,
    )
    return [bwrap, *profile, "--", shell, "-c", command]


def signal_from_exit_code(code: int | None) -> int | None:
    """The signal number a bubblewrap exit status stands for (129..159 means 128+signal), else None."""
    if isinstance(code, int) and not isinstance(code, bool) and 129 <= code <= 159:
        return code - 128
    return None
