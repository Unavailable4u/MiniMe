"""
m2c/src/m2c/policy/__init__.py -- Patch C06: the policy engine v0.

    modes.py       the modes ``plan`` / ``ask`` / ``accept-edits``, the tool
                   catalog (a copy of protocol/v1/tools) and the default decision
                   per mode, tool class and risk
    rules.py       ``allow`` / ``ask`` / ``deny`` rules as ``tool(pattern)``, the
                   managed > user > project layers, the loader, and the lexical
                   path checks (protected paths, the secrets denylist)
    shellparse.py  a real tokenizer for the shell subset that matters; whatever
                   it cannot classify with certainty is "unknown syntax" (ask)
    decide.py      ``decide(request, context) -> Decision``: pure, total, and
                   fail closed

Typical use::

    from m2c.policy import Context, Request, load_layers
    from m2c.policy.decide import decide

    layers = load_layers(managed=None, user=config_dir / "settings.toml",
                         project=root / ".m2c" / "settings.toml",
                         project_trusted=trusted)
    d = decide(Request("bash", {"command": "git status"}, "R2"),
               Context(mode="ask", layers=layers, project_root=str(root),
                       project_trusted=trusted))
    # d.action is "allow", "ask" or "deny"; d.reason says why

``decide`` the function is deliberately NOT re-exported here: ``m2c.policy.decide``
is the module, and a function of the same name would shadow it (the same choice
as ``m2c.auth.login``).  Import it as ``from m2c.policy.decide import decide``.

The one rule that matters: ``allow`` is returned only when an explicit allow
rule matched or the mode's default says allow; everything unknown is ``ask`` or
``deny``.  The engine does not log, does not touch the filesystem and imports
nothing outside the standard library.  Executing a tool is C05 (files) and C07
(shell); asking the user is C09; both consume the Decision.

Place this file at: m2c/src/m2c/policy/__init__.py
"""
from .decide import Context, Decision, Request
from .modes import MODES, TOOLS, effective_risk, mode_default
from .rules import (
    MergedRules,
    Rule,
    RuleLayers,
    RuleSet,
    RuleSyntaxError,
    load_layers,
    load_rule_file,
    merge_layers,
    parse_rule,
)
from .shellparse import ParseResult, Part, parse as parse_shell

__all__ = [
    "Context",
    "Decision",
    "MODES",
    "MergedRules",
    "ParseResult",
    "Part",
    "Request",
    "Rule",
    "RuleLayers",
    "RuleSet",
    "RuleSyntaxError",
    "TOOLS",
    "effective_risk",
    "load_layers",
    "load_rule_file",
    "merge_layers",
    "mode_default",
    "parse_rule",
    "parse_shell",
]
