"""
m2c/src/m2c/context/__init__.py -- Patch C12: instructions, rules and memory.

This package prepares the three context fields of a ``run.start`` message
(protocol/v1/run.start.json) from what is on this device:

    instructions.py   M2C.md / AGENTS.md / CLAUDE.md, hierarchical, with @imports
    rules.py          path-scoped rules from .m2c/rules/*.md
    memory.py         the project's auto-memory index, and memory writes the
                      user approves first (never containing secrets)

``assemble_run_context(root, cwd, home)`` calls all three and returns a
``RunContext``; ``RunContext.run_start_fields()`` is a dict with exactly the
keys ``instructions``, ``rules`` and ``memory_index``, ready to be passed to
``DeviceLink.start_run(**fields, ...)`` (C04) or merged into a ``run.resume``
state.  NOTHING IS SENT BY THIS PACKAGE: it reads files, and writes only
through ``apply_memory_write`` after an explicit approval.

How the pieces join later (not done here)
-----------------------------------------
* The caller (I01's repl today, C09/I02 later) supplies ``root`` (the session's
  project path), ``cwd`` and ``home`` (None: ``~/.m2c`` or ``M2C_HOME``).
* ``rules`` in ``run.start`` are "the rules matched to the files in play".  At
  the start of a session there are none, so only unconditional rules are sent;
  pass ``files_in_play`` when the caller knows some (for example on a
  ``run.resume`` with state, or a fresh run started after files were touched).
  Sending further rules in the middle of a run needs a protocol message that
  v1 does not have; that is for the context assembler (S08) / integration (I02).
* The package is deliberately independent of the C05 (tools), C06 (policy) and
  C04 (link) packages, which are built in parallel.

Place this file at: m2c/src/m2c/context/__init__.py
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from os import PathLike

from ..logging_setup import log_event
from .instructions import (
    ContextNote,
    InstructionsResult,
    load_instructions,
    resolve_home,
)
from .memory import (
    MemoryIndex,
    MemoryWriteConflict,
    MemoryWriteError,
    MemoryWriteNotApproved,
    MemoryWriteRefused,
    PreparedMemoryWrite,
    SecretFinding,
    apply_memory_write,
    find_secrets,
    load_memory_index,
    memory_dir,
    prepare_memory_write,
    project_key,
)
from .rules import Rule, RulesResult, load_rules, rules_for_run_start, select_rules

logger = logging.getLogger("m2c.context")

__all__ = [
    "ContextNote", "InstructionsResult", "MemoryIndex", "MemoryWriteConflict", "MemoryWriteError",
    "MemoryWriteNotApproved", "MemoryWriteRefused", "PreparedMemoryWrite", "Rule", "RulesResult", "RunContext",
    "SecretFinding", "apply_memory_write", "assemble_run_context", "find_secrets", "load_instructions",
    "load_memory_index", "load_rules", "memory_dir", "prepare_memory_write", "project_key",
    "rules_for_run_start", "select_rules",
]


@dataclass(frozen=True)
class RunContext:
    """The context fields of a run.start, plus what was skipped on the way."""

    instructions: str
    rules: tuple[dict[str, str], ...]        # [{"name", "text"}], the wire shape
    memory_index: str
    notes: tuple[ContextNote, ...]
    sources: tuple[str, ...]                 # instruction files that contributed, least specific first

    def run_start_fields(self) -> dict:
        """Exactly the three run.start fields (a fresh, JSON-ready dict)."""
        return {
            "instructions": self.instructions,
            "rules": [dict(rule) for rule in self.rules],
            "memory_index": self.memory_index,
        }


def assemble_run_context(
    root: str | PathLike[str],
    cwd: str | PathLike[str],
    home: str | PathLike[str] | None = None,
    *,
    files_in_play: Iterable[str | PathLike[str]] = (),
) -> RunContext:
    """Assemble ``instructions``, ``rules`` and ``memory_index`` for a session.

    ``root`` is the repository root, ``cwd`` the working directory inside it
    (ValueError otherwise), ``home`` the M2C home (None: ``paths.config_dir()``;
    a bad M2C_HOME raises ConfigDirError).  ``files_in_play`` selects the
    path-scoped rules.  Never raises for the content of any file.
    """
    home_dir = resolve_home(home)
    loaded_instructions = load_instructions(root, cwd, home_dir)
    loaded_rules = load_rules(root, home_dir)
    selected = select_rules(loaded_rules.rules, files_in_play, root)
    loaded_memory = load_memory_index(root, home_dir)
    notes = loaded_instructions.notes + loaded_rules.notes + loaded_memory.notes
    log_event(logger, logging.DEBUG, "context.assembled", count=len(notes))
    return RunContext(
        instructions=loaded_instructions.text,
        rules=tuple(rules_for_run_start(selected)),
        memory_index=loaded_memory.text,
        notes=notes,
        sources=loaded_instructions.sources,
    )
