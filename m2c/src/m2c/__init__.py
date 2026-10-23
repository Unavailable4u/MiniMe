"""
m2c/src/m2c/__init__.py -- Patch C01: the M2C client package.

``m2c`` is the local half of M2C (Minime to Code): a Python command-line
client that will, in later work packages, store sessions on this machine
(C02), sign in with a device code (C03), keep a link to the MiniMe server
(C04) and execute tools locally (C05+).  This first patch (C01) is only the
skeleton: the package layout, the ``m2c`` console entry point, the config
directory, a content-free logging setup and a minimal ``m2c doctor``.

Design rules that every later patch inherits (build plan §0.2 and §1):

* Python >= 3.11 (decision D1) and the standard library only at runtime
  until a work package explicitly adds a dependency.  tests/test_cli.py
  enforces this.
* Security checks fail closed; telemetry and logging fail open.
* Nothing the user typed, the model produced or a tool returned is ever
  written to a log (see logging_setup.py).
* Everything the client keeps on disk lives under one directory, ``~/.m2c``
  by default, relocatable with the ``M2C_HOME`` environment variable so that
  tests never touch the real home directory.

This module deliberately imports nothing, so ``import m2c`` is free and the
version can be read by the build backend without importing the package.

Place this file at: m2c/src/m2c/__init__.py
"""

# Single source of truth for the version: pyproject.toml reads this line
# (tool.hatch.version) and `m2c --version` prints it.
__version__ = "0.1.0.dev0"
