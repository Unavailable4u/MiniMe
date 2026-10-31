"""
m2c/src/m2c/policy/decide.py -- Patch C06: the policy decision function.

    decide(request, context) -> Decision(action, reason, matched_rule, ...)

``action`` is ``allow``, ``ask`` or ``deny``.  The function is PURE (no file or
network access, no clock, no logging, no printing) and TOTAL (it never raises:
any internal failure is a ``deny``).  This is the most security-critical module
of the client, so the whole design is "fail closed":

* An unknown tool, malformed arguments, an unknown mode or risk level, a bad
  project root: ``deny``.
* A shell command that cannot be classified with certainty: ``ask``.
* A settings layer that could not be read safely: nothing is ever ``allow``.
* ``allow`` exists in exactly two forms and the Decision says which in
  ``basis``: ``"rule"`` (an explicit ``allow`` rule matched, named in
  ``matched_rule``) or ``"mode"`` (the permission mode's default says allow for
  this tool and risk class; see modes.py, which never allows a shell command or
  anything above R1).  Every ``allow`` is created by one of two functions
  (``_allow_rule``, ``_allow_mode``) and passes ``_finalize``, which re-checks
  the basis and downgrades anything unexplained to ``deny``.
  tests/policy/test_policy_invariants.py proves this by reading this file.

Order of evaluation for one path or one shell part (first hit wins, and for a
whole shell command the WORST part wins: any deny -> deny, else any ask -> ask,
else allow only when every part is allowed):

 1. hard denies that no rule can override: the secrets denylist (private keys,
    ``.env*``, credential stores), and, for edits, writes, deletes and shell
    redirection/``tee``/``cp``-style targets, the protected paths (``.git/``,
    ``.m2c/``, shell start-up files);
 2. ``deny`` rules (managed, user and, only if trusted, project, as one union);
 3. plan mode: every mutation and every command is denied;
 4. ``ask`` rules;
 5. a path outside the project root (lexically, after ``..`` and ``~``): ask;
 6. ``allow`` rules (never for ``plan_exit``: leaving plan mode is always asked);
 7. the mode default.

Because deny and ask are checked before allow over the union of all layers, a
lower layer can never weaken a higher layer's deny or ask.

Paths are checked LEXICALLY (``posixpath.normpath`` on strings).  A symlink that
points out of the project looks "inside" here; C05's executors resolve real
paths and refuse it.  Both checks apply (defense in depth).  Likewise the
secrets denylist here only sees names the command spells out: C05 must refuse
the hard-denylisted files when ``grep``/``glob`` walk a directory.

Shell commands (bash) are split by shellparse.py into simple commands.  Each is
matched against the ``bash(...)`` rules in its dequoted form (so ``c'u'rl``,
``\\curl`` and ``"cur"l`` are all ``curl``; deny and ask rules also see the
base-name form, so ``/usr/bin/curl`` is ``curl``).  ``allow`` rules are
stricter: they match only the command as written, and never a command with an
environment-variable prefix (``PATH=... ls``) unless the rule names the prefix.
Redirections that write are checked as writes to their target; arguments of
known writers (``tee``, ``cp``, ``mv``, ``rm``, ``sed``, ``dd of=`` ...) are
checked as write targets; arguments of known readers (``cat``, ``ls``,
``grep`` ...) are checked as read targets.  A ``cd`` is followed lexically.
These checks only ever make a decision stricter than the rule that matched.

Known limits (by design; the sandbox, C11, is what contains the rest)
---------------------------------------------------------------------
Rules and checks here read the TEXT of a command.  They cannot see code that a
command carries or fetches: a script file, ``python -c``, ``make`` or ``npm run``
targets, an alias or function defined elsewhere, a variable that holds a command,
a program's own config.  An ``allow`` rule for an interpreter or build tool is
therefore a grant of arbitrary execution, and a ``deny bash(curl:*)`` stops the
text ``curl`` and the wrappers this engine models (see shellparse.py), not a
program that opens a socket by other means.  Launchers it does not model (a long
list is in shellparse._OPAQUE) are asked about, never allowed by a mode.  Programs
that take a script or command in an argument (sed, awk, rg ``--pre``, tar
``--to-command``, git ``-c`` ...) are inspected for the common ways to run a
program or write a file and ask when they find one; that list is not complete.
A wildcard allow rule never covers an argument that climbs with ``..``.  Deny and
ask rules also match flags in any order (``rm -fr`` for ``bash(rm -rf:*)``) and
the base name of a path-qualified command.  The lexical path checks cannot see
symlinks (C05 does) or case-folding filesystems beyond the protected-name lists.

``Context.tainted``: reserved for C17 (web tools and taint).  It is accepted
and has NO effect in this patch; C17 will make it tighten approvals.

``Context.non_interactive`` (headless runs, C10): nobody can answer a prompt, so
an ``ask`` becomes a ``deny`` ("approval needed but this run is
non-interactive").  An ``allow`` is unchanged.

Nothing here logs.  Command text and paths appear only inside the returned
Decision (``matched_rule`` is the rule's own text, never the command).

Standard library only.

Place this file at: m2c/src/m2c/policy/decide.py
"""
from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple, Sequence

