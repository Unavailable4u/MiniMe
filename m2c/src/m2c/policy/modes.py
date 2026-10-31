"""
m2c/src/m2c/policy/modes.py -- Patch C06: permission modes, the tool catalog the
engine knows, and the default decision per (mode, tool class, risk).

Modes (product guide 8.2, protocol ``common.json#/$defs/mode``):

    plan           reads allowed; every mutation DENIED (``plan_exit`` is the
                   way out, and it asks); no shell.
    ask            reads allowed; edits, deletes and shell ask.   (the default)
    accept-edits   edits to non-protected paths INSIDE the project are allowed;
                   deletes and shell still ask.

A mode default answers only "what happens when no rule says anything".  It can
return ``allow`` in exactly two cases, both for tools that cannot run a process
or reach the network: a read tool at risk R0 (every mode) and an edit tool at
risk R1 inside the project in ``accept-edits``.  A mode default NEVER allows
network access or process execution: the shell class has no allow entry at all,
and anything above the class ceiling (R3 network or outside the project, R4
irreversible) falls back to ``ask``.  ``tests/policy/test_policy_invariants.py``
enumerates the table to prove that.

The catalog below is a hard-coded copy of ``protocol/v1/tools/*.json`` (P00):
tool name, risk (and ``risk_max`` for ``delete_path``) and the closed set of
arguments.  It is a copy so that deciding never reads a file, and a test fails
if it drifts from the JSON.  The client's risk may RAISE a call's risk and never
lowers it (protocol README, section 7): ``effective_risk`` is the higher of the
server's risk and the client's.  A tool that is not in the catalog (an MCP tool,
a later catalog) is denied by the engine: it fails closed.

Standard library only.  Pure: no I/O, no logging.

Place this file at: m2c/src/m2c/policy/modes.py
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

__all__ = [
    "ALLOW",
    "ASK",
    "DENY",
    "MODES",
    "RISKS",
    "ModeDefault",
    "TOOLS",
    "ToolSpec",
    "class_ceiling",
    "effective_risk",
    "is_valid_mode",
    "max_risk",
    "mode_default",
    "risk_rank",
]

ALLOW, ASK, DENY = "allow", "ask", "deny"

MODES = ("plan", "ask", "accept-edits")
RISKS = ("R0", "R1", "R2", "R3", "R4")

# Tool classes.  "shell" is the only class that runs a process.
READ, EDIT, DELETE, SHELL, PLAN_EXIT = "read", "edit", "delete", "shell", "plan_exit"


@dataclass(frozen=True)
class ToolSpec:
    """What the engine knows about one tool (a copy of its P00 schema)."""

    name: str
    tool_class: str
    risk: str
    risk_max: str | None
    required: tuple[str, ...]
    optional: tuple[tuple[str, type], ...]
    types: tuple[tuple[str, type], ...]  # every argument and its JSON type

    @property
    def ceiling(self) -> str:
        """The highest risk this tool's own catalog entry allows."""
        return self.risk_max or self.risk

    @property
    def arg_types(self) -> dict[str, type]:
        return dict(self.types)


def _spec(name, cls, risk, required, types, risk_max=None) -> ToolSpec:
    req = tuple(required)
    t = tuple(types.items())
    opt = tuple((k, v) for k, v in types.items() if k not in req)
    return ToolSpec(name, cls, risk, risk_max, req, opt, t)


TOOLS: Mapping[str, ToolSpec] = {
    s.name: s
    for s in (
        _spec("read_file", READ, "R0", ["path"],
              {"path": str, "start_line": int, "end_line": int}),
        _spec("list_dir", READ, "R0", ["path"], {"path": str, "max_depth": int}),
        _spec("glob", READ, "R0", ["pattern"],
              {"pattern": str, "path": str, "max_results": int}),
        _spec("grep", READ, "R0", ["pattern"],
              {"pattern": str, "path": str, "glob": str, "context_lines": int,
               "case_insensitive": bool, "max_matches": int}),
        _spec("edit_file", EDIT, "R1", ["path", "old_string", "new_string"],
              {"path": str, "old_string": str, "new_string": str, "replace_all": bool}),
        _spec("write_file", EDIT, "R1", ["path", "content"], {"path": str, "content": str}),
        _spec("delete_path", DELETE, "R1", ["path"], {"path": str}, risk_max="R4"),
        _spec("bash", SHELL, "R2", ["command"],
              {"command": str, "timeout_s": int, "reason": str}),
        _spec("plan_exit", PLAN_EXIT, "R0", ["plan"], {"plan": str}),
    )
}  # fmt: skip


# --- risk helpers ----------------------------------------------------------


def risk_rank(risk: object) -> int:
    """0..4 for R0..R4.  Raises ValueError for anything else (callers that must
    not raise check ``risk in RISKS`` first)."""
    if isinstance(risk, str) and risk in RISKS:
        return RISKS.index(risk)
    raise ValueError("not a risk level")


def max_risk(a: str, b: str) -> str:
    return a if risk_rank(a) >= risk_rank(b) else b


def effective_risk(server_risk: str, client_risk: str) -> str:
    """The HIGHER of the server's risk and the client's own (never lower)."""
    return max_risk(server_risk, client_risk)


def is_valid_mode(mode: object) -> bool:
    return isinstance(mode, str) and mode in MODES


# --- the default decision --------------------------------------------------


@dataclass(frozen=True)
class ModeDefault:
    action: str
    reason: str


# Highest effective risk at which a class may be allowed BY A MODE DEFAULT.
# The shell class has none: no mode ever allows running a process.
_CLASS_CEILING = {READ: "R0", EDIT: "R1"}


def class_ceiling(tool_class: str) -> str | None:
    return _CLASS_CEILING.get(tool_class)


def mode_default(
    mode: str,
    tool_class: str,
    risk: str,
    *,
    inside_project: bool,
    protected: bool = False,
) -> ModeDefault:
    """The default when no rule matched.  Total: never raises, and an input it
    does not understand is a ``deny`` (fail closed).

    ``risk`` is the EFFECTIVE risk.  ``inside_project`` and ``protected`` come
    from the lexical path check; for a tool without a path pass True / False.
    """
    if not is_valid_mode(mode):
        return ModeDefault(DENY, "unknown permission mode")
    if not isinstance(risk, str) or risk not in RISKS:
        return ModeDefault(DENY, "unknown risk level")

    if tool_class == PLAN_EXIT:
        return ModeDefault(ASK, "leaving plan mode needs the user's approval")

    if tool_class == READ:
        if inside_project and risk_rank(risk) <= risk_rank("R0"):
            return ModeDefault(ALLOW, f"{mode} mode: reading inside the project is allowed")
        return ModeDefault(ASK, "read outside the project or above R0 needs approval")

    if tool_class in (EDIT, DELETE, SHELL):
        if mode == "plan":
            return ModeDefault(DENY, "plan mode does not allow changes or commands")
        if protected:
            return ModeDefault(DENY, "protected path is read-only")
        if (
            tool_class == EDIT
            and mode == "accept-edits"
            and inside_project
            and risk_rank(risk) <= risk_rank("R1")
        ):
            return ModeDefault(ALLOW, "accept-edits mode: edit inside the project is allowed")
        return ModeDefault(ASK, f"{mode} mode: this action needs approval")

    return ModeDefault(DENY, "unknown tool class")
