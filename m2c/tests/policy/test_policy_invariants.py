"""
m2c/tests/policy/test_policy_invariants.py -- Patch C06: "no allow without a rule or a mode".

Done-when item: *no code path allows without an explicit rule or mode.*  Three
independent checks:

1. STATIC.  Read decide.py and list every place the string "allow" can be turned
   into a verdict.  Exactly two functions may create an allow verdict
   (``_allow_rule``, ``_allow_mode``) and ``_finalize`` may only compare it; the
   module-level rank table is the only other mention.  A new allow path added
   later fails this test until the test is edited on purpose.
2. EXHAUSTIVE.  modes.mode_default is enumerated over its whole domain
   (test_policy_modes.py); here the engine's own allow paths are enumerated by
   wrapping the two constructors and running thousands of seeded random calls.
   Every Decision that says allow must have been built by one of them, must name
   its basis, and the basis must hold up when recomputed independently.
3. NEGATIVE.  With no allow rule anywhere, shell, delete and plan_exit are never
   allowed in any mode; in plan mode nothing but a read is ever allowed.

The decision function is also shown to be pure: it opens no file, stats nothing,
resolves no symlink, and writes no log.
"""
from __future__ import annotations

import ast
import builtins
import os
import random
import sys
from pathlib import Path

import pytest

import m2c.policy.decide as decide_mod
from m2c.policy import Context, Request, RuleLayers, RuleSet
from m2c.policy.decide import decide
from m2c.policy.modes import MODES, TOOLS
from m2c.policy.rules import parse_rule
from policy_helpers import HOME, ROOT, ctx, layers, ruleset

POLICY_DIR = Path(decide_mod.__file__).parent


# --- 1. static ----------------------------------------------------------------
def _allow_sites(tree: ast.AST):
    """(enclosing function or None, line) for every "allow" string constant."""
    sites = []

    def walk(node, fn):
        for child in ast.iter_child_nodes(node):
            f = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            if isinstance(child, ast.Constant) and child.value == "allow":
                sites.append((fn, child.lineno))
            walk(child, f)

    walk(tree, None)
    return sites


def test_only_two_functions_construct_an_allow_verdict():
    tree = ast.parse((POLICY_DIR / "decide.py").read_text())
    constructors = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_V":
            if node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "allow":
                constructors.add(node)
    owners = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for c in constructors:
                if any(c is n for n in ast.walk(fn)):
                    owners.add(fn.name)
    assert owners == {"_allow_rule", "_allow_mode"}


def test_the_string_allow_appears_only_where_expected():
    tree = ast.parse((POLICY_DIR / "decide.py").read_text())
    where = {fn for fn, _ in _allow_sites(tree)}
    # None: the module-level rank table.  _finalize only compares and re-checks.
    assert where == {None, "_allow_rule", "_allow_mode", "_finalize"}