from . import shellparse
from .modes import (
    ALLOW,
    ASK,
    DELETE,
    DENY,
    EDIT,
    PLAN_EXIT,
    READ,
    RISKS,
    SHELL,
    TOOLS,
    ToolSpec,
    is_valid_mode,
    max_risk,
    mode_default,
)
from .rules import (
    MergedRules,
    NormPath,
    Rule,
    RuleLayers,
    SECRET_SAMPLES,
    first_command_match,
    first_path_match,
    is_protected_path,
    is_secret_path,
    merge_layers,
    normalize_path,
)

__all__ = ["Context", "Decision", "Request", "decide"]

_RANK = {"deny": 2, "ask": 1, "allow": 0}
_BASES = ("rule", "mode")
_UNKNOWN_CWD = "/\x01unknown-cwd"

# bounds copied from protocol/v1/tools/*.json (a call outside them is malformed)
_BOUNDS = {
    "timeout_s": (1, 3600),
    "start_line": (1, None),
    "end_line": (1, None),
    "max_depth": (1, 8),
    "max_results": (1, 2000),
    "context_lines": (0, 10),
    "max_matches": (1, 1000),
}
_MAX_TEXT = 4 * 1024 * 1024


# --------------------------------------------------------------------------
# Public types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """A proposed tool call: ``tool``, its ``input`` object and the ``risk`` the
    server put on ``tool_proposed``.  ``decide`` also accepts a plain mapping
    with the same keys."""

    tool: Any
    input: Any
    risk: Any = "R0"


@dataclass(frozen=True)
class Context:
    """Everything the decision may depend on (all of it is data, none is read
    from the environment).

    mode             ``plan`` | ``ask`` | ``accept-edits``
    layers           the rule layers (managed, user, project) from rules.load_layers
    project_root     absolute path of the project (the session root)
    project_trusted  True only after the user trusted this folder; otherwise the
                     project layer is ignored
    non_interactive  nobody can approve: ``ask`` becomes ``deny``
    tainted          reserved for C17; accepted, documented, no effect yet
    home             the user's home directory, for ``~`` (None: ``~`` is
                     treated as somewhere outside the project)
    cwd              the directory a shell command starts in (default: the root)
    """

    mode: str = "ask"
    layers: RuleLayers = field(default_factory=RuleLayers)
    project_root: str = "/"
    project_trusted: bool = False
    non_interactive: bool = False
    tainted: bool = False
    home: str | None = None
    cwd: str | None = None


@dataclass(frozen=True)
class Decision:
    """The answer.  ``reason`` is human-readable and never contains secrets
    from settings; ``matched_rule`` is the text of the rule that decided it, or
    None; ``risk`` is the effective risk; ``basis`` says why an ``allow`` is
    allowed (``"rule"`` or ``"mode"``) and is None otherwise."""

    action: str
    reason: str
    matched_rule: str | None = None
    risk: str | None = None
    basis: str | None = None


class _V(NamedTuple):
    action: str
    reason: str
    rule: str | None = None
    basis: str | None = None


# --------------------------------------------------------------------------
# Verdict constructors.  "allow" may only be created by _allow_rule and
# _allow_mode; _finalize re-checks.  tests/policy/test_policy_invariants.py
# reads this file and fails if the allow action appears anywhere else.
# --------------------------------------------------------------------------


def _deny(reason: str, rule: str | None = None) -> _V:
    return _V("deny", reason, rule)


def _ask(reason: str, rule: str | None = None) -> _V:
    return _V("ask", reason, rule)


def _allow_rule(rule: Rule) -> _V:
    return _V("allow", f"allowed by rule {rule.layer}:{rule.raw}", rule.label, "rule")


def _allow_mode(reason: str) -> _V:
    return _V("allow", reason, None, "mode")


def _worst(vs: Sequence[_V]) -> _V:
    """Highest-ranked verdict (deny > ask > allow); the first one on a tie."""
    best = vs[0]
    for v in vs[1:]:
        if _RANK[v.action] > _RANK[best.action]:
            best = v
    return best


def _finalize(v: _V, ctx: Context, merged: MergedRules, risk: str | None) -> Decision:
    """The last stop of every decision: re-check the allow invariant, apply
    invalid settings and non-interactive runs, and build the Decision."""
    action, reason, rule, basis = v
    if action == "allow":
        explained = (basis == "rule" and bool(rule)) or basis == "mode"
        if not explained:
            action, reason, rule, basis = "deny", "internal check: allow without a rule or mode", None, None
        elif merged.invalid:
            action, reason, rule, basis = "ask", "a settings file could not be read safely, so nothing is auto-allowed", None, None
    if action != "allow":
        basis = None
    if action == "ask" and ctx.non_interactive:
        action, reason = "deny", "approval is needed but this run is non-interactive: " + reason
    return Decision(action, reason, rule, risk, basis)


# --------------------------------------------------------------------------
# Request validation
# --------------------------------------------------------------------------


def _type_ok(value: object, want: type) -> bool:
    if want is bool:
        return isinstance(value, bool)
    if want is int:
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, want)


def _validate(spec: ToolSpec, inp: object) -> str | None:
    """None when the input is well formed, else a short fixed reason."""
    if not isinstance(inp, dict):
        return "tool input is not an object"
    types = spec.arg_types
    for k in inp:
        if not isinstance(k, str) or k not in types:
            return "tool input has an unknown argument"
    for k in spec.required:
        if k not in inp:
            return "tool input is missing a required argument"
    for k, v in inp.items():
        if not _type_ok(v, types[k]):
            return "tool input has an argument of the wrong type"
        if isinstance(v, str) and len(v) > _MAX_TEXT:
            return "tool input argument is too large"
        if k in _BOUNDS:
            lo, hi = _BOUNDS[k]
            if (lo is not None and v < lo) or (hi is not None and v > hi):
                return "tool input argument is out of range"
    return None


def _client_risk(spec: ToolSpec, outside: bool) -> str:
    if spec.name == "delete_path":
        return "R4" if outside else "R1"
    return spec.risk


# --------------------------------------------------------------------------
# One path, one decision
# --------------------------------------------------------------------------


class _Env(NamedTuple):
    ctx: Context
    merged: MergedRules
    root: str
    eff: str  # effective risk of the request


def _path_verdict(env: _Env, tools: Sequence[str], tool_class: str, np: NormPath) -> _V:
    ctx, merged = env.ctx, env.merged
    mutating = tool_class in (EDIT, DELETE)
    if is_secret_path(np):
        return _deny("this path is on the hard denylist of secrets")
    if mutating and is_protected_path(np):
        return _deny("protected path (.git, .m2c, shell start-up files) is read-only")
    r = first_path_match(merged.deny, tools, np, ctx.home)
    if r is not None:
        return _deny(f"denied by rule {r.layer}:{r.raw}", r.label)
    if mutating and ctx.mode == "plan":
        return _deny("plan mode does not allow changes")
    if tool_class == DELETE and np.rel == ".":
        return _ask("this would delete the whole project and needs approval")
    r = first_path_match(merged.ask, tools, np, ctx.home)
    if r is not None:
        return _ask(f"rule {r.layer}:{r.raw} requires approval", r.label)
    if not np.inside:
        return _ask("path is outside the project and needs approval")
    r = first_path_match(merged.allow, tools, np, ctx.home, raw=False)
    if r is not None:
        return _allow_rule(r)
    md = mode_default(ctx.mode, tool_class, env.eff, inside_project=True, protected=False)
    if md.action == ALLOW:
        return _allow_mode(md.reason)
    if md.action == ASK:
        return _ask(md.reason)
    return _deny(md.reason)


def _norm(env: _Env, raw: object, cwd: str | None) -> NormPath | None:
    return normalize_path(raw, env.root, cwd=cwd or _UNKNOWN_CWD, home=env.ctx.home)


# --------------------------------------------------------------------------
# File tools
# --------------------------------------------------------------------------

_PATTERN_STOP = "*?[{"


def _pattern_dir(pattern: str) -> str | None:
    """The literal directory part of a glob pattern when it could leave the
    project (absolute, ``~`` or a ``..`` component); otherwise None."""
    parts = pattern.split("/")
    risky = pattern.startswith(("/", "~")) or ".." in parts
    if not risky:
        return None
    out: list[str] = []
    for comp in parts:
        if any(c in comp for c in _PATTERN_STOP):
            break
        out.append(comp)
    d = "/".join(out)
    return d if d else ("/" if pattern.startswith("/") else ".")


def _decide_file_tool(env: _Env, spec: ToolSpec, inp: dict) -> _V:
    cls = spec.tool_class
    name = spec.name
    if name in ("read_file", "edit_file", "write_file", "delete_path"):
        paths = [inp["path"]]
    else:  # list_dir, glob, grep: path is optional (default: the project root)
        paths = [inp.get("path", ".")]
    vs: list[_V] = []
    for raw in paths:
        np = _norm(env, raw, env.ctx.cwd or env.root)
        if np is None:
            return _deny("path is empty, too long or malformed")
        vs.append(_path_verdict(env, (name,), cls, np))
    extra = inp.get("pattern") if name == "glob" else inp.get("glob") if name == "grep" else None
    if isinstance(extra, str):
        if ".." in extra and any(c in extra for c in _PATTERN_STOP):
            vs.append(_ask("a pattern that mixes .. with wildcards may leave the project"))
        d = _pattern_dir(extra)
        if d is not None:
            np = _norm(env, d, env.ctx.cwd or env.root)
            if np is None:
                return _deny("path pattern is malformed")
            vs.append(_path_verdict(env, (name,), READ, np))
    return _worst(vs)


# --------------------------------------------------------------------------
# Shell
# --------------------------------------------------------------------------

_WRITERS = frozenset(
    {
        "tee", "cp", "mv", "rm", "ln", "install", "touch", "truncate", "chmod", "chown", "chgrp",
        "mkdir", "rmdir", "unlink", "shred", "dd", "sed", "perl", "awk", "gawk", "patch", "rsync",
        "scp", "mkfifo", "mknod", "split", "csplit", "ed", "ex", "vi", "vim", "nano", "emacs",
        "sponge", "fallocate", "setfacl", "chattr", "wget", "curl", "tar", "unzip", "zip", "gzip",
        "gunzip", "bzip2", "xz", "7z", "cpio", "python", "python3", "ruby", "node", "git",
        "sort", "uniq", "xxd", "tree", "find", "file", "column", "bat", "base64", "openssl",
    }
)  # fmt: skip
# `git` and the interpreters are here only so that a protected path in their
# arguments (git config .git/config, python -c ... .bashrc) is denied, not asked.
_READERS = frozenset(
    {
        "cat", "head", "tail", "less", "more", "ls", "wc", "file", "stat", "grep", "egrep", "fgrep",
        "rg", "diff", "cmp", "sort", "uniq", "nl", "tac", "od", "xxd", "hexdump", "strings", "du",
        "tree", "find", "readlink", "realpath", "md5sum", "sha1sum", "sha256sum", "sha512sum", "cut",
        "bat", "column", "fold", "paste", "comm", "join",
    }
)  # fmt: skip
_REMOVERS = frozenset({"rm", "rmdir", "unlink", "shred", "mv"})
_URL_SCHEMES = ("http://", "https://", "ftp://", "ftps://", "ssh://", "git://")
_REDIR_NOOP = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/fd/0", "/dev/fd/1", "/dev/fd/2"})

# Names a glob must not be able to reach in a command we cannot fully predict:
# the secrets denylist (rules.SECRET_SAMPLES) plus the protected paths.
_SENSITIVE_SAMPLES = SECRET_SAMPLES + (
    ".git", ".m2c", ".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile", ".zshenv", ".gitconfig",
)  # fmt: skip


def _glob_may_hit_sensitive(text: str) -> bool:
    comps = [c for c in text.split("/") if c]
    for comp in comps:
        if not any(ch in comp for ch in "*?["):
            continue
        for sample in _SENSITIVE_SAMPLES:
            if sample.startswith(".") and not comp.startswith("."):
                continue  # a shell glob does not match dotfiles unless asked to
            if fnmatch.fnmatchcase(sample, comp):
                return True
    return False


def _arg_candidates(w: shellparse.Word) -> list[str]:
    t = w.text
    if t == "" or t.startswith(_URL_SCHEMES):
        return []
    out: list[str] = []
    if t.startswith("-"):
        if t.startswith("--"):
            if "=" in t:
                out.append(t.split("=", 1)[1])
        elif len(t) > 2:
            # `-cf.git/config`: the value may start after any letter of the cluster
            for i in range(2, min(len(t), 42)):
                out.append(t[i:])
            if "=" in t:
                out.append(t.split("=", 1)[1])
    else:
        out.append(t)
        if "=" in t:
            out.append(t.split("=", 1)[1])
    more: list[str] = []
    for c in out:
        if c.startswith("@") and len(c) > 1:
            more.append(c[1:])             # curl -d @.env
        if ":" in c and not c.startswith(_URL_SCHEMES):
            more.append(c.rsplit(":", 1)[1])  # git show HEAD:.env
    return [c for c in out + more if c and not c.startswith(_URL_SCHEMES)]


# Options that make a program run another program, by program.  An option that
# matches makes the command ask even under an allow rule.
_EXEC_OPTION_PREFIXES: dict[str, tuple[str, ...]] = {
    "rg": ("--pre",),
    "tar": ("--to-command", "--checkpoint-action", "--use-compress-program", "-I", "--rsh-command"),
    "rsync": ("-e", "--rsh"),
    "scp": ("-S", "-o"),
    "sort": ("--compress-program",),
}
_AWKS = frozenset({"awk", "gawk", "mawk", "nawk"})
_AWK_RISKY = ("system", "getline", "|", ">", "@include", "@load", "inplace", "popen", "close(")


def _sed_script_is_risky(script: str) -> bool:
    """True unless ``script`` is made only of sed commands that cannot run a
    program or write a file.  A small scanner, not a regex: it understands
    addresses, ``s`` and ``y`` with any delimiter, and skips ``a i c`` text.
    The commands ``e w W r R`` and the ``s///e`` and ``s///w`` flags are risky,
    and so is anything it does not recognise."""
    i, n = 0, len(script)

    def skip_delimited(j: int, delim: str) -> int:
        while j < n:
            if script[j] == "\\":
                j += 2
            elif script[j] == delim:
                return j + 1
            else:
                j += 1
        return -1

    while i < n:
        c = script[i]
        if c in " \t\n;{}!":
            i += 1
            continue
        if c == "#":
            k = script.find("\n", i)
            i = n if k < 0 else k
            continue
        # optional addresses
        while i < n:
            c = script[i]
            if c.isdigit() or c in "$,~+":
                i += 1
            elif c == "/":
                i = skip_delimited(i + 1, "/")
                if i < 0:
                    return True
                while i < n and script[i] in "IM":
                    i += 1
            elif c == "\\" and i + 1 < n:
                i = skip_delimited(i + 2, script[i + 1])
                if i < 0:
                    return True
            else:
                break
        while i < n and script[i] in " \t!":
            i += 1
        if i >= n:
            break
        c = script[i]
        i += 1
        if c in "sy":
            if i >= n:
                return True
            delim = script[i]
            if delim in "\\\n":
                return True
            i = skip_delimited(i + 1, delim)
            if i < 0:
                return True
            i = skip_delimited(i, delim)
            if i < 0:
                return True
            while i < n and script[i] not in ";\n}":
                if script[i] in "ewWrR":
                    return True
                i += 1
        elif c in "aic":
            k = script.find("\n", i)
            i = n if k < 0 else k
        elif c in "btT:":
            while i < n and script[i] not in ";\n}":
                i += 1
        elif c in "qQlL":
            while i < n and script[i].isdigit() or i < n and script[i] == " ":
                i += 1
        elif c in "pPdDgGhHxnN=zF}":
            continue
        else:
            return True  # e w W r R v and anything unknown
    return False


_GIT_ASK_SUBS = frozenset({"submodule", "filter-branch", "difftool", "mergetool", "daemon", "instaweb", "gui", "bisect", "config"})
_GIT_READONLY_CONFIG = ("--get", "--get-all", "--get-regexp", "--list", "-l", "--show-origin", "--show-scope")
_GIT_ASK_OPTIONS = ("--exec", "--extcmd", "--ext-diff", "--upload-pack", "--receive-pack", "--open-files-in-pager",
                    "--config-env", "--exec-path", "--ext-cmd")  # fmt: skip
_BUILTINS_WITH_ARITHMETIC = frozenset({"declare", "typeset", "let", "printf", "test", "[", "export", "unset", "local",
                                       "read", "readonly", "set", "shopt", "mapfile", "readarray", "getopts"})  # fmt: skip
# variables that make the shell, a pager or a loader run something
_DANGEROUS_ENV = frozenset({
    "PS0", "PS1", "PS2", "PS4", "PROMPT_COMMAND", "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "BASH_XTRACEFD", "IFS",
    "PATH", "CDPATH", "GLOBIGNORE", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT", "DYLD_INSERT_LIBRARIES", "PAGER",
    "GIT_PAGER", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_ASKPASS", "SSH_ASKPASS", "EDITOR", "VISUAL",
    "GIT_EDITOR", "NODE_OPTIONS", "PYTHONSTARTUP", "PYTHONPATH", "PERL5OPT", "PERL5LIB", "RUBYOPT", "RUBYLIB", "MANPAGER",
})  # fmt: skip


def _assign_name(text: str) -> str:
    name = text.split("=", 1)[0]
    return name[:-1] if name.endswith("+") else name


def _hidden_code(name: str, part: shellparse.Part) -> list[_V]:
    """Strictness-only: programs that take code or a command in an argument."""
    out: list[_V] = []
    args = [w for w in part.argv[1:]]
    if any(not w.literal for w in args) and name in ("sed", "awk", "gawk", "mawk", "nawk"):
        out.append(_ask("the script of this program is built from an expansion"))
        return out
    texts = [w.text for w in args]
    if name == "sed":
        scripts: list[str] = []
        i = 0
        while i < len(texts):
            t = texts[i]
            if t in ("-e", "--expression") and i + 1 < len(texts):
                scripts.append(texts[i + 1])
                i += 2
                continue
            if t.startswith("--expression="):
                scripts.append(t.split("=", 1)[1])
            elif t == "--file" or t.startswith("--file=") or (t.startswith("-") and not t.startswith("--") and "f" in t[1:]):
                out.append(_ask("sed reads its script from a file"))
            elif not t.startswith("-") and not scripts and not any(x in ("-e", "--expression") for x in texts):
                scripts.append(t)
                i += 1
                break
            i += 1
        if any(_sed_script_is_risky(sc) for sc in scripts):
            out.append(_ask("this sed script can run a command or write a file"))
    elif name in _AWKS:
        if any(any(k in t for k in _AWK_RISKY) for t in texts) or any(
            t in ("-f", "-i", "-E") or t.startswith(("--file", "--exec", "--source", "-f")) for t in texts
        ):
            out.append(_ask("this awk program can run a command or write a file"))
    prefixes = _EXEC_OPTION_PREFIXES.get(name)
    if prefixes:
        for t in texts:
            if t.startswith(prefixes):
                out.append(_ask("an option of this program can run another program"))
                break
    elif name == "git":
        # global options come before the subcommand: `git -c core.pager=x log`
        i = 0
        while i < len(texts) and texts[i].startswith("-"):
            t = texts[i]
            if t == "-c" or t.startswith(("-c", "--exec-path", "--upload-pack", "--receive-pack", "--config-env", "--ext-cmd")):
                out.append(_ask("a git option can run another program"))
                break
            i += 2 if t in ("-C", "--git-dir", "--work-tree", "--namespace") else 1
        else:
            sub = texts[i] if i < len(texts) else ""
            rest = texts[i + 1 :]
            if sub in _GIT_ASK_SUBS and not (sub == "config" and rest and rest[0] in _GIT_READONLY_CONFIG):
                out.append(_ask("this git command can run a program or change .git/config"))
            elif sub == "remote" and rest and rest[0] not in ("-v", "--verbose", "show", "get-url"):
                out.append(_ask("this git command can change .git/config"))
            elif sub == "rebase" and any(t in ("-x", "--exec") or t.startswith(("-x", "--exec")) for t in rest):
                out.append(_ask("git rebase --exec runs a command"))
            elif sub in ("clone", "fetch", "ls-remote", "pull", "push") and any(t == "-u" and sub != "push" for t in rest):
                out.append(_ask("a git option can run another program"))
            elif sub == "grep" and any(t.startswith("-O") for t in rest):
                out.append(_ask("git grep -O runs a program"))
            if any(t.startswith(_GIT_ASK_OPTIONS) for t in rest):
                out.append(_ask("a git option can run another program"))
    return out


def _env_and_builtin_checks(env: _Env, name: str | None, part: shellparse.Part) -> list[_V]:
    """Strictness-only: code that rides in an environment variable or in the
    arithmetic of a shell builtin's argument."""
    out: list[_V] = []
    for w in part.assigns:
        if _assign_name(w.text) in _DANGEROUS_ENV:
            out.append(_ask("this sets an environment variable that can make a program run something"))
    if name in _BUILTINS_WITH_ARITHMETIC:
        for w in part.argv[1:]:
            if "$(" in w.text or "`" in w.text:
                out.append(_ask("a shell builtin evaluates this argument, which contains a command substitution"))
                break
        if name in ("export", "declare", "typeset", "local", "readonly"):
            for w in part.argv[1:]:
                if not w.text.startswith("-") and _assign_name(w.text) in _DANGEROUS_ENV:
                    out.append(_ask("this sets an environment variable that can make a program run something"))
                    break
        if name == "set" and any(
            (t.startswith("-") and not t.startswith("--") and "x" in t[1:]) or t == "xtrace" for t in (w.text for w in part.argv[1:])
        ):
            out.append(_ask("set -x makes the shell expand PS4 and print commands"))
    return out


