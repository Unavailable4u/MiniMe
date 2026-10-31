"""
m2c/tests/policy/test_policy_rules.py -- Patch C06: rules, layers, trust, loading.

Covers rule syntax, matching (exact first, then ``*`` patterns, no regular
expressions), evaluation order deny > ask > allow over the UNION of the layers,
the untrusted-project rule, and the loader (which takes paths as arguments, so
nothing here touches the real home directory, and which fails closed).
"""
from __future__ import annotations

import pytest

from m2c.policy import RuleLayers, RuleSet, load_layers, load_rule_file, merge_layers, parse_rule
from m2c.policy.rules import RuleSyntaxError, pattern_matches
from policy_helpers import bash, layers, run

A, K, D = "allow", "ask", "deny"


# --- syntax ---------------------------------------------------------------
@pytest.mark.parametrize("text", [
    "bash(git status)", "read_file(src/**)", "edit_file(*.md)", "bash(git diff:*)", "bash(*)",
    "bash( ls )", "delete_path(build/**)", "bash(echo (nested) parens)", "mcp.server-x(a)",
])
def test_parse_rule_accepts(text):
    r = parse_rule(text, "allow")
    assert r.action == "allow" and r.pattern


@pytest.mark.parametrize("text", [
    "", "bash", "bash()", "bash( )", "(ls)", "Bash(ls)", "bash(ls", "bash ls)", "*(ls)", "bash (ls)",
    "bash(ls)x", "bash(l\ns)", "bash(\x00)", "a" * 2000, None, 5, ["bash(ls)"], "bash(" + "a" * 1100 + ")",
])
def test_parse_rule_rejects(text):
    with pytest.raises(RuleSyntaxError):
        parse_rule(text, "allow")


def test_parse_rule_rejects_unknown_action_and_layer():
    with pytest.raises(RuleSyntaxError):
        parse_rule("bash(ls)", "permit")
    with pytest.raises(RuleSyntaxError):
        parse_rule("bash(ls)", "allow", "root")


def test_rule_label_and_raw():
    r = parse_rule("bash(git status)", "deny", "managed")
    assert r.raw == "bash(git status)" and r.label == "deny:managed:bash(git status)"


# --- matching -------------------------------------------------------------
@pytest.mark.parametrize("pattern,text,expected", [
    ("git status", "git status", True), ("git status", "git status ", False), ("git status", "git", False),
    ("git diff:*", "git diff", True), ("git diff:*", "git diff --stat", True), ("git diff:*", "git difftool", False),
    ("git diff:*", "git", False), ("git:*", "git", True), ("git:*", "gitx", False), (":*", "anything", True),
    ("*", "", True), ("*", "x y z", True), ("a*c", "abc", True), ("a*c", "ac", True), ("a*c", "abd", False),
    ("a*c", "acc", True), ("a**c", "abc", True), ("*a*b*", "xaxbx", True), ("*a*b*", "xbxax", False),
    ("a*b*c", "abc", True), ("a*b*c", "ac", False), ("a*b*c", "aXbYc", True), ("src/**", "src/a/b.py", True),
    ("src/**", "src/", True), ("src/**", "src", False), ("src/**", "srcx/a", False), ("*.md", "a.md", True),
    ("*.md", "a.mdx", False), ("a?c", "abc", False), ("a?c", "a?c", True), ("a.c", "abc", False),
    ("a[b]c", "abc", False), ("a(b", "a(b", True), ("^a$", "a", False), ("a|b", "a", False), ("a+", "aa", False),
    ("ab*ab", "ab", False), ("ab*ab", "abab", True), ("ab*ba", "aba", False),
])
def test_pattern_matches(pattern, text, expected):
    assert pattern_matches(pattern, text) is expected


def test_pattern_matching_is_linear_on_hostile_patterns():
    # a regex engine would backtrack for a very long time on this
    assert pattern_matches("*a" * 40 + "b", "a" * 5000) is False
    assert pattern_matches("*" * 500, "x" * 100000) is True


@pytest.mark.parametrize("path,expected", [
    ("src/a.py", A), ("src/a/b/c.py", A), ("srcx/a.py", K), ("a.py", K), ("lib/src/a.py", K),
])
def test_path_allow_rule(path, expected):
    d = run("read_file", {"path": path}, mode="ask", lay=layers(allow=["edit_file(src/**)"]))
    assert d.action == A  # reads are allowed by the mode regardless
    d = run("edit_file", {"path": path, "old_string": "a", "new_string": "b"}, lay=layers(allow=["edit_file(src/**)"]))
    assert d.action == expected
    if expected == A:
        assert d.basis == "rule" and d.matched_rule == "allow:user:edit_file(src/**)"


