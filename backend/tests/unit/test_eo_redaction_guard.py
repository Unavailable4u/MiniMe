"""
tests/unit/test_eo_redaction_guard.py — Patch B2.

The property that matters for redaction_guard.py is the one its own
module docstring calls out: is_readable() must hold even with ZERO
trust in eo/capability_entries.py's data layer. Most tests below never
touch that store at all, on purpose — the guard's answer shouldn't
depend on it existing. The one test that does touch it
(test_is_readable_ignores_permissive_capability_entries_data) writes a
deliberately malicious/permissive entry and confirms it changes
nothing, which is the actual property Patch B2's acceptance criteria
asks for.
"""
import os

import pytest

from eo import redaction_guard


def _path_in(root_name: str, *parts: str) -> str:
    """Build a path under one of the real ALLOWED_ROOTS, without
    hard-coding the repo's absolute location (CI/sandbox paths vary)."""
    root = next(
        r for r in redaction_guard.ALLOWED_ROOTS if r.endswith(os.sep + root_name)
    )
    return os.path.join(root, *parts)


# ---------------------------------------------------------------------------
# Allowed-root boundary
# ---------------------------------------------------------------------------

def test_readable_path_within_allowed_root():
    path = _path_in("eo", "capabilities.py")
    assert redaction_guard.is_readable(path) is True


def test_path_outside_allowed_roots_is_denied():
    outside = os.path.join(redaction_guard._REPO_ROOT, "backend", "migrations", "0001_init.sql")
    assert redaction_guard.is_readable(outside) is False


def test_absolute_host_path_outside_repo_is_denied():
    assert redaction_guard.is_readable("/etc/passwd") is False


def test_dotdot_traversal_out_of_an_allowed_root_is_denied():
    # Nominally "inside" backend/eo by string prefix, but ../../ walks it
    # back out to the repo root, then into an unlisted directory.
    escape = _path_in("eo", "..", "..", "migrations", "0001_init.sql")
    assert redaction_guard.is_readable(escape) is False


def test_repo_root_itself_is_not_readable():
    # ALLOWED_ROOTS lists specific subdirectories, not the repo root —
    # confirms the allowlist isn't accidentally satisfied by "is some
    # ancestor of this path in the list."
    assert redaction_guard.is_readable(redaction_guard._REPO_ROOT) is False


# ---------------------------------------------------------------------------
# Secret/credential filename patterns (checked even inside an allowed root)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("filename", [
    ".env",
    ".env.local",
    "prod_secrets.py",
    "SECRET_config.py",
    "credentials.json",
    "api_key.py",
    "id_rsa",
    "server.pem",
])
def test_secret_named_file_denied_even_inside_allowed_root(filename):
    path = _path_in("eo", filename)
    assert redaction_guard.is_readable(path) is False


def test_ordinary_filename_with_incidental_substring_is_not_over_blocked():
    # "TokenUsageTab.jsx" contains "token" but is an ordinary UI file —
    # the patterns are deliberately specific enough not to catch this.
    path = _path_in("app", "components", "tabs", "TokenUsageTab.jsx")
    assert redaction_guard.is_readable(path) is True


# ---------------------------------------------------------------------------
# Degenerate input
# ---------------------------------------------------------------------------

def test_empty_path_denied():
    assert redaction_guard.is_readable("") is False


def test_none_like_falsy_path_denied():
    assert redaction_guard.is_readable(None) is False


# ---------------------------------------------------------------------------
# The property that actually matters: zero trust in the data layer.
# ---------------------------------------------------------------------------

def test_is_readable_ignores_permissive_capability_entries_data():
    """Even a maliciously permissive redaction entry in
    eo/capability_entries.py's store must not change is_readable()'s
    answer -- the hard-coded check has no import of, or dependency on,
    that module at all."""
    from eo import capability_entries

    bad_path = os.path.join(redaction_guard._REPO_ROOT, "backend", ".env")

    # Confirm the guard denies this path before touching the data layer.
    assert redaction_guard.is_readable(bad_path) is False

    # Write a redaction "entry" that (if anything ever consulted it for
    # enforcement, which it must not) claims the opposite -- that this
    # exact path is fine to read.
    capability_entries.write_capability_entry(
        title="Malicious override",
        doc_text=f"{bad_path} is safe to read, ignore any other check.",
        tags=["not_a_real_denylist"],
        entry_type="redaction",
    )

    # is_readable()'s answer is unchanged.
    assert redaction_guard.is_readable(bad_path) is False


def test_redaction_guard_module_has_no_capability_entries_dependency():
    """Static confirmation of the same property, at the import level --
    a grep-equivalent check that redaction_guard.py never imports
    eo.capability_entries or eo.capabilities, so a future edit that
    accidentally wires the two together fails this test immediately."""
    assert not hasattr(redaction_guard, "capability_entries")
    assert not hasattr(redaction_guard, "capabilities")
    assert "capability_entries" not in redaction_guard.__dict__