def _denied_program_names(env: _Env) -> frozenset[str]:
    names = set()
    for r in env.merged.deny:
        if r.tool == "bash":
            first = r.pattern.split(" ")[0].split(":")[0]
            if first and "*" not in first:
                names.add(first.rsplit("/", 1)[-1])
    return frozenset(names)


def _denied_name_as_argument(env: _Env, part: shellparse.Part) -> list[_V]:
    """A launcher this engine does not know (``perf stat curl``, ``npx curl``)
    still has the program it launches in its arguments: if that name is one a
    deny rule names, ask instead of trusting an allow rule."""
    names = _denied_program_names(env)
    if not names:
        return []
    for w in part.argv[1:]:
        if w.literal and w.text.rsplit("/", 1)[-1] in names:
            return [_ask("an argument names a program that a deny rule forbids")]
    return []


def _script_words(name: str, part: shellparse.Part) -> set[int]:
    """Indexes (into argv) of the words of sed/awk that are a script, not a path.
    ``_hidden_code`` has already judged those."""
    if name not in ("sed", *_AWKS):
        return set()
    texts = [w.text for w in part.argv]
    out: set[int] = set()
    flag = ("-e", "--expression") if name == "sed" else ("-e", "--source")
    has_flag = any(t in flag for t in texts[1:])
    for i, t in enumerate(texts[1:], start=1):
        if i > 1 and texts[i - 1] in flag:
            out.add(i)
    if not has_flag:
        for i, t in enumerate(texts[1:], start=1):
            if not t.startswith("-"):
                out.add(i)
                break
    return out