@pytest.mark.parametrize("path,expected", [
    ("README.md", A), ("docs/a.md", A), ("a/b/c/d.md", A), ("README.mdx", K), ("md", K), ("docs/a.txt", K),
])
def test_pattern_without_slash_matches_the_file_name(path, expected):
    d = run("write_file", {"path": path, "content": "x"}, lay=layers(allow=["write_file(*.md)"]))
    assert d.action == expected


@pytest.mark.parametrize("path,expected", [
    (".env", D), (".env.example", D), ("a/.env.local", D), ("src/x.py", A), ("env.txt", A),
])
def test_user_deny_rule_for_env_files(path, expected):
    # the built-in denylist exempts .env.example; a user's broader rule does not
    assert run("read_file", {"path": path}, lay=layers(deny=["read_file(.env*)"])).action == expected


def test_deny_rule_by_basename_and_home_form():
    lay = layers(deny=["read_file(*.secret)", "read_file(~/notes/**)", "read_file(private/**)"])
    assert run("read_file", {"path": "a/b/x.secret"}, lay=lay).action == D
    assert run("read_file", {"path": "private/a"}, lay=lay).action == D
    assert run("read_file", {"path": "/home/u/proj/private/a"}, lay=lay).action == D
    assert run("read_file", {"path": "~/notes/a"}, lay=lay).action == D
    assert run("read_file", {"path": "/home/u/notes/a"}, lay=lay).action == D
    assert run("read_file", {"path": "public/a"}, lay=lay).action == A


def test_a_rule_for_one_tool_does_not_apply_to_another():
    lay = layers(deny=["read_file(src/**)"])
    assert run("read_file", {"path": "src/a"}, lay=lay).action == D
    assert run("list_dir", {"path": "src"}, lay=lay).action == A
    assert run("edit_file", {"path": "src/a", "old_string": "a", "new_string": "b"}, mode="accept-edits", lay=lay).action == A
    assert bash("ls", lay=layers(allow=["read_file(*)"])).action == K


# --- evaluation order -----------------------------------------------------
def test_deny_beats_ask_beats_allow_in_one_layer():
    lay = layers(allow=["bash(make)"], ask=["bash(make)"], deny=["bash(make)"])
    assert bash("make", lay=lay).action == D
    lay = layers(allow=["bash(make)"], ask=["bash(make)"])
    assert bash("make", lay=lay).action == K
    assert bash("make", lay=layers(allow=["bash(make)"])).action == A


def test_exact_string_and_pattern_both_apply():
    lay = layers(allow=["bash(make:*)"], deny=["bash(make clean)"])
    assert bash("make test", lay=lay).action == A
    assert bash("make clean", lay=lay).action == D
    lay = layers(deny=["bash(make:*)"], allow=["bash(make clean)"])
    assert bash("make clean", lay=lay).action == D  # deny is checked first, whatever is exact


def test_ask_rule_beats_a_mode_default_allow():
    lay = layers(ask=["read_file(secrets-ish/**)"])
    assert run("read_file", {"path": "secrets-ish/a"}, lay=lay).action == K
    assert run("read_file", {"path": "a"}, lay=lay).action == A


def test_allow_rule_does_not_override_outside_the_project():
    lay = layers(allow=["read_file(/etc/**)", "read_file(*)", "edit_file(*)", "write_file(**)"])
    assert run("read_file", {"path": "/etc/hosts"}, lay=lay).action == K
    assert run("read_file", {"path": "../x"}, lay=lay).action == K
    assert run("write_file", {"path": "../x", "content": "c"}, lay=lay).action == K


def test_allow_rule_does_not_override_hard_lists():
    lay = layers(allow=["read_file(**)", "edit_file(**)", "write_file(**)", "delete_path(**)", "bash(*)"])
    assert run("read_file", {"path": ".env"}, lay=lay).action == D
    assert run("read_file", {"path": "~/.ssh/id_rsa"}, lay=lay).action == D
    assert run("edit_file", {"path": ".git/config", "old_string": "a", "new_string": "b"}, lay=lay).action == D
    assert run("write_file", {"path": ".bashrc", "content": "x"}, lay=lay).action == D
    assert run("delete_path", {"path": ".m2c/x"}, lay=lay).action == D
    assert bash("cat .env", lay=lay).action == D


# --- layering -------------------------------------------------------------
M = lambda a=(), k=(), d=(): (a, k, d)  # noqa: E731


def test_managed_deny_beats_user_and_project_allow():
    lay = layers(allow=["bash(make)"], managed=M(d=["bash(make)"]), project=M(a=["bash(make)"]))
    assert bash("make", lay=lay, project_trusted=True).action == D
    assert bash("make", lay=lay).action == D


