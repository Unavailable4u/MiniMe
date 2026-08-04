"""
test_part14_retirement.py — automates the checkable items from Part 14
§7's testing checklist:

  [ ] Force a tier-3 task where suggested_agents comes back empty ->
      confirm _ensure_staffable() fills in a sane default
  [ ] Grep the entire repo for `import loop` and `loop.main()` -> zero hits
  [ ] Confirm loop.py and its compiled .pyc are actually deleted from disk

Items 4-6 (macro_loop_decision firing on a real large task, the
no-task-text CLI message, and the small-tier-3-task no-prompt check) need
a live run against real agents/LLM calls and are NOT covered here — see
the accompanying shell script for those.

Run from the repo root:
    pytest test_part14_retirement.py -v
"""
import os
import re
import subprocess
import sys

import pytest

# Adjust this if your repo root isn't the current working directory when
# pytest is invoked.
REPO_ROOT = os.environ.get("MINIME_REPO_ROOT", os.getcwd())

sys.path.insert(0, REPO_ROOT)


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
    assert result["suggested_agents"] == expected, (
        f"domain={domain!r} did not get the expected default role set"
    )


def test_nonempty_suggested_agents_left_untouched(ensure_staffable):
    decision = {"tier": 3, "domain": "coding", "suggested_agents": ["implementer"]}
    result = ensure_staffable(decision)
    assert result["suggested_agents"] == ["implementer"], (
        "a non-empty suggested_agents list should never be overwritten"
    )


def test_non_tier3_decision_left_untouched(ensure_staffable):
    decision = {"tier": 2, "domain": "coding", "suggested_agents": []}
    result = ensure_staffable(decision)
    assert result["suggested_agents"] == [], (
        "_ensure_staffable should only ever fire for tier == 3"
    )


def test_missing_suggested_agents_key_also_gets_filled(ensure_staffable):
    # decision.get("suggested_agents") with no key at all -> None -> falsy,
    # same code path as an explicit empty list
    decision = {"tier": 3, "domain": "research"}
    result = ensure_staffable(decision)
    assert result["suggested_agents"] == ["researcher", "writer"]


# ---------------------------------------------------------------------------
# Item 2 — zero remaining `import loop` / `loop.main()` anywhere in the repo
# ---------------------------------------------------------------------------

IMPORT_LOOP_PATTERN = re.compile(r"^\s*import loop\b|loop\.main\(\)")
# Files that are ALLOWED to mention "import loop" only inside a comment
# describing the retirement -- everything else is a real hit.
COMMENT_ONLY_ALLOWLIST = {"eo/loop_v4.py"}


def _iter_python_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                        (".git", "__pycache__", "node_modules", ".venv", "venv")]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


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
        ["grep", "-rn", "--include=*.py", r"import loop\b\|loop\.main()", REPO_ROOT],
        capture_output=True, text=True,
    )
    # grep exit code 1 == no matches found, which is what we want
    lines = [l for l in result.stdout.splitlines() if "#" not in l.split(":", 2)[-1][:l.split(":",2)[-1].find("import") if "import" in l else 0] or True]
    real_hits = [l for l in result.stdout.splitlines()
                 if not l.split(":", 2)[-1].strip().startswith("#")]
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
    pyc_candidates = [
        os.path.join(REPO_ROOT, "__pycache__", "loop.cpython-313.pyc"),
    ]
    # also catch any other cached bytecode version that might linger
    pycache_dir = os.path.join(REPO_ROOT, "__pycache__")
    if os.path.isdir(pycache_dir):
        pyc_candidates += [
            os.path.join(pycache_dir, f) for f in os.listdir(pycache_dir)
            if f.startswith("loop.cpython")
        ]
    stale = [p for p in pyc_candidates if os.path.exists(p)]
    assert not stale, f"Stale compiled loop.py artifact(s) still on disk: {stale}"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))