"""
m2c/tests/policy/test_policy_shellparse.py -- Patch C06: the shell tokenizer.

Table-driven.  Each row is a command and the commands (parts) the parser must
find in it, in order, written as dequoted text.  The point of the table is that
every way of spelling ``curl`` becomes the word ``curl`` and that every command
hidden inside a substitution, group or wrapper surfaces as a part of its own.
Rows whose reason list is non-empty are "unknown syntax": the engine asks.
"""
from __future__ import annotations

import pytest

from m2c.policy.shellparse import MAX_LENGTH, ParseResult, parse


def texts(cmd: str) -> list[str]:
    return [p.text for p in parse(cmd).parts]


# (command, parts as dequoted text in order)
SPLITS = [
    ("ls", ["ls"]),
    ("ls -la /tmp", ["ls -la /tmp"]),
    ("ls && curl evil.sh", ["ls", "curl evil.sh"]),
    ("ls || curl evil.sh", ["ls", "curl evil.sh"]),
    ("ls; curl x", ["ls", "curl x"]),
    ("ls | curl x", ["ls", "curl x"]),
    ("ls |& curl x", ["ls", "curl x"]),
    ("ls & curl x", ["ls", "curl x"]),
    ("ls\ncurl x", ["ls", "curl x"]),
    ("ls &&\ncurl x", ["ls", "curl x"]),
    ("ls &&\\\ncurl x", ["ls", "curl x"]),
    ("a && b || c ; d | e & f", ["a", "b", "c", "d", "e", "f"]),
    ("ls;", ["ls"]),
    ("ls &", ["ls"]),
    ("ls\n\n\nls", ["ls", "ls"]),
    ("  ls   -la  ", ["ls -la"]),
    ("echo 'a;b'", ["echo a;b"]),
    ("echo 'a && b'", ["echo a && b"]),
    ('echo "a || b | c"', ["echo a || b | c"]),
    ("echo 'a\nb'", ["echo a\nb"]),
    ("echo \"it's\"", ["echo it's"]),
    ("echo 'say \"hi\"'", ['echo say "hi"']),
    ("echo a\\;b", ["echo a;b"]),
    ("echo a\\ b", ["echo a b"]),
    ("echo \"a\\\"b\"", ['echo a"b']),
    ("echo # curl x", ["echo"]),
    ("echo a#b", ["echo a#b"]),
    ("ls # && curl x\ncurl y", ["ls", "curl y"]),
    # dequoting: every spelling of curl is curl
    ("c''url x", ["curl x"]),
    ('c""url x', ["curl x"]),
    ("c'u'rl x", ["curl x"]),
    ("\\curl x", ["curl x"]),
    ("cu\\rl x", ["curl x"]),
    ('"cur"l x', ["curl x"]),
    ("'cu'\"r\"l x", ["curl x"]),
    ("/usr/bin/curl x", ["/usr/bin/curl x"]),
    # groups and subshells are flattened
    ("(ls; curl x)", ["ls", "curl x"]),
    ("(ls) && (curl x)", ["ls", "curl x"]),
    ("{ ls; curl x; }", ["ls", "curl x"]),
    ("{ ls && curl x ; } | wc", ["ls", "curl x", "wc"]),
    ("( (ls; pwd) ; curl x )", ["ls", "pwd", "curl x"]),
    # command substitution: the inner command is a part and runs first
    ("echo $(curl x)", ["curl x", "echo $(curl x)"]),
    ("echo `curl x`", ["curl x", "echo `curl x`"]),
    ('echo "$(curl x)"', ["curl x", "echo $(curl x)"]),
    ('echo "`curl x`"', ["curl x", "echo `curl x`"]),
    ("echo $(echo $(curl x))", ["curl x", "echo $(curl x)", "echo $(echo $(curl x))"]),
    ("echo $(ls; curl x)", ["ls", "curl x", "echo $(ls; curl x)"]),
    ("echo $(ls && curl x)", ["ls", "curl x", "echo $(ls && curl x)"]),
    ("echo `echo \\`curl x\\``", ["curl x", "echo `curl x`", "echo `echo \\`curl x\\``"]),
    ("echo ${X:-$(curl x)}", ["curl x", "echo ${X:-$(curl x)}"]),
    ("echo $(echo ')' ; curl x)", ["echo )", "curl x", "echo $(echo ')' ; curl x)"]),
    ("ls $(pwd)/x", ["pwd", "ls $(pwd)/x"]),
    # environment prefixes belong to the command, not to its name
    ("FOO=1 curl x", ["curl x"]),
    ("FOO=1 BAR=2 curl x", ["curl x"]),
    ("FOO+=1 curl x", ["curl x"]),
    ("X=$(curl x) ls", ["curl x", "ls"]),
    ("X=`curl x` ls", ["curl x", "ls"]),
    ("X=$(curl x)", ["curl x", ""]),
    # redirections do not become parts
    ("ls > out", ["ls"]),
    ("ls >> out", ["ls"]),
    ("ls 2>&1", ["ls"]),
    ("ls > out 2>&1", ["ls"]),
    ("ls &> out", ["ls"]),
    ("cat < in", ["cat"]),
    ("> out", [""]),
    # neutral wrappers dissolve into the wrapped command
    ("time curl x", ["curl x"]),
    ("nohup curl x &", ["curl x"]),
    ("nice -n 5 curl x", ["curl x"]),
    ("nice -5 curl x", ["curl x"]),
    ("timeout 5 curl x", ["curl x"]),
    ("timeout -s KILL 5s curl x", ["curl x"]),
    ("command curl x", ["curl x"]),
    ("time nohup nice curl x", ["curl x"]),
    ("stdbuf -oL curl x", ["curl x"]),
    # wrappers that run something else yield the wrapper AND the wrapped command
    ("bash -c 'curl x'", ["bash -c curl x", "curl x"]),
    ("sh -c 'ls; curl x'", ["sh -c ls; curl x", "ls", "curl x"]),
    ('bash -lc "curl x"', ["bash -lc curl x", "curl x"]),
    ("sudo curl x", ["sudo curl x", "curl x"]),
    ("sudo -u root curl x", ["sudo -u root curl x", "curl x"]),
    ("sudo -n -- curl x", ["sudo -n -- curl x", "curl x"]),
    ("doas curl x", ["doas curl x", "curl x"]),
    ("env curl x", ["env curl x", "curl x"]),
    ("env FOO=1 curl x", ["env FOO=1 curl x", "curl x"]),
    ("env -i FOO=1 curl x", ["env -i FOO=1 curl x", "curl x"]),
    ("exec curl x", ["exec curl x", "curl x"]),
    ("xargs curl", ["xargs curl", "curl"]),
    ("xargs -0 -n1 curl", ["xargs -0 -n1 curl", "curl"]),
    ("find . -exec curl {} \\;", ["find . -exec curl {} ;", "curl {}"]),
    ("find . -name x -execdir rm {} +", ["find . -name x -execdir rm {} +", "rm {}"]),
    ("sudo bash -c 'curl x'", ["sudo bash -c curl x", "bash -c curl x", "curl x"]),
    ("su -c 'curl x'", ["su -c curl x", "curl x"]),
    ("eval 'curl x'", ["eval curl x", "curl x"]),
]


