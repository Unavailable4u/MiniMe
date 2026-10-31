"""
m2c/tests/tools/tools_fixtures.py -- Patch C05: shared helpers for the local
tool executor tests.

Imported by name from the test modules in this directory (the same convention
as tests/auth/auth_fixtures.py and tests/link/link_fakes.py: pytest puts a test
file's own directory on sys.path).

Nothing here looks like a real secret.  The "secret" files the tests plant are
filled with the marker ``SECRETWORD`` (or ``MARKER_*`` strings), which no
secret scanner treats as a credential, and any fake token a test needs is built
at run time (``"x" * 40``) rather than written as a literal.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from m2c.tools import ToolContext, ToolResult, run_tool

# What every planted "protected" file contains.  Tests assert that this never
# appears in any tool output.
SECRET = "SECRETWORD"

# A representative slice of the hard denylist, as project-relative paths.
PROTECTED_FILES = [
    ".env",
    ".env.local",
    "app/.env.production",
    "prod.env",
    "id_rsa",
    "id_rsa.pub",
    "keys/id_ed25519",
    "certs/server.pem",
    "tls/server.key",
    "store.p12",
    ".ssh/config",
    ".aws/credentials",
    ".netrc",
    ".git-credentials",
    ".docker/config.json",
    ".kube/config",
    ".config/gcloud/credentials.db",
    ".bash_history",
    ".zsh_history",
]

SAFE_FILE = "README.md"


@pytest.fixture
def proj(tmp_path) -> Path:
    """An empty, resolved project root (a sibling of the hermetic HOME)."""
    root = tmp_path / "proj"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def ctx(proj) -> ToolContext:
    return ToolContext(proj)


@pytest.fixture
def outside(tmp_path) -> Path:
    """A directory next to the project holding a file that must never leak."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "secret.txt").write_text(f"{SECRET} outside\n")
    return d.resolve()


def call(ctx: ToolContext, name: str, **args: Any) -> ToolResult:
    """run_tool with keyword arguments, for readability."""
    return run_tool(ctx, name, args)


def write(root: Path, rel: str, text: str = "hello\n", mode: int | None = None) -> Path:
    """Create ``root/rel`` (and parents) with ``text``; optionally chmod it."""
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    if mode is not None:
        os.chmod(p, mode)
    return p


def plant_protected(root: Path) -> None:
    """Create every PROTECTED_FILES entry plus a harmless README, all containing SECRET."""
    for rel in PROTECTED_FILES:
        write(root, rel, f"{SECRET} in {rel}\n")
    write(root, SAFE_FILE, f"{SECRET} is also in the readme\n")


def ok(result: ToolResult) -> str:
    """Assert success and return the output text."""
    assert result.ok, f"expected success, got {result.code}: {result.output}"
    return result.output


def fails(result: ToolResult, code: str) -> ToolResult:
    """Assert a specific error code and return the result."""
    assert not result.ok, f"expected {code}, but the call succeeded: {result.output[:200]}"
    assert result.code == code, f"expected {code}, got {result.code}: {result.output}"
    return result


def mode_of(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def make_fake_rg(directory: Path, body: str, name: str = "rg") -> Path:
    """Write an executable Python script standing in for ripgrep.

    ``body`` is Python source run with ``sys`` and ``os`` imported and
    ``ARGV`` (the argument list without the program name) defined.  Every fake
    first dumps what it saw to ``<directory>/seen.json`` so tests can assert on
    the exact argument list, working directory and environment it was given.
    """
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / name
    prologue = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import json, os, sys
        ARGV = sys.argv[1:]
        with open({str(directory / "seen.json")!r}, "w") as _f:
            json.dump({{"argv": ARGV, "cwd": os.getcwd(), "env": dict(os.environ)}}, _f)
        """
    )
    script.write_text(prologue + textwrap.dedent(body))
    script.chmod(0o755)
    return script


def fake_rg_seen(directory: Path) -> dict:
    return json.loads((directory / "seen.json").read_text())


HAVE_RG = shutil.which("rg") is not None
needs_rg = pytest.mark.skipif(
    not HAVE_RG,
    reason="ripgrep (rg) is not installed: the real-rg grep tests are skipped "
    "(the fake-rg and argument-builder tests still run)",
)