def _scan_args(env: _Env, part: shellparse.Part, name: str, cwd: str | None) -> list[_V]:
    """Strictness-only checks of a command's arguments (see module docstring)."""
    out: list[_V] = list(_hidden_code(name, part))
    scripts = _script_words(name, part)
    writer = name in _WRITERS or (name == "find" and any(w.text == "-delete" for w in part.argv))
    reader = name in _READERS
    for idx, w in enumerate(part.argv[1:], start=1):
        if idx in scripts:
            continue
        if (w.expands or w.glob) and (writer or reader):
            if w.expands or writer or _glob_may_hit_sensitive(w.text):
                out.append(_ask("an argument expands at run time, so its target cannot be checked"))
        for cand in _arg_candidates(w):
            np = _norm(env, cand, cwd)
            if np is None:
                if writer or reader:  # a path-taking command with an unusable path
                    out.append(_ask("an argument is not a usable path"))
                continue
            if is_secret_path(np):
                out.append(_deny("an argument names a path on the hard denylist of secrets"))
            elif writer:
                if is_protected_path(np):
                    out.append(_deny("a protected path (.git, .m2c, shell start-up files) is read-only"))
                elif not np.inside:
                    out.append(_ask("a path outside the project needs approval"))
                elif np.rel == "." and name in _REMOVERS:
                    out.append(_ask("this would remove the whole project and needs approval"))
                else:
                    tools = ("edit_file", "write_file", "delete_path") if name == "rm" else ("edit_file", "write_file")
                    r = first_path_match(env.merged.deny, tools, np, env.ctx.home)
                    if r is not None:
                        out.append(_deny(f"denied by rule {r.layer}:{r.raw}", r.label))
                    elif _plan(env):
                        out.append(_deny("plan mode does not allow changes"))
            elif reader:
                if not np.inside:
                    out.append(_ask("a path outside the project needs approval"))
                else:
                    r = first_path_match(env.merged.deny, ("read_file",), np, env.ctx.home)
                    if r is not None:
                        out.append(_deny(f"denied by rule {r.layer}:{r.raw}", r.label))
            elif is_protected_path(np):
                out.append(_ask("an argument names a protected path"))
    return out