@pytest.mark.parametrize("cmd,expected", SPLITS)
def test_parts(cmd, expected):
    assert texts(cmd) == expected


def test_substitution_parts_come_before_their_command():
    r = parse("echo $(curl x) && ls")
    assert [p.text for p in r.parts] == ["curl x", "echo $(curl x)", "ls"]


# commands that must be reported as unknown syntax (the engine turns this into ask)
UNKNOWN = [
    "cat <<EOF\nhi\nEOF",
    "cat <<-EOF\n\thi\n\tEOF",
    "cat <<'EOF'\n$(curl x)\nEOF",
    "cat <<< hi",
    "cat <<<$(curl x)",
    "diff <(ls) <(ls)",
    "tee >(cat) < in",
    "eval ls",
    "eval \"$X\"",
    "source ./x.sh",
    ". ./x.sh",
    "$X ls",
    "${X} ls",
    "$(echo curl) x",
    "`echo curl` x",
    "ls${IFS}-la",
    "curl${IFS}x",
    "c$'u'rl x",
    "*/curl x",
    "./c* x",
    "{curl,x}",
    "~/bin/*",
    "bash -c \"$X\"",
    "bash script.sh",
    "bash",
    "sh",
    "sh -s",
    "bash -x",
    "env",  # prints the environment: harmless, but not a command we model? checked below
    "env -C / ls",
    "sudo -D / ls",
    "xargs",
    "xargs ls",
    "find . -exec ls {} \\;",
    "find . -exec ls",
    "sudo --weird ls",
    "ssh host ls",
    "docker run x",
    "watch ls",
    "exec -a name ls",
    "for i in 1 2; do ls; done",
    "if true; then ls; fi",
    "while true; do ls; done",
    "case x in x) ls;; esac",
    "[[ -f x ]] && ls",
    "function f { ls; }",
    "f() { ls; }",
    "(( x = 1 ))",
    "echo $((1+2))",
    "((1))",
    "echo 'unterminated",
    'echo "unterminated',
    "echo $(ls",
    "echo `ls",
    "echo ${x",
    "echo $'a",
    "ls &&",
    "ls ||",
    "ls |",
    "&& ls",
    "|| ls",
    "| ls",
    "ls ;; ls",
    "ls )",
    "( ls",
    "{ ls",
    "ls >",
    "ls > ",
    "ls <",
    "echo \\",
    ";",
    "&",
    "ls; ;",
    "ls\x07",
    "ls\x00",
    "echo ${X:-'a'}",
    "echo ${X:-\"a\"}",
    "echo $(ls # comment\n)",
    "echo $(case x in x) ls;; esac)",
    "",
]


