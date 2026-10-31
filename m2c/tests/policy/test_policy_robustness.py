"""
m2c/tests/policy/test_policy_robustness.py -- Patch C06: hostile and huge input.

Every input here must come back as a Decision quickly and must never raise.
Anything malformed, unterminated or too complex is ask or deny, never allow, even
under a rule that allows every command.  The time limits are loose (they catch
quadratic or exponential behaviour, not a slow machine).
"""
from __future__ import annotations

import random
import string
import time

import pytest

from m2c.policy import Context, Request, parse_shell
from m2c.policy.decide import decide
from m2c.policy.shellparse import MAX_LENGTH
from policy_helpers import ctx, layers, ROOT

ALLOW_ALL = layers(allow=["bash(*)"])
LIMIT_S = 3.0


def go(cmd, lay=ALLOW_ALL, mode="ask"):
    t = time.perf_counter()
    d = decide(Request("bash", {"command": cmd}, "R2"), ctx(mode, lay))
    assert time.perf_counter() - t < LIMIT_S, "decision took too long"
    assert d.action in ("allow", "ask", "deny") and d.reason
    return d


def test_a_very_long_plain_command():
    assert go("echo " + "a" * 60000).action == "allow"            # under the cap: analysed normally
    assert go("echo " + "a" * (MAX_LENGTH + 10)).action == "ask"   # over the cap: cannot be analysed
    assert go("a" * 3_000_000).action == "ask"
    assert go("a" * 5_000_000).action == "deny"      # beyond the 4 MB input cap: malformed


def test_a_huge_number_of_commands():
    assert go("ls;" * 20000).action == "ask"
    assert go("ls && " * 20000 + "ls").action == "ask"
    assert go("\n".join(["ls"] * 50000)).action == "ask"
    assert go(" | ".join(["cat"] * 3000)).action == "ask"


@pytest.mark.parametrize("depth", [10, 13, 50, 1000, 20000])
def test_deeply_nested_substitutions(depth):
    d = go("$(" * depth + "ls" + ")" * depth)
    if depth > 12:
        assert d.action == "ask"
    d = go("echo " + "`" * (depth | 1))      # an odd number of backticks never closes
    assert d.action == "ask"
    d = go("(" * depth + "ls" + ")" * depth)
    assert d.action in ("ask", "allow")
    d = go("{ " * depth + "ls" + "; }" * depth)
    assert d.action in ("ask", "allow")


def test_nested_backticks_and_quotes():
    assert go("echo " + "$(echo " * 11 + "hi" + ")" * 11).action == "allow"
    assert go("echo " + "$(echo " * 13 + "hi" + ")" * 13).action == "ask"
    assert go('echo "' + '$(echo "' * 8 + "x" + '")' * 8 + '"').action in ("allow", "ask")


@pytest.mark.parametrize("cmd", [
    "echo 'x", 'echo "x', "echo `x", "echo $(x", "echo ${x", "echo $((x", "echo $'x", "echo \\", "echo '", 'echo "', "echo `",
    "echo $(", "echo ${", "ls &&", "ls ||", "ls |", "ls ;;", "( ls", "{ ls", "ls )", "ls >", "ls >>", "ls <", "ls 2>",
    "ls <<", "ls <<EOF", "cat <<EOF\nx", "echo x\\", "echo 'a' 'b", "(", ")", "{", "}", "((", "$(", "`", "'", '"', "\\", ";", "&", "|",
    "&&", "||", ">", "<", "<<", "${", "$'", "!", "# c", "ls # c\n(", "echo $(echo 'a)", "echo $(echo \"a)", "echo `echo 'a`",
    "'" * 10001, '"' * 10001, "`" * 10001, "(" * 10001, ")" * 10001, "$(" * 10001, "\\" * 10001, "<<" * 5001, ">" * 10001,
    "echo " + "'a' " * 10000 + "'",
])
def test_malformed_input_is_ask_even_under_allow_everything(cmd):
    d = go(cmd)
    assert d.action in ("ask", "deny"), (cmd[:60], d)


def test_unbalanced_input_is_never_allowed_in_any_mode():
    for mode in ("ask", "accept-edits"):
        for cmd in ("echo 'x", "ls && (", "echo $(ls", "ls &&", "echo `ls"):
            assert go(cmd, mode=mode).action != "allow"


@pytest.mark.parametrize("cmd", [
    "", " ", "\n", "\t\t", "\x00", "ls\x00", "\x00ls", "ls\x1b[31m", "ls\x07", "ls\r\nls", "ls\rcurl x", "﻿ls", "ls​",
    "lѕ", "‮ls", "ls ls", "echo \udcff" if False else "echo \xff", "ls " + "\U0001f600" * 1000,
])
def test_odd_characters(cmd):
    d = go(cmd)
    if cmd.strip() == "":
        assert d.action == "deny"
    if any(ord(c) < 32 and c not in "\t\n" for c in cmd) or "\x00" in cmd:
        assert d.action != "allow"