def _plan(env: _Env) -> bool:
    return env.ctx.mode == "plan"


def _redirect_verdicts(env: _Env, part: shellparse.Part, cwd: str | None) -> list[_V]:
    out: list[_V] = []
    for rd in part.redirects:
        if rd.op in ("<<", "<<-", "<<<"):
            continue  # already unknown on the part
        tgt = rd.target
        if tgt is None:
            out.append(_ask("a redirection has no target"))
            continue
        writes, reads = rd.writes_file, rd.reads_file
        if not (writes or reads):
            continue
        if not tgt.literal:
            out.append(_ask("a redirection target expands at run time"))
            continue
        if tgt.text in _REDIR_NOOP:
            continue
        np = _norm(env, tgt.text, cwd)
        if np is None:
            out.append(_ask("a redirection target is not a usable path"))
            continue
        # Judged as the edit or the read it is, at that tool's own risk, not at
        # the risk of the shell call that carries it (product guide 8.3).
        if writes:
            out.append(_path_verdict(env._replace(eff="R1"), ("edit_file", "write_file"), EDIT, np))
        if reads:
            out.append(_path_verdict(env._replace(eff="R0"), ("read_file",), READ, np))
    return out


def _part_verdict(env: _Env, part: shellparse.Part, cwd: str | None) -> _V:
    merged, ctx = env.merged, env.ctx
    vs: list[_V] = []
    name = part.name
    # --- the command itself against bash(...) rules --------------------------
    if part.argv:
        written = part.text
        base = " ".join([part.argv[0].text.rsplit("/", 1)[-1]] + [w.text for w in part.argv[1:]])
        looks = {written, base, part.full_text}
        r = first_command_match(merged.deny, "bash", looks, canon=True)
        if r is not None:
            vs.append(_deny(f"denied by rule {r.layer}:{r.raw}", r.label))
        else:
            r = first_command_match(merged.ask, "bash", looks, canon=True)
            if r is not None:
                vs.append(_ask(f"rule {r.layer}:{r.raw} requires approval", r.label))
            else:
                allow_text = part.full_text if part.assigns else written
                allow_rules = merged.allow
                if any(".." in w.text.split("/") for w in part.argv[1:]):
                    # `bash(rm build/*)` must not cover `rm build/../src/x`: with a
                    # `..` in play only a rule without a wildcard may allow it
                    allow_rules = tuple(a for a in merged.allow if "*" not in a.pattern)
                r = first_command_match(allow_rules, "bash", (allow_text,), word_bound=True)
                if r is not None:
                    vs.append(_allow_rule(r))
                else:
                    md = mode_default(ctx.mode, SHELL, env.eff, inside_project=True)
                    vs.append(_ask(md.reason) if md.action == ASK else _deny(md.reason))
    else:
        md = mode_default(ctx.mode, SHELL, env.eff, inside_project=True)
        vs.append(_ask("a command with no program (assignment or redirection only) needs approval")
                  if md.action == ASK else _deny(md.reason))
    if part.unknown:
        vs.append(_ask("unknown shell syntax: " + "; ".join(dict.fromkeys(part.unknown))))
    vs.extend(_env_and_builtin_checks(env, name, part))
    if name is not None:
        vs.extend(_scan_args(env, part, name, cwd))
        vs.extend(_denied_name_as_argument(env, part))
    vs.extend(_redirect_verdicts(env, part, cwd))
    return _worst(vs)


