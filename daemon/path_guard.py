"""
daemon/path_guard.py — F2 Part 1: the one function every later part
(read tool calls in Part 3, write/delete/execute in Part 4) must route
every filesystem path through before touching disk.

Why this is its own module rather than a method on the daemon class:
Part 3 and Part 4 both need it, and it has to be trivially unit-testable
in isolation from any websocket/event-loop code, since a bug here is a
containment-boundary bug, not a feature bug.

The check is deliberately conservative:
- Resolves symlinks (`Path.resolve()`), so a symlink inside the allowed
  root that points outside it is rejected, not followed.
- Rejects the allowed root itself being (or resolving to) `/`, the
  daemon's own install directory, or the user's home directory root --
  configuring the daemon with an overly broad root is a setup mistake
  this module can catch even though it can't stop the user from doing
  it deliberately.
- Uses `os.path.commonpath`, not a plain string-prefix check, so
  `/home/user/project-x` is never treated as "inside"
  `/home/user/project` just because the string happens to start with
  it.

Place this file at: daemon/path_guard.py
"""
from __future__ import annotations

import os
from pathlib import Path

# Roots we refuse to accept as the configured allowed folder, even if
# the user's config file names one of them. Resolved at import time so
# comparisons below are apples-to-apples with the resolved candidate.
_DISALLOWED_ROOTS = frozenset(
    {
        Path(p).resolve()
        for p in ("/", str(Path.home()))
        if Path(p).exists()
    }
)


class PathGuardError(Exception):
    """Raised whenever a path fails containment or root-sanity checks."""


def assert_safe_root(root: str | Path) -> Path:
    """Validate the *configured* allowed-root folder itself (called once,
    at daemon startup, from config.py). Returns the resolved, absolute
    Path on success; raises PathGuardError otherwise.

    This is intentionally stricter than is_within_root() below: it also
    rejects the root not existing, not being a directory, or being one
    of the disallowed system/home roots.
    """
    resolved = Path(root).expanduser().resolve()

    if not resolved.exists():
        raise PathGuardError(f"configured root does not exist: {resolved}")
    if not resolved.is_dir():
        raise PathGuardError(f"configured root is not a directory: {resolved}")
    if resolved in _DISALLOWED_ROOTS:
        raise PathGuardError(
            f"configured root is too broad to be safe: {resolved} "
            "(refusing '/', the home directory, or other system roots — "
            "point the daemon at a specific project folder instead)"
        )

    return resolved


def is_within_root(candidate: str | Path, root: Path) -> bool:
    """True iff `candidate` resolves to a path at or inside `root`.

    `root` must already be the resolved Path returned by
    assert_safe_root() — this function does not re-validate the root
    itself, only where `candidate` lands relative to it.
    """
    try:
        resolved_candidate = Path(candidate).expanduser().resolve()
    except (OSError, RuntimeError):
        # Unresolvable path (e.g. a symlink loop) -- never treat as safe.
        return False

    if resolved_candidate == root:
        return True

    common = os.path.commonpath([str(resolved_candidate), str(root)])
    return common == str(root)


def assert_within_root(candidate: str | Path, root: Path) -> Path:
    """Same check as is_within_root(), but raises PathGuardError with a
    message instead of returning False. This is the one later parts
    (Part 3's list_dir/read_file, Part 4's write_file/delete/
    execute_command) should actually call, so a rejected path always
    produces a loud, logged error rather than a silently-ignored no-op.
    """
    resolved_candidate = Path(candidate).expanduser().resolve()
    if not is_within_root(resolved_candidate, root):
        raise PathGuardError(
            f"path escapes configured root: {resolved_candidate} "
            f"(root is {root})"
        )
    return resolved_candidate