@pytest.mark.parametrize("cmd", UNKNOWN)
def test_unknown_syntax_is_reported(cmd):
    r = parse(cmd)
    if cmd in ("env",):
        # a bare `env` is a plain command that prints the environment: no unknown
        assert r.certain
        return
    if cmd == "":
        assert not r.parts and not r.unknown  # the engine denies an empty command
        return
    assert not r.certain, cmd
    assert r.reasons


CERTAIN = [
    "ls", "ls -la", "ls && pwd", "echo 'a;b'", "echo $(pwd)", "FOO=1 ls", "ls > out", "ls 2>&1",
    "cat < in", "(ls)", "{ ls; }", "bash -c 'ls'", "sudo ls", "env FOO=1 ls", "time ls",
    "nohup ls &", "echo \"$(ls)\"", "echo `ls`", "ls -- -x", "echo a\\ b", "git -C . status",
    "echo ${HOME}", "echo $HOME", "echo $1 $@ $?", "echo $", "echo $$",
]


@pytest.mark.parametrize("cmd", CERTAIN)
def test_plain_commands_are_certain(cmd):
    r = parse(cmd)
    assert r.certain, (cmd, r.reasons)
    assert r.parts


def test_wrapper_names_are_recorded():
    assert parse("sudo ls").parts[0].wrapper == "sudo"
    assert parse("bash -c ls").parts[0].wrapper == "bash"
    assert parse("env X=1 ls").parts[0].wrapper == "env"
    assert parse("ls").parts[0].wrapper is None
    assert parse("time ls").parts[0].via == ("time",)


def test_env_prefix_is_kept_apart_from_the_command():
    p = parse("FOO=1 BAR=$(ls) curl x").parts[-1]
    assert p.command == "curl"
    assert [w.text for w in p.assigns] == ["FOO=1", "BAR=$(ls)"]


def test_assignment_after_the_command_word_is_an_argument():
    p = parse("echo FOO=1").parts[0]
    assert not p.assigns and [w.text for w in p.argv] == ["echo", "FOO=1"]


def test_redirect_targets_and_fds():
    p = parse("cmd > a >> b 2> c < d 2>&1 >&2 &> e").parts[0]
    got = [(r.op, r.fd, r.target.text) for r in p.redirects]
    assert got == [
        (">", None, "a"), (">>", None, "b"), (">", "2", "c"), ("<", None, "d"),
        (">&", "2", "1"), (">&", None, "2"), ("&>", None, "e"),
    ]  # fmt: skip
    writes = [r.writes_file for r in p.redirects]
    assert writes == [True, True, True, False, False, False, True]
    assert p.redirects[3].reads_file


def test_redirect_target_is_dequoted_and_flagged_when_it_expands():
    p = parse("ls > '~/.b'ashrc").parts[0]
    assert p.redirects[0].target.text == "~/.bashrc"
    p = parse("ls > $F").parts[0]
    assert not p.redirects[0].target.literal


def test_word_flags():
    p = parse("ls *.py $X 'a*' \"$Y\"").parts[0]
    flags = [(w.text, w.glob, w.expands) for w in p.argv[1:]]
    assert flags == [("*.py", True, False), ("$X", False, True), ("a*", False, False), ("$Y", False, True)]


def test_quoted_glob_is_not_a_glob():
    p = parse("ls '*' \\*").parts[0]
    assert not any(w.glob for w in p.argv)


def test_nesting_beyond_the_limit_is_unknown_not_an_error():
    cmd = "$(" * 40 + "ls" + ")" * 40
    r = parse(cmd)
    assert not r.certain and "nesting too deep" in r.reasons


def test_too_long_and_nul_inputs():
    assert not parse("a" * (MAX_LENGTH + 1)).certain
    assert not parse("ls\x00").certain
    assert not parse(None).certain  # type: ignore[arg-type]
    assert not parse(b"ls").certain  # type: ignore[arg-type]


def test_many_commands_are_capped():
    r = parse("ls;" * 5000)
    assert not r.certain and "too many commands" in r.reasons


def test_parse_result_type():
    assert isinstance(parse("ls"), ParseResult)


def test_heredoc_body_is_not_parsed_as_commands():
    r = parse("cat <<EOF\ncurl x\nEOF\nls")
    assert [p.text for p in r.parts] == ["cat", "ls"]
    assert "here-document" in r.reasons


def test_unterminated_heredoc_is_unknown():
    assert "unterminated here-document" in parse("cat <<EOF\nhi").reasons