def test_managed_ask_beats_user_allow():
    lay = layers(allow=["bash(make)"], managed=M(k=["bash(make)"]))
    assert bash("make", lay=lay).action == K


def test_user_deny_beats_project_allow_and_project_cannot_weaken_user_ask():
    lay = layers(deny=["bash(make)"], ask=["bash(lint)"], project=M(a=["bash(make)", "bash(lint)"]))
    assert bash("make", lay=lay, project_trusted=True).action == D
    assert bash("lint", lay=lay, project_trusted=True).action == K


def test_a_lower_layer_may_add_a_deny_or_ask():
    lay = layers(allow=["bash(make)", "bash(lint)"], project=M(d=["bash(make)"], k=["bash(lint)"]))
    assert bash("make", lay=lay, project_trusted=True).action == D
    assert bash("lint", lay=lay, project_trusted=True).action == K


def test_the_order_of_layers_does_not_matter_for_deny_and_ask():
    deny_in_managed = layers(allow=["bash(make)"], managed=M(d=["bash(make)"]))
    deny_in_user = layers(deny=["bash(make)"], managed=M(a=["bash(make)"]))
    assert bash("make", lay=deny_in_managed).action == D
    assert bash("make", lay=deny_in_user).action == D


def test_allow_from_any_trusted_layer_works():
    lay = layers(managed=M(a=["bash(a1)"]), allow=["bash(a2)"], project=M(a=["bash(a3)"]))
    assert [bash(c, lay=lay, project_trusted=True).action for c in ("a1", "a2", "a3")] == [A, A, A]
    assert bash("a3", lay=lay, project_trusted=True).matched_rule == "allow:project:bash(a3)"


def test_untrusted_project_rules_are_ignored():
    lay = layers(project=M(a=["bash(make)"], k=["bash(ls)"], d=["bash(lint)"]))
    assert bash("make", lay=lay).action == K            # allow ignored: asks by default
    assert bash("make", lay=lay, project_trusted=False).action == K
    assert bash("lint", lay=lay).action == K            # deny ignored too (nothing else allows it)
    assert bash("make", lay=lay, project_trusted=True).action == A
    assert bash("lint", lay=lay, project_trusted=True).action == D
    assert bash("ls", lay=lay, project_trusted=True).action == K


@pytest.mark.parametrize("flag", [None, 1, "yes", "true", [True], 0])
def test_trust_must_be_literally_true(flag):
    lay = layers(project=M(a=["bash(make)"]))
    assert bash("make", lay=lay, project_trusted=flag).action == K


def test_project_path_rules_are_ignored_when_untrusted():
    lay = layers(project=M(a=["edit_file(src/**)"]))
    e = {"path": "src/a", "old_string": "a", "new_string": "b"}
    assert run("edit_file", e, lay=lay).action == K
    assert run("edit_file", e, lay=lay, project_trusted=True).action == A


def test_merge_layers_is_pure_and_labels_by_slot():
    lay = RuleLayers(user=RuleSet((parse_rule("bash(a)", "allow", "managed"),)))  # claims to be managed
    m = merge_layers(lay, True)
    assert [r.layer for r in m.allow] == ["user"]
    assert merge_layers(lay, True) == merge_layers(lay, True)


def test_merge_layers_never_raises():
    assert merge_layers(None, True).invalid  # type: ignore[arg-type]
    assert merge_layers(RuleLayers(user="x"), True).invalid  # type: ignore[arg-type]
    assert merge_layers(RuleLayers(user=RuleSet(("x",))), True).invalid  # type: ignore[arg-type]


def test_merge_orders_managed_then_user_then_project():
    lay = layers(deny=["bash(u)"], managed=M(d=["bash(m)"]), project=M(d=["bash(p)"]))
    m = merge_layers(lay, True)
    assert [r.layer for r in m.deny] == ["managed", "user", "project"]
    assert [r.layer for r in merge_layers(lay, False).deny] == ["managed", "user"]
    assert merge_layers(lay, False).project_ignored


# --- loading --------------------------------------------------------------
GOOD = """
[permissions]
allow = ["bash(npm test)", "edit_file(src/**)"]
ask = ["bash(git push:*)"]
deny = ["read_file(.env*)", "bash(curl:*)"]
"""


def write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_load_good_file(tmp_path):
    rs = load_rule_file(write(tmp_path, "s.toml", GOOD), "user")
    assert not rs.invalid and len(rs.rules) == 5
    assert {r.layer for r in rs.rules} == {"user"}
    assert [r.action for r in rs.rules] == ["deny", "deny", "ask", "allow", "allow"]  # deny, ask, allow: the evaluation order