def test_no_other_policy_module_produces_a_decision():
    for name in ("modes.py", "rules.py", "shellparse.py"):
        tree = ast.parse((POLICY_DIR / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in ("Decision", "_V"), name


def test_modes_py_allow_only_in_mode_default():
    tree = ast.parse((POLICY_DIR / "modes.py").read_text())
    users = set()
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and n.id == "ALLOW":
                    users.add(fn.name)
    assert users == {"mode_default"}


# --- the engine never logs, prints, or touches the outside world --------------
FORBIDDEN_IMPORTS = {"logging", "subprocess", "socket", "shutil", "pathlib", "sys", "io", "tempfile", "urllib"}


@pytest.mark.parametrize("name", ["modes.py", "decide.py", "shellparse.py", "rules.py", "__init__.py"])
def test_policy_modules_import_nothing_that_could_log_or_touch_the_world(name):
    tree = ast.parse((POLICY_DIR / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods = {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            mods = {(node.module or "").split(".")[0]} if node.level == 0 else set()
        else:
            continue
        assert not (mods & FORBIDDEN_IMPORTS), (name, mods)
        assert "m2c" not in mods or name == "__init__.py"  # no import of other m2c packages (C05 tools etc.)


@pytest.mark.parametrize("name", ["modes.py", "decide.py", "shellparse.py", "rules.py"])
def test_no_print_no_logger_no_open_outside_the_loader(name):
    tree = ast.parse((POLICY_DIR / name).read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "print", name
            if node.func.id == "open":
                assert name == "rules.py", name
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("debug", "info", "warning", "error", "exception", "critical", "getLogger"), name


def test_open_in_rules_py_is_only_in_the_loader():
    tree = ast.parse((POLICY_DIR / "rules.py").read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef):
            for n in ast.walk(fn):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "open":
                    assert fn.name == "load_rule_file"


def test_decide_touches_no_file_and_resolves_no_symlink(tmp_path, monkeypatch):
    # a real symlink that points out of the project: the engine is lexical and must not look
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    (root / "link").symlink_to(outside)

    def boom(*a, **k):
        raise AssertionError("the policy engine touched the filesystem")

    for target in (builtins, ):
        monkeypatch.setattr(target, "open", boom)
    for fn in ("stat", "lstat", "listdir", "scandir", "readlink", "access", "getcwd", "open", "chdir"):
        monkeypatch.setattr(os, fn, boom)
    for fn in ("realpath", "exists", "isdir", "isfile", "islink", "lexists", "abspath", "expanduser"):
        monkeypatch.setattr(os.path, fn, boom)
    c = Context(mode="accept-edits", project_root=str(root), home="/home/u")
    d = decide(Request("write_file", {"path": "link", "content": "x"}, "R1"), c)
    # lexically inside the project: this layer allows it, C05 resolves the symlink and refuses
    assert d.action == "allow" and d.basis == "mode"
    assert decide(Request("bash", {"command": "cat link && echo $(ls) > out"}, "R2"), c).action == "ask"
    assert decide(Request("read_file", {"path": "../outside.txt"}, "R0"), c).action == "ask"


# --- 2. enumerate the allow paths with seeded random calls ---------------------
RULE_VOCAB = {
    "bash": ["ls", "ls:*", "git status", "git diff:*", "echo:*", "cat:*", "rm:*", "tee:*", "cd:*", "make", "make:*", "*",
             "curl:*", "rm -rf:*", "sudo:*", "cp:*", "touch:*", "sed:*", "bash:*", "sh -c:*", "env:*", "xargs:*"],
    "read_file": ["**", "src/**", "*.md", ".env*", "secrets/**", "~/.ssh/**", "/etc/**", "*"],
    "edit_file": ["**", "src/**", "*.md", ".git/**", "*"],
    "write_file": ["**", "src/**", "*.md", "out/**", ".bashrc", "*"],
    "delete_path": ["**", "build/**", ".git/**", "*"],
    "list_dir": ["**", "src"], "glob": ["**"], "grep": ["**"], "plan_exit": ["*"],
}
SHELL_PIECES = [
    "ls", "pwd", "git status", "git diff", "echo hi", "echo $(ls)", "cat a.py", "cat .env", "cat ~/.ssh/id_rsa", "rm -rf /",
    "rm x", "rm ../x", "rm .git/HEAD", "curl x", "sudo ls", "env ls", "bash -c ls", "sh -c 'curl x'", "xargs rm", "make",
    "cd /", "cd sub", "cd .git", "tee out", "tee ~/.bashrc", "cp a b", "cp a /tmp/b", "touch .bashrc", "sed -i s/a/b/ f",
    "echo x > out", "echo x > ~/.bashrc", "echo x >> .git/config", "echo x > /tmp/y", "cat < in", "ls | sh", "$X ls", "eval ls",
    "cat <<EOF", "diff <(ls) b", "echo 'x", "ls &&", "(ls", "FOO=1 ls", "PATH=/x ls", "time ls", "nohup ls &", "ls *", "rm *",
    "rm $X", "find . -delete", "find . -exec rm {} +", "`curl x`", "c''url x", "ls ${IFS} curl",
]
SEPS = [" && ", " || ", "; ", " | ", "\n", " & ", " "]
PATHS = ["a.py", "src/a.py", "docs/x.md", "../a", "/etc/passwd", "~/x", "~/.ssh/id_rsa", ".env", ".env.example", ".git/config",
         ".bashrc", "src/../.git/x", "out/x", ".", "", "a/../../b", "sub/.zshrc", "id_rsa", "x.pem", "/home/u/proj/z", "/home/u/proj2/z"]


def random_layers(rng):
    def rs(layer):
        rules = []
        for _ in range(rng.randint(0, 6)):
            tool = rng.choice(list(RULE_VOCAB))
            action = rng.choice(["allow", "allow", "ask", "deny"])
            rules.append(parse_rule(f"{tool}({rng.choice(RULE_VOCAB[tool])})", action, layer))
        return RuleSet(tuple(rules))
    return RuleLayers(managed=rs("managed") if rng.random() < 0.4 else RuleSet(), user=rs("user"), project=rs("project"))


def random_request(rng):
    tool = rng.choice(sorted(TOOLS))
    risk = rng.choice(["R0", "R1", "R2", "R3", "R4"])
    if tool == "bash":
        n = rng.randint(1, 4)
        cmd = rng.choice(SEPS).join(rng.choice(SHELL_PIECES) for _ in range(n)) if n > 1 else rng.choice(SHELL_PIECES)
        return Request(tool, {"command": cmd}, risk)
    p = rng.choice(PATHS)
    inp = {"read_file": {"path": p}, "list_dir": {"path": p or "."}, "glob": {"pattern": rng.choice(["*.py", "../*", p or "x"])},
           "grep": {"pattern": "x", "path": p or "."}, "edit_file": {"path": p, "old_string": "a", "new_string": "b"},
           "write_file": {"path": p, "content": "c"}, "delete_path": {"path": p}, "plan_exit": {"plan": "p"}}[tool]
    return Request(tool, inp, risk)


def test_every_allow_is_built_by_an_allow_constructor_and_is_explained(monkeypatch):
    built = []
    real_rule, real_mode = decide_mod._allow_rule, decide_mod._allow_mode

    def spy_rule(rule):
        v = real_rule(rule)
        built.append(("rule", v))
        return v

    def spy_mode(reason):
        v = real_mode(reason)
        built.append(("mode", v))
        return v

    monkeypatch.setattr(decide_mod, "_allow_rule", spy_rule)
    monkeypatch.setattr(decide_mod, "_allow_mode", spy_mode)

    rng = random.Random(20261030)
    n_allow = n_total = 0
    seen_bases = set()
    for _ in range(6000):
        lay = random_layers(rng)
        mode = rng.choice(MODES)
        trusted = rng.random() < 0.5
        req = random_request(rng)
        c = ctx(mode, lay, project_trusted=trusted, non_interactive=rng.random() < 0.2, tainted=rng.random() < 0.3)
        built.clear()
        d = decide(req, c)
        n_total += 1
        assert d.action in ("allow", "ask", "deny") and d.reason
        if d.action != "allow":
            assert d.basis is None
            continue
        n_allow += 1
        seen_bases.add(d.basis)
        assert built, ("an allow with no allow constructor call", req, d)
        # 3. negative checks that hold in every mode
        assert req.tool not in ("plan_exit",), (req, d)
        if d.basis == "mode":
            assert req.tool in ("read_file", "list_dir", "glob", "grep", "edit_file", "write_file"), (req, d)
            if req.tool in ("edit_file", "write_file"):
                assert mode == "accept-edits", (req, mode, d)
            assert d.matched_rule is None
        else:
            assert d.basis == "rule" and d.matched_rule and d.matched_rule.startswith("allow:")
            label = d.matched_rule
            # the rule really is in a layer that counted
            live = [r for name, rs_ in (("managed", lay.managed), ("user", lay.user), ("project", lay.project))
                    if name != "project" or trusted for r in rs_.rules if r.action == "allow"]
            assert label in {f"allow:{name}:{r.raw}" for name in ("managed", "user", "project") for r in live}, (label, req)
        if mode == "plan":
            assert req.tool in ("read_file", "list_dir", "glob", "grep"), (req, d)
        if req.tool == "bash":
            assert d.basis == "rule"
        if req.tool == "delete_path":
            assert d.basis == "rule"
    assert n_allow > 300 and n_allow < n_total  # the fuzz really exercised both outcomes
    assert seen_bases == {"rule", "mode"}


def test_without_any_allow_rule_only_reads_and_accept_edits_edits_can_be_allowed():
    rng = random.Random(7)
    for _ in range(3000):
        lay = random_layers(rng)
        lay = RuleLayers(*(RuleSet(tuple(r for r in rs.rules if r.action != "allow")) for rs in (lay.managed, lay.user, lay.project)))
        mode = rng.choice(MODES)
        req = random_request(rng)
        d = decide(req, ctx(mode, lay, project_trusted=True))
        if d.action == "allow":
            assert req.tool in ("read_file", "list_dir", "glob", "grep", "edit_file", "write_file")
            assert d.basis == "mode"
            if req.tool in ("edit_file", "write_file"):
                assert mode == "accept-edits"


def test_with_no_rules_no_command_is_ever_allowed():
    rng = random.Random(11)
    for _ in range(2000):
        n = rng.randint(1, 4)
        cmd = rng.choice(SEPS).join(rng.choice(SHELL_PIECES) for _ in range(n))
        for mode in MODES:
            d = decide(Request("bash", {"command": cmd}, "R2"), ctx(mode))
            assert d.action in ("ask", "deny"), (cmd, mode, d)


def test_an_unreadable_settings_file_means_no_allow_at_all():
    rng = random.Random(3)
    bad = RuleSet(invalid=True, errors=("x",))
    for _ in range(1500):
        lay = random_layers(rng)
        lay = RuleLayers(managed=bad, user=lay.user, project=lay.project)
        d = decide(random_request(rng), ctx(rng.choice(MODES), lay, project_trusted=True))
        assert d.action != "allow"


def test_non_interactive_never_returns_ask_and_never_adds_an_allow():
    rng = random.Random(5)
    for _ in range(2000):
        lay, mode, req = random_layers(rng), rng.choice(MODES), random_request(rng)
        base = decide(req, ctx(mode, lay, project_trusted=True))
        hl = decide(req, ctx(mode, lay, project_trusted=True, non_interactive=True))
        assert hl.action != "ask"
        assert (base.action == "allow") == (hl.action == "allow")
        if base.action == "ask":
            assert hl.action == "deny"
        else:
            assert hl.action == base.action


def test_tainted_flag_is_accepted_and_has_no_effect_yet():
    rng = random.Random(9)
    for _ in range(1500):
        lay, mode, req = random_layers(rng), rng.choice(MODES), random_request(rng)
        a = decide(req, ctx(mode, lay, project_trusted=True, tainted=False))
        b = decide(req, ctx(mode, lay, project_trusted=True, tainted=True))
        assert a == b
    assert Context().tainted is False


def test_decide_is_deterministic_and_does_not_mutate_its_inputs():
    rng = random.Random(13)
    for _ in range(300):
        req, lay = random_request(rng), random_layers(rng)
        inp_before = dict(req.input)
        c = ctx("ask", lay, project_trusted=True)
        assert decide(req, c) == decide(req, c)
        assert req.input == inp_before


def test_reasons_and_rules_do_not_echo_the_command():
    marker = "UNIQUE-MARKER-8271"
    cmds = [f"echo {marker}", f"curl {marker}", f"cat <<EOF\n{marker}\nEOF", f"echo '{marker}", f"rm {marker}/../x > ~/.bashrc"]
    lay = layers(allow=["bash(echo:*)"], deny=["bash(curl:*)"])
    for cmd in cmds:
        d = decide(Request("bash", {"command": cmd}, "R2"), ctx("ask", lay))
        assert marker not in d.reason, (cmd, d)
        assert d.matched_rule is None or marker not in d.matched_rule
    d = decide(Request("read_file", {"path": f"../{marker}"}, "R0"), ctx())
    assert marker not in d.reason