def _next_cwd(env: _Env, part: shellparse.Part, cwd: str | None) -> str | None:
    """Follow ``cd`` lexically.  None means "somewhere we cannot know"."""
    name = part.name
    if name in ("pushd", "popd"):
        return None
    if name != "cd":
        return cwd
    args = [w for w in part.argv[1:] if not (w.literal and w.text in ("-L", "-P", "-e", "-@", "--"))]
    if len(args) != 1 or not args[0].literal or args[0].text == "-":
        return None
    np = _norm(env, args[0].text, cwd)
    return None if np is None else np.abs


def _decide_shell(env: _Env, inp: dict) -> _V:
    command = inp["command"]
    if command.strip() == "":
        return _deny("the command is empty")
    result = shellparse.parse(command)
    vs: list[_V] = []
    cwd: str | None = env.ctx.cwd or env.root
    for part in result.parts:
        vs.append(_part_verdict(env, part, cwd))
        cwd = _next_cwd(env, part, cwd)
    if result.unknown:
        vs.append(_ask("unknown shell syntax: " + "; ".join(result.unknown)))
    if not result.parts and not result.unknown:
        vs.append(_ask("there is no command to analyse"))
    return _worst(vs)


# --------------------------------------------------------------------------
# plan_exit
# --------------------------------------------------------------------------