def test_missing_file_and_none_are_empty_not_invalid(tmp_path):
    assert load_rule_file(str(tmp_path / "nope.toml"), "user") == RuleSet()
    assert load_rule_file(None, "user") == RuleSet()
    assert load_rule_file(write(tmp_path, "e.toml", ""), "user") == RuleSet()
    assert load_rule_file(write(tmp_path, "o.toml", "[other]\nx = 1\n"), "user") == RuleSet()


@pytest.mark.parametrize("text", [
    "[permissions\nallow = []", "allow = [", "not toml at all {", '[permissions]\nallow = "bash(ls)"',
    '[permissions]\nallow = ["ls"]', '[permissions]\nallow = [5]', '[permissions]\nallow = ["bash(ls)", "oops"]',
    '[permissions]\nalow = ["bash(ls)"]', '[permissions]\nallow = []\nextra = 1', 'permissions = "x"',
    '[permissions]\ndeny = ["bash()"]', '[permissions]\nask = [["bash(ls)"]]',
])
def test_malformed_files_are_invalid_not_partial(tmp_path, text):
    rs = load_rule_file(write(tmp_path, "s.toml", text), "user")
    assert rs.invalid and rs.errors and not rs.rules


def test_unreadable_oversized_and_binary_files_are_invalid(tmp_path):
    assert load_rule_file(str(tmp_path), "user").invalid                      # a directory
    big = write(tmp_path, "big.toml", "# " + "x" * 1_100_000)
    assert load_rule_file(big, "user").invalid
    p = tmp_path / "bin.toml"
    p.write_bytes(b"\xff\xfe\x00\x01")
    assert load_rule_file(str(p), "user").invalid
    many = write(tmp_path, "many.toml", '[permissions]\nallow = [' + ",".join(['"bash(a)"'] * 6000) + "]")
    assert load_rule_file(many, "user").invalid


def test_error_messages_never_echo_the_file(tmp_path):
    rs = load_rule_file(write(tmp_path, "s.toml", '[permissions]\nallow = ["bash(SECRETWORD", "x"]'), "user")
    assert rs.invalid
    assert "SECRETWORD" not in " ".join(rs.errors)


def test_load_layers_reads_three_files_and_skips_an_untrusted_project(tmp_path):
    m = write(tmp_path, "m.toml", '[permissions]\ndeny = ["bash(curl:*)"]')
    u = write(tmp_path, "u.toml", '[permissions]\nallow = ["bash(make)"]')
    p = write(tmp_path, "p.toml", '[permissions]\nallow = ["bash(lint)"]')
    lay = load_layers(managed=m, user=u, project=p, project_trusted=True)
    assert [len(x.rules) for x in (lay.managed, lay.user, lay.project)] == [1, 1, 1]
    lay = load_layers(managed=m, user=u, project=p, project_trusted=False)
    assert lay.project == RuleSet()
    # the untrusted project file is never even opened: a broken one cannot taint the result
    bad = write(tmp_path, "bad.toml", "{{{{")
    assert load_layers(user=u, project=bad).project == RuleSet()
    assert load_layers(user=u, project=bad, project_trusted=True).project.invalid


def test_load_layers_defaults_do_not_touch_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(AssertionError("opened a file")))
    assert load_layers() == RuleLayers()


def test_end_to_end_from_files(tmp_path):
    m = write(tmp_path, "m.toml", '[permissions]\ndeny = ["bash(curl:*)"]')
    u = write(tmp_path, "u.toml", GOOD)
    lay = load_layers(managed=m, user=u)
    assert bash("npm test", lay=lay).action == A
    assert bash("curl x", lay=lay).action == D
    assert bash("git push origin", lay=lay).action == K
    assert run("read_file", {"path": ".env.local"}, lay=lay).action == D


# --- invalid settings never produce an allow --------------------------------
def test_an_invalid_layer_means_nothing_is_auto_allowed(tmp_path):
    bad = load_rule_file(write(tmp_path, "bad.toml", "{{{{"), "managed")
    good = layers(allow=["bash(make)"]).user
    lay = RuleLayers(managed=bad, user=good)
    assert bash("make", lay=lay).action == K
    assert run("read_file", {"path": "a"}, lay=lay).action == K   # even the mode default
    assert run("read_file", {"path": ".env"}, lay=lay).action == D  # hard denies still hold
    assert bash("curl x", lay=RuleLayers(managed=bad, user=layers(deny=["bash(curl:*)"]).user)).action == D


def test_invalid_project_layer_only_matters_when_trusted(tmp_path):
    bad = load_rule_file(write(tmp_path, "bad.toml", "{{{{"), "project")
    lay = RuleLayers(user=layers(allow=["bash(make)"]).user, project=bad)
    assert bash("make", lay=lay).action == A
    assert bash("make", lay=lay, project_trusted=True).action == K