def test_lookalike_commands_are_not_the_allowed_command():
    lay = layers(allow=["bash(ls)"])
    assert go("ls", lay).action == "allow"
    assert go("ls ", lay).action == "allow"        # a plain trailing space is just whitespace
    looks = [
        "l" + chr(0x0455),                 # Cyrillic dze for s
        chr(0x217C) + chr(0x0455),         # small roman numeral fifty for l
        "ls" + chr(0x200B),                # zero-width space after
        chr(0x200B) + "ls",                # zero-width space before
        "ls" + chr(0x00A0),                # no-break space is not shell whitespace
        chr(0x00A0) + "ls",
        "LS", "l s", "ls" + chr(7), "ls" + chr(0x202E), "l" + chr(0x200D) + "s",
    ]
    for look in looks:
        assert go(look, lay).action != "allow", look.encode("unicode_escape")


def test_random_garbage_never_raises_and_never_allows_unparsed_input():
    rng = random.Random(424242)
    alphabet = string.printable + "\x00\x01\x1f\x7f" + "é中\U0001f600"
    t = time.perf_counter()
    for _ in range(3000):
        s = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        d = decide(Request("bash", {"command": s}, "R2"), ctx("ask", ALLOW_ALL))
        assert d.action in ("allow", "ask", "deny")
        r = parse_shell(s)
        if d.action == "allow":
            assert r.certain and r.parts, repr(s)  # an allow needs a clean parse
    assert time.perf_counter() - t < 20


def test_random_shell_fragments_never_raise():
    rng = random.Random(99)
    frags = ["ls", "'", '"', "`", "$(", ")", "(", "{", "}", ";", "&&", "||", "|", "&", ">", ">>", "<", "<<", "<<<", "<(", "${", "\\",
             "\n", " ", "#", "=", "FOO=", "*", "?", "[", "]", "~", "2>&1", "&>", "eval", "sh -c", "sudo", "env", "xargs", "find", "-exec", "{}"]
    for _ in range(5000):
        s = "".join(rng.choice(frags) for _ in range(rng.randint(1, 40)))
        d = decide(Request("bash", {"command": s}, "R2"), ctx("ask", ALLOW_ALL))
        assert d.action in ("allow", "ask", "deny")
        if d.action == "allow":
            assert parse_shell(s).certain, repr(s)


@pytest.mark.parametrize("path", ["a" * 4097, "a/" * 3000, "../" * 3000, "./" * 5000, "~" * 5000, "/" * 5000, "a\x00", "\x00", "\n", " ", "a b", "‮"])
def test_hostile_paths(path):
    for tool, inp in (("read_file", {"path": path}), ("edit_file", {"path": path, "old_string": "a", "new_string": "b"}),
                      ("delete_path", {"path": path}), ("list_dir", {"path": path}), ("glob", {"pattern": path}),
                      ("grep", {"pattern": "x", "path": path, "glob": path})):
        t = time.perf_counter()
        d = decide(Request(tool, inp, "R1"), ctx("accept-edits", ALLOW_ALL))
        assert time.perf_counter() - t < LIMIT_S
        assert d.action in ("allow", "ask", "deny")


def test_paths_over_the_length_cap_are_denied_and_long_escapes_ask():
    assert decide(Request("read_file", {"path": "../" * 3000}, "R0"), ctx()).action == "deny"
    assert decide(Request("read_file", {"path": "../" * 100 + "etc/passwd"}, "R0"), ctx()).action == "ask"
    assert decide(Request("read_file", {"path": "a/" * 100 + "b"}, "R0"), ctx()).action == "allow"


def test_large_but_valid_tool_inputs():
    big = "x" * 3_000_000
    assert decide(Request("write_file", {"path": "a.txt", "content": big}, "R1"), ctx("accept-edits")).action == "allow"
    assert decide(Request("write_file", {"path": "a.txt", "content": "x" * 5_000_000}, "R1"), ctx("accept-edits")).action == "deny"
    assert decide(Request("plan_exit", {"plan": big}, "R0"), ctx()).action == "ask"


def test_context_with_hostile_fields():
    for kw in ({"home": "relative"}, {"home": 5}, {"cwd": "relative"}, {"cwd": 5}, {"layers": None}, {"layers": "x"}, {"project_root": "/a/../.."}):
        c = Context(mode="ask", project_root=kw.pop("project_root", ROOT), **kw)
        d = decide(Request("read_file", {"path": "a"}, "R0"), c)
        assert d.action in ("ask", "deny") or kw == {}, (kw, d)


def test_rule_with_hostile_patterns_is_still_fast():
    lay = layers(allow=["bash(" + "*a" * 200 + "b)"], deny=["bash(" + "*" * 300 + "x)"])
    d = go("a" * 30000, lay)
    assert d.action == "ask"