def _decide_plan_exit(env: _Env, inp: dict) -> _V:
    text = inp["plan"]
    r = first_command_match(env.merged.deny, "plan_exit", (text,))
    if r is not None:
        return _deny(f"denied by rule {r.layer}:{r.raw}", r.label)
    # No allow rule and no mode ever allows plan_exit: leaving plan mode is
    # always put to the user.
    return _ask("leaving plan mode needs the user's approval")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _unpack(request: object) -> tuple[Any, Any, Any]:
    if isinstance(request, Request):
        return request.tool, request.input, request.risk
    if isinstance(request, Mapping):
        return request.get("tool"), request.get("input"), request.get("risk", "R0")
    raise TypeError("request")


def decide(request: Request | Mapping[str, Any], context: Context) -> Decision:
    """Decide one proposed tool call.  Pure; never raises; fails closed."""
    try:
        return _decide(request, context)
    except Exception:  # noqa: BLE001 - RecursionError, MemoryError or a bug: deny
        return Decision("deny", "internal error while deciding (denied to be safe)")


def _decide(request: object, ctx: object) -> Decision:
    if not isinstance(ctx, Context):
        return Decision("deny", "the policy context is malformed")
    try:
        tool, inp, server_risk = _unpack(request)
    except TypeError:
        return Decision("deny", "the request is malformed")
    if not is_valid_mode(ctx.mode):
        return Decision("deny", "unknown permission mode")
    if not isinstance(server_risk, str) or server_risk not in RISKS:
        return Decision("deny", "the request carries an unknown risk level")
    spec = TOOLS.get(tool) if isinstance(tool, str) else None
    if spec is None:
        return Decision("deny", "unknown tool (not in the v0 catalog)", risk=None)
    bad = _validate(spec, inp)
    if bad is not None:
        return Decision("deny", bad)

    root = ctx.project_root
    if not isinstance(root, str) or not root.startswith("/") or "\x00" in root or len(root) > 4096:
        return Decision("deny", "the project root is not an absolute path")
    root = posixpath.normpath(root)
    if root.startswith("//"):
        root = "/" + root.lstrip("/")
    if root == "/":
        return Decision("deny", "the project root is the filesystem root")
    if ctx.home is not None and (not isinstance(ctx.home, str) or not ctx.home.startswith("/")):
        return Decision("deny", "the home directory is not an absolute path")
    if ctx.cwd is not None and (not isinstance(ctx.cwd, str) or not ctx.cwd.startswith("/")):
        return Decision("deny", "the working directory is not an absolute path")

    merged = merge_layers(ctx.layers, ctx.project_trusted) if isinstance(ctx.layers, RuleLayers) else MergedRules(invalid=True)

    # the effective risk: the HIGHER of the server's and the client's own
    outside = False
    if spec.name == "delete_path":
        np = normalize_path(inp.get("path"), root, cwd=ctx.cwd or root, home=ctx.home)
        outside = np is None or not np.inside
    eff = max_risk(server_risk, _client_risk(spec, outside))

    env = _Env(ctx, merged, root, eff)
    cls = spec.tool_class
    if cls == SHELL:
        if ctx.mode == "plan":
            v = _deny("plan mode does not allow commands")
        else:
            v = _decide_shell(env, inp)
    elif cls == PLAN_EXIT:
        v = _decide_plan_exit(env, inp)
    else:
        v = _decide_file_tool(env, spec, inp)
    return _finalize(v, ctx, merged, eff)
