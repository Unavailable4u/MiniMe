"""
m2c/tests/policy/policy_helpers.py -- Patch C06: shared builders for the policy tests.

Everything is data: a project root and home directory that are plain strings
(nothing is created on disk unless a test says so), rule layers built from
rule strings, and a ``run`` helper that calls ``decide`` once.  No real secret
shapes appear anywhere in these tests: they only name paths.

Place this file at: m2c/tests/policy/policy_helpers.py
"""
from __future__ import annotations

from m2c.policy import Context, Request, RuleLayers, RuleSet
from m2c.policy.decide import decide
from m2c.policy.rules import parse_rule

ROOT = "/home/u/proj"
HOME = "/home/u"

# Default rules most table rows use: a few explicit allows, some denies, one ask.
ALLOW_BASIC = (
    "bash(ls)", "bash(pwd)", "bash(git status)", "bash(git diff:*)", "bash(echo:*)",
    "bash(cat:*)", "bash(true)",
)  # fmt: skip
DENY_BASIC = ("bash(curl:*)", "bash(wget:*)", "bash(rm -rf:*)", "bash(nc:*)", "bash(mkfs:*)")
ASK_BASIC = ("bash(git push:*)",)
ALLOW_WRITERS = (
    "bash(rm:*)", "bash(cp:*)", "bash(mv:*)", "bash(tee:*)", "bash(touch:*)", "bash(cd:*)",
    "bash(echo:*)", "bash(sed:*)", "bash(mkdir:*)", "bash(ln:*)", "bash(dd:*)", "bash(ls:*)",
)  # fmt: skip


def ruleset(allow=(), ask=(), deny=(), layer="user") -> RuleSet:
    rules = [parse_rule(r, "allow", layer) for r in allow]
    rules += [parse_rule(r, "ask", layer) for r in ask]
    rules += [parse_rule(r, "deny", layer) for r in deny]
    return RuleSet(tuple(rules))


def layers(allow=(), ask=(), deny=(), *, managed=None, project=None) -> RuleLayers:
    """User-layer rules from the keyword arguments; ``managed`` and ``project``
    are (allow, ask, deny) triples for those layers."""
    def mk(triple, layer):
        a, k, d = triple or ((), (), ())
        return ruleset(a, k, d, layer)
    return RuleLayers(
        managed=mk(managed, "managed"),
        user=ruleset(allow, ask, deny, "user"),
        project=mk(project, "project"),
    )


BASIC = layers(ALLOW_BASIC, ASK_BASIC, DENY_BASIC)
WRITERS = layers(ALLOW_WRITERS, (), DENY_BASIC)
EMPTY = RuleLayers()


def ctx(mode="ask", lay=EMPTY, **kw) -> Context:
    kw.setdefault("project_root", ROOT)
    kw.setdefault("home", HOME)
    return Context(mode=mode, layers=lay, **kw)


def run(tool, inp, risk=None, mode="ask", lay=EMPTY, **kw):
    """One decision.  ``risk`` defaults to the tool's catalog risk."""
    from m2c.policy import TOOLS

    if risk is None:
        risk = TOOLS[tool].risk if tool in TOOLS else "R0"
    return decide(Request(tool, inp, risk), ctx(mode, lay, **kw))


def bash(command, mode="ask", lay=BASIC, **kw):
    return run("bash", {"command": command}, "R2", mode, lay, **kw)
