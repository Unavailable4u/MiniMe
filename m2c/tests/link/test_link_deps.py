"""
m2c/tests/link/test_link_deps.py -- Patch C04: the one new dependency, and the
promise that the client's copy of the protocol schemas was not touched.
"""
from __future__ import annotations

import ast
import importlib.metadata as md
import re
import subprocess
import sys
import tomllib

import pytest

from link_fakes import *  # noqa: F401,F403  (autouse fixtures; see link_fakes.py)

PYPROJECT = PKG_DIR.parent.parent / "pyproject.toml"


def test_websockets_is_the_one_runtime_dependency_and_is_pinned_exactly():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    assert data["project"]["dependencies"] == ["websockets==16.0"]


def test_the_pin_matches_the_daemons_and_the_installed_version():
    daemon = REPO_ROOT / "daemon" / "requirements.txt"
    if daemon.is_file():
        pins = re.findall(r"^websockets==([0-9.]+)\s*$", daemon.read_text(encoding="utf-8"), re.M)
        assert pins == ["16.0"], "m2c and daemon must pin the same websockets"
    assert md.version("websockets") == "16.0"


def test_only_the_link_package_imports_websockets():
    offenders = []
    for path in sorted(PKG_DIR.rglob("*.py")):
        if "link" in path.relative_to(PKG_DIR).parts[:1]:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import) and any(a.name.split(".")[0] == "websockets" for a in node.names):
                offenders.append(path.name)
            if isinstance(node, ast.ImportFrom) and node.level == 0 and (node.module or "").split(".")[0] == "websockets":
                offenders.append(path.name)
    assert offenders == []


def test_the_client_copy_of_the_schemas_is_still_byte_identical_to_protocol_v1():
    pytest.importorskip("jsonschema", reason="install protocol/requirements.txt to run protocol/validate.py")
    validate = REPO_ROOT / "protocol" / "validate.py"
    if not validate.is_file():
        pytest.skip("protocol/validate.py not found (running from an sdist, outside the repository)")
    result = subprocess.run([sys.executable, str(validate), "--check-client-copy"], capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
