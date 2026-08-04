"""
tests/unit/test_part14_retirement.py — automates the checkable items
from Part 14 §7's testing checklist:

  [ ] Force a tier-3 task where suggested_agents comes back empty ->
      confirm _ensure_staffable() fills in a sane default
  [ ] Grep the entire repo for `import loop` and `loop.main()` -> zero hits
  [ ] Confirm loop.py and its compiled .pyc are actually deleted from disk

Items 4-6 (macro_loop_decision firing on a real large task, the
no-task-text CLI message, and the small-tier-3-task no-prompt check) need
a live run against real agents/LLM calls and are NOT covered here -- see
the accompanying manual/nightly check for those.

Relocated from the flat tests/ directory into tests/unit/ (Part B1 §0);
no behavior changes from the original version besides the repo-root
resolution, which now walks up two levels instead of one to land back
at the actual project root.
"""
import os
import re
import subprocess

import pytest

REPO_ROOT = os.environ.get(
    "MINIME_REPO_ROOT",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
)


# ---------------------------------------------------------------------------
# Item 1 — _ensure_staffable() never leaves a tier-3 decision unstaffable
# ---------------------------------------------------------------------------

@pytest.fixture
def ensure_staffable():
    from eo.loop_v4 import _ensure_staffable
    return _ensure_staffable


@pytest.mark.parametrize("domain,expected", [
    ("coding", ["implementer", "verifier", "fixer"]),
    ("creative_writing", ["writer", "editor"]),
    ("research", ["researcher", "writer"]),
    ("data_analysis", ["analyst", "writer"]),
    (None, ["writer"]),
    ("some_unrecognized_domain", ["writer"]),
])
def test_empty_suggested_agents_gets_filled(ensure_staffable, domain, expected):
    decision = {"tier": 3, "domain": domain, "suggested_agents": []}
    result = ensure_staffable(decision)
    assert result["suggested_agents"] == expected


def test_nonempty_suggested_agents_left_untouched(ensure_staffable):
    decision = {"tier": 3, "domain": "coding", "suggested_agents": ["implementer"]}
    result = ensure_staffable(decision)
    assert result["suggested_agents"] == ["implementer"]


def test_non_tier3_decision_left_untouched(ensure_staffable):
    decision = {"tier": 2, "domain": "coding", "suggested_agents": []}
    result = ensure_staffable(decision)
    assert result["suggested_agents"] == [], (
        "_ensure_staffable should only ever fire for tier == 3"
    )


def test_missing_suggested_agents_key_also_gets_filled(ensure_staffable):
    decision = {"tier": 3, "domain": "research"}
    result = ensure_staffable(decision)
    assert result["suggested_agents"] == ["researcher", "writer"]


# ---------------------------------------------------------------------------
# Item 2 — zero remaining `import loop` / `loop.main()` anywhere in the repo
# ---------------------------------------------------------------------------

IMPORT_LOOP_PATTERN = re.compile(r"^\s*import loop\b|loop\.main\(\)")


_THIS_FILE = os.path.abspath(__file__)


def _iter_python_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                        (".git", "__pycache__", "node_modules", ".venv", "venv")]
        for name in filenames:
            if name.endswith(".py"):
                path = os.path.join(dirpath, name)
                # Skip this test file itself -- its own docstring/checklist
                # text and regex literal legitimately contain the strings
                # this scan is looking for.
                if os.path.abspath(path) == _THIS_FILE:
                    continue
                yield path


def test_no_live_import_loop_anywhere():
    hits = []
    for path in _iter_python_files(REPO_ROOT):
        rel = os.path.relpath(path, REPO_ROOT)
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for lineno, line in enumerate(f, start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue  # comments referencing the retirement are fine
                if IMPORT_LOOP_PATTERN.search(line):
                    hits.append(f"{rel}:{lineno}: {stripped}")
    assert not hits, "Found live `import loop` / `loop.main()` references:\n" + "\n".join(hits)


def test_grep_for_loop_reference_matches_pytest_scan():
    """Cross-check using the actual grep command from the guide's
    checklist, in case the pure-Python walk above missed something (e.g.
    a non-.py file, or an unusual import form)."""
    result = subprocess.run(
        ["grep", "-rn", "--include=*.py",
         "--exclude=" + os.path.basename(_THIS_FILE),
         r"import loop\b\|loop\.main()", REPO_ROOT],
        capture_output=True, text=True,
    )
    real_hits = [
        line for line in result.stdout.splitlines()
        if not line.split(":", 2)[-1].strip().startswith("#")
    ]
    # grep exit code 1 == no matches found, which is what we want.
    assert result.returncode == 1 or not real_hits, (
        "grep found live import loop / loop.main() references:\n" + result.stdout
    )


# ---------------------------------------------------------------------------
# Item 3 — loop.py and its compiled artifact are actually gone from disk
# ---------------------------------------------------------------------------

def test_loop_py_deleted_from_disk():
    assert not os.path.exists(os.path.join(REPO_ROOT, "loop.py")), (
        "loop.py still exists on disk -- delete it"
    )


def test_loop_pyc_deleted_from_disk():
    pycache_dir = os.path.join(REPO_ROOT, "__pycache__")
    stale = []
    if os.path.isdir(pycache_dir):
        stale = [
            os.path.join(pycache_dir, f) for f in os.listdir(pycache_dir)
            if f.startswith("loop.cpython")
        ]
    assert not stale, f"Stale compiled loop.py artifact(s) still on disk: {stale}"
