"""
m2c/tests/conftest.py -- Patch C01: shared fixtures.

Two jobs:

1. Hermeticity.  The autouse ``_hermetic_env`` fixture points both
   ``M2C_HOME`` and ``HOME`` at a per-test temp directory and clears
   ``M2C_LOG_LEVEL``, so no test can read or create anything in the real
   ``~/.m2c`` -- even a test that deliberately exercises the "M2C_HOME
   unset" fallback only ever sees the temp HOME.

2. A ``make_env`` factory that builds a ``doctor.Environment`` in which
   every probe is a plain value or a fake callable.  The doctor takes its
   facts through that one object (see m2c/doctor.py), so each check can be
   mocked individually without monkeypatching globals.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from m2c import doctor

# A kernel release string as printed by a real WSL2 Ubuntu.
WSL2_RELEASE = "5.15.153.1-microsoft-standard-WSL2"
# What WSL1 reports (capital M, no "WSL2").
WSL1_RELEASE = "4.4.0-19041-Microsoft"
# A plain Ubuntu kernel.
NATIVE_RELEASE = "6.8.0-45-generic"


@pytest.fixture(autouse=True)
def _hermetic_env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("M2C_HOME", str(tmp_path / "m2c-home"))
    monkeypatch.delenv("M2C_LOG_LEVEL", raising=False)
    return home


@pytest.fixture
def make_env(tmp_path) -> Callable[..., doctor.Environment]:
    """Factory for an all-green Environment; override any field by keyword."""

    def _factory(**overrides) -> doctor.Environment:
        tools = overrides.pop(
            "tools", {"rg": "/usr/bin/rg", "git": "/usr/bin/git", "bwrap": "/usr/bin/bwrap"}
        )
        config_dir = tmp_path / "fake-config-dir"
        defaults = dict(
            system="Linux",
            release=WSL2_RELEASE,
            python_version=(3, 11, 9),
            which=lambda name: tools.get(name),
            project_path=Path("/home/user/code/app"),
            resolved_project_path=Path("/home/user/code/app"),
            prepare_config_dir=lambda: config_dir,
        )
        defaults.update(overrides)
        return doctor.Environment(**defaults)

    return _factory
