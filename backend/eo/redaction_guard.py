"""
eo/redaction_guard.py — Patch B2 (CLI-as-Internal-Interface plan, §3.2
"why the denylist needs two forms").

The denylist has two halves, and they are deliberately NOT the same
mechanism:

  - The *documentation* half lives in eo/capability_entries.py, as
    entry_type="redaction" rows in the same registry:capability_entries
    store Patch B1 introduced for capability entries. It's
    human-editable and visible — the place to jot down a new redaction
    the moment someone thinks of it ("don't let an agent read
    config/prod_keys.json"), the same way you'd add a line to a
    checklist. Because it's just data, a model can write to it,
    misread it, or (if something upstream goes wrong) ignore it.

  - This module is the *enforcement* half. It is hard-coded Python —
    not data, not something write_capability_entry() can touch — so
    that "a path is unreadable" does not depend on any row existing,
    surviving, or being interpreted correctly in a data store the
    model itself has write access to. Patch B4's introspection.py
    calls is_readable() here before returning any file content,
    directory listing, or search hit; it does NOT consult
    capability_entries.py's redaction rows to decide readability,
    because the whole point of a hard-coded check is that it holds
    even if that data is empty, wrong, or maliciously permissive.

The two halves aren't meant to converge. The documentation half is
where a human (or an agent, on a human's behalf) writes down *why*
something is sensitive and keeps that reasoning visible in the same
place capability entries live. The enforcement half is where "can this
byte actually reach the model" gets decided, and it answers that
question the same way regardless of what the documentation says.

is_readable() covers three kinds of denial, checked in this order:

  1. Path resolves outside ALLOWED_ROOTS entirely (e.g. absolute paths
     into the host filesystem, `..` escapes) — denied regardless of
     name.
  2. Path matches a secret/credential filename pattern
     (SECRET_NAME_PATTERNS) — denied even if it's inside an allowed
     root, since "inside the repo" and "safe to show a model" are not
     the same property.
  3. Everything else inside an allowed root is readable.

No exception is raised for a denied path — see B4's own notes for why
(a raised reason can leak information about what's being hidden and
why). This module only ever returns True/False.
"""
from __future__ import annotations

import os
import re

# ---------------------------------------------------------------------------
# Allowed roots — the only parts of the repo eo/introspection.py (Patch B4)
# is permitted to read from at all. Deliberately an allowlist, not a
# denylist of "everywhere except X": anything not explicitly listed here
# is unreadable by default, including new top-level directories added to
# the repo later without this file being updated. That's a feature, not
# an oversight — a forgotten update to this list fails closed.
#
# Resolved relative to the repo root, which this file locates by walking
# up from its own location (backend/eo/redaction_guard.py -> backend/ ->
# repo root) rather than trusting an environment variable or cwd, since
# neither of those is guaranteed to be set the same way in every process
# that might import this module.
# ---------------------------------------------------------------------------
_THIS_FILE = os.path.abspath(__file__)
_BACKEND_DIR = os.path.dirname(os.path.dirname(_THIS_FILE))  # backend/eo -> backend
_REPO_ROOT = os.path.dirname(_BACKEND_DIR)  # backend -> repo root

ALLOWED_ROOTS = [
    os.path.join(_REPO_ROOT, "backend", "eo"),
    os.path.join(_REPO_ROOT, "backend", "agents"),
    os.path.join(_REPO_ROOT, "backend", "api"),
    os.path.join(_REPO_ROOT, "backend", "tests"),
    os.path.join(_REPO_ROOT, "docs"),
    os.path.join(_REPO_ROOT, "cli"),
    os.path.join(_REPO_ROOT, "frontend", "app"),
    os.path.join(_REPO_ROOT, "DEFERRED.md"),
    os.path.join(_REPO_ROOT, "README.md"),
]
# Notably absent: backend/.env* (secrets), backend/config (mcp_servers.json
# is a low-risk file today, but this stays out of the allowlist rather
# than special-cased in), backend/migrations (raw SQL, not something an
# agent answering "what can this system do" needs), backend/data, and
# anything above _REPO_ROOT entirely.

# ---------------------------------------------------------------------------
# Secret/credential filename patterns — denied even inside an allowed
# root. Matched against the filename only (not the full path), case-
# insensitive, so `frontend/app/lib/api_secret_client.js` is denied for
# the same reason `.env` is: the name itself signals sensitive content,
# and this check doesn't try to be smarter than that signal.
#
# Deliberately narrower than "matches the substring 'token' anywhere" —
# this repo has plenty of legitimately-readable files with "token" in
# the name (frontend/app/components/tabs/TokenUsageTab.jsx is a UI tab,
# not a secret) that a blunter pattern would wrongly deny. The patterns
# below key off the specific shapes real secrets show up in
# (dotenv files, "secret"/"credential" in the name, API-key/private-key
# filenames) rather than every word that's merely adjacent to "auth."
# ---------------------------------------------------------------------------
SECRET_NAME_PATTERNS = [
    re.compile(r"^\.env(\..+)?$", re.IGNORECASE),  # .env, .env.local — NOT env.py, NOT .env.example is still matched (deliberately: examples get denied too, since the real file must stay in lockstep per its own docstring)
    re.compile(r"secret", re.IGNORECASE),
    re.compile(r"credential", re.IGNORECASE),
    re.compile(r"(^|[_\-.])(api[_\-]?key|apikey)([_\-.]|$)", re.IGNORECASE),
    re.compile(r"^id_rsa|^id_ed25519|\.pem$|\.pfx$|\.p12$", re.IGNORECASE),
]


def _is_within(path: str, root: str) -> bool:
    """True if the resolved `path` is `root` itself or lives under it.
    Uses os.path.commonpath on resolved absolute paths so that `..`
    segments and symlink-free traversal can't walk a nominally-allowed
    path back out to somewhere it shouldn't be."""
    try:
        common = os.path.commonpath([path, root])
    except ValueError:
        # Different drives on Windows, or otherwise incomparable — treat
        # as not-within rather than raising.
        return False
    return common == root


def _matches_secret_pattern(path: str) -> bool:
    filename = os.path.basename(path)
    return any(pattern.search(filename) for pattern in SECRET_NAME_PATTERNS)


def is_readable(path: str) -> bool:
    """Hard-coded readability check for eo/introspection.py (Patch B4).

    Returns False for anything outside ALLOWED_ROOTS, anything matching
    a secret/credential filename pattern, or anything that fails to
    resolve to a real path at all. Returns True only for paths that
    clear every check.

    Deliberately has NO import from eo.capability_entries or
    eo.capabilities — this function's answer must not change based on
    what's stored in the (model-writable) capability-entries data. See
    this module's docstring for why that separation is the point.
    """
    if not path:
        return False

    try:
        resolved = os.path.abspath(os.path.realpath(path))
    except (OSError, ValueError):
        return False

    if _matches_secret_pattern(resolved):
        return False

    return any(_is_within(resolved, root) for root in ALLOWED_ROOTS)
