"""
m2c/src/m2c/policy/shellparse.py -- Patch C06: a real tokenizer for the subset of
shell syntax the policy engine has to understand.

This is NOT a regex scorer (MiniLocker's engine, which this patch deliberately
does not port, scored command text with regexes and treated syntax it could not
parse as low risk).  It is a small hand-written scanner whose one rule is:

    Anything it cannot classify with certainty is "unknown syntax", and the
    engine turns unknown syntax into ``ask``.  It never turns it into ``allow``.

``parse(command)`` never raises.  It returns a ``ParseResult``: the simple
commands ("parts") found anywhere in the text, in execution order, plus the
reasons (fixed strings, never the command) why some of it is unknown.

What it understands
-------------------
* Separators ``&&  ||  ;  |  |&  &`` and newlines; a part is one simple command.
* Single quotes, double quotes and backslash escapes, and dequoting of words, so
  ``c'u'rl``, ``\\curl`` and ``"cur"l`` are all the word ``curl``.  Separators
  inside quotes do not split.
* Subshells ``( )`` and groups ``{ ... ; }`` (flattened: their commands are parts).
* Redirections ``> >> < >| <> &> &>> >& <&`` with an optional fd (``2>&1``) and
  the target word of each.
* Command substitution ``$(...)`` and backticks, nested to a fixed depth: the
  inner text is parsed and its commands become parts that run BEFORE the
  command that contains them.  Substitutions inside double quotes and inside
  ``${...}`` are found too.
* Environment-variable prefixes (``FOO=bar cmd``, ``FOO+=x cmd``), kept apart
  from the command word.
* Wrappers.  Neutral wrappers that add no authority (``time``, ``nohup``,
  ``nice``, ``timeout``, ``stdbuf``, ``setsid``, ``ionice``, ``command``,
  ``builtin``) are dissolved: the wrapped command is the part.  Wrappers that
  can run an arbitrary string or change authority (``sh -c``, ``bash -c``,
  ``env``, ``sudo``, ``doas``, ``su -c``, ``xargs``, ``find -exec``, ``exec``)
  yield the wrapper part AND the wrapped command as a second part, so both
  must be allowed.  If the wrapped command cannot be found with certainty the
  part is unknown.

What it marks unknown (so: ask)
-------------------------------
Here-documents and here-strings, process substitution, ``eval``, ``source``,
arithmetic expansion and commands, shell keywords (``for case function [[``),
control structures (the commands inside are still returned), a command word
built from an expansion or a glob (``$X``, ``$(...)``, ``*``, ``{a,b}``,
``$'..'``), unterminated quotes or substitutions, unbalanced ``( ) { }``,
dangling operators, control characters, ``${...}`` that contains quotes or
substitutions, wrappers whose arguments it does not understand, wrappers that
run an arbitrary program (``watch``, ``ssh``, ``docker``, ``flock``, ...), a
shell run without ``-c`` (a script or stdin), nesting beyond ``MAX_DEPTH``,
more than ``MAX_PARTS`` commands or input longer than ``MAX_LENGTH``.

Limits are fixed so that hostile input (a huge command, deeply nested
substitutions) returns quickly: ``parse`` is linear in the input apart from the
depth cap.  Standard library only.  No logging, no printing, no filesystem.

Place this file at: m2c/src/m2c/policy/shellparse.py
"""
from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "MAX_DEPTH",
    "MAX_LENGTH",
    "MAX_PARTS",
    "ParseResult",
    "Part",
    "Redirect",
    "Word",
    "parse",
]

MAX_LENGTH = 65536
MAX_DEPTH = 12
MAX_PARTS = 256
_MAX_WORK = 8 * MAX_LENGTH  # total characters parsed across all nesting levels

_NAME_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_STRONG = frozenset({"&&", "||", "|", "|&"})
_SPECIAL_PARAMS = frozenset("@*#?$!-0123456789")

# Reserved words that merely introduce a command: the command after them is
# still a part, but the construct around it is unknown.
_REDUCERS = frozenset({"if", "then", "else", "elif", "while", "until", "do"})
# Reserved words that cannot be analysed at all.
_KEYWORDS = frozenset({"for", "case", "select", "function", "coproc", "fi", "done", "esac", "[[", "((", "in"})
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh", "ash", "fish", "csh", "tcsh", "mksh", "rbash", "yash"})
# Programs whose job is to run another program or string in a way this module
# does not model.  Treated as unknown.
_OPAQUE = frozenset(
    {
        "watch", "script", "scriptreplay", "parallel", "flock", "chroot", "strace", "ltrace",
        "gdb", "valgrind", "taskset", "numactl", "fakeroot", "ssh", "docker", "podman",
        "kubectl", "bwrap", "nsenter", "unshare", "runuser", "setpriv", "busybox", "at",
        "batch", "crontab", "unbuffer", "proot", "nix-shell", "firejail", "systemd-run",
        "expect", "socat", "tmux", "screen", "dbus-launch", "gosu", "chpst", "start-stop-daemon",
        "chrt", "eatmydata", "xvfb-run", "sshpass", "setarch", "linux32", "linux64", "schedtool",
        "cgexec", "faketime", "torsocks", "proxychains", "proxychains4", "tsocks", "caffeinate",
        "runcon", "nsjail", "apptainer", "singularity", "lxc", "lxc-attach", "flatpak", "wine",
        "mpirun", "mpiexec", "sg", "newgrp", "run-parts", "trap", "alias", "coproc", "entr",
        "inotifywait", "inotifywatch", "parallel", "xdotool", "xterm", "gnome-terminal", "konsole",
        "env-update", "nsenter", "systemd-nspawn", "machinectl", "su-exec", "ts", "chronic", "vipe",
    }
)  # fmt: skip


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Word:
    """One shell word after dequoting.  ``text`` keeps unexpanded ``$...``
    exactly as written; ``expands``/``glob`` say the real value is unknown."""

    text: str
    expands: bool = False
    glob: bool = False
    quoted: bool = False
    is_assign: bool = False

    @property
    def literal(self) -> bool:
        return not self.expands and not self.glob


@dataclass(frozen=True)
class Redirect:
    op: str  # one of > >> < >| <> &> &>> >& <& << <<- <<<
    fd: str | None
    target: Word | None

    @property
    def writes_file(self) -> bool:
        """True when this redirection (probably) creates or truncates a file."""
        if self.op in (">", ">>", ">|", "<>", "&>", "&>>"):
            return True
        if self.op == ">&":
            t = self.target.text if self.target is not None else ""
            return not (t == "-" or (_ascii_digits(t) and self.target is not None and self.target.literal))
        return False

    @property
    def reads_file(self) -> bool:
        if self.op in ("<", "<>"):
            return True
        if self.op == "<&":
            t = self.target.text if self.target is not None else ""
            return not (t == "-" or _ascii_digits(t))
        return False


@dataclass(frozen=True)
class Part:
    """One simple command.

    ``argv`` is empty for a bare assignment or a redirection-only command.
    ``wrapper`` names the wrapper when this part is one that runs the next part
    (env, sudo, sh -c, xargs, find -exec); ``via`` lists neutral wrappers that
    were dissolved around this part.  ``unknown`` lists fixed-string reasons the
    part cannot be classified with certainty.
    """

    argv: tuple[Word, ...] = ()
    assigns: tuple[Word, ...] = ()
    redirects: tuple[Redirect, ...] = ()
    unknown: tuple[str, ...] = ()
    wrapper: str | None = None
    via: tuple[str, ...] = ()

    @property
    def command(self) -> str | None:
        return self.argv[0].text if self.argv else None

    @property
    def name(self) -> str | None:
        """Base name of the command word (``/usr/bin/curl`` -> ``curl``)."""
        c = self.command
        return None if c is None else c.rsplit("/", 1)[-1]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.argv)

    @property
    def full_text(self) -> str:
        return " ".join([w.text for w in self.assigns] + [w.text for w in self.argv])


@dataclass(frozen=True)
class ParseResult:
    parts: tuple[Part, ...] = ()
    unknown: tuple[str, ...] = ()

    @property
    def certain(self) -> bool:
        return not self.unknown and not any(p.unknown for p in self.parts)

    @property
    def reasons(self) -> tuple[str, ...]:
        out: list[str] = list(self.unknown)
        for p in self.parts:
            out.extend(p.unknown)
        return tuple(dict.fromkeys(out))


# --------------------------------------------------------------------------
# Low-level scanning helpers (iterative: no recursion on user input)
# --------------------------------------------------------------------------


def _ascii_digits(t: str) -> bool:
    """True for a non-empty run of ASCII digits (``str.isdigit`` also accepts
    superscripts and other scripts, which bash treats as file names)."""
    return t != "" and all(c in "0123456789" for c in t)


def _find_backtick(s: str, j: int) -> int:
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
        elif c == "`":
            return j
        else:
            j += 1
    return -1


def _find_ansi_quote(s: str, j: int) -> int:
    n = len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
        elif c == "'":
            return j
        else:
            j += 1
    return -1


def _find_close(s: str, start: int) -> tuple[int, bool]:
    """Index of the ``)`` that closes a ``$(`` whose body starts at ``start``,
    or -1.  Quote- and nesting-aware.  The bool is True when the body holds
    something this scanner may read differently from a real shell (a comment or
    a ``case`` pattern), so the caller must mark the command unknown."""
    stack = [")"]
    j, n = start, len(s)
    ambiguous = False
    while j < n:
        top = stack[-1]
        c = s[j]
        if top == '"':
            if c == "\\":
                j += 2
            elif c == '"':
                stack.pop()
                j += 1
            elif c == "$" and s[j + 1 : j + 2] == "(":
                stack.append(")")
                j += 2
            elif c == "$" and s[j + 1 : j + 2] == "{":
                stack.append("}")
                j += 2
            elif c == "`":
                k = _find_backtick(s, j + 1)
                if k < 0:
                    return -1, ambiguous
                j = k + 1
            else:
                j += 1
            continue
        if c == "\\":
            j += 2
        elif c == "'":
            k = s.find("'", j + 1)
            if k < 0:
                return -1, ambiguous
            j = k + 1
        elif c == '"':
            stack.append('"')
            j += 1
        elif c == "`":
            k = _find_backtick(s, j + 1)
            if k < 0:
                return -1, ambiguous
            j = k + 1
        elif c == "$" and s[j + 1 : j + 2] == "(":
            stack.append(")")
            j += 2
        elif c == "$" and s[j + 1 : j + 2] == "{":
            stack.append("}")
            j += 2
        elif c == "(" and top == ")":
            stack.append(")")
            j += 1
        elif c == top:
            stack.pop()
            if not stack:
                return j, ambiguous
            j += 1
        else:
            if c == "#" and (j == start or s[j - 1] in " \t\n;&|(") or (
                c == "c" and s.startswith("case", j) and (j == start or s[j - 1] in " \t\n;&|(")
            ):
                ambiguous = True
            j += 1
    return -1, ambiguous


def _find_brace_close(s: str, start: int) -> int:
    """Close of ``${``: counts only ``{`` and ``}`` (quotes are judged by the
    caller, who marks anything with a quote in it unknown)."""
    depth, j, n = 1, start, len(s)
    while j < n:
        c = s[j]
        if c == "\\":
            j += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return j
        j += 1
    return -1


def _unescape_backtick(inner: str, in_dq: bool) -> str:
    out: list[str] = []
    j, n = 0, len(inner)
    special = "`\\$\"" if in_dq else "`\\$"
    while j < n:
        c = inner[j]
        if c == "\\" and j + 1 < n and inner[j + 1] in special:
            out.append(inner[j + 1])
            j += 2
        else:
            out.append(c)
            j += 1
    return "".join(out)


class _WordBuilder:
    __slots__ = ("buf", "expands", "glob", "quoted", "brace", "plain", "eq")

    def __init__(self) -> None:
        self.buf: list[str] = []
        self.expands = False
        self.glob = False
        self.quoted = False
        self.brace = False
        self.plain = True  # every char so far is part of an unquoted NAME
        self.eq: int | None = None

    def add_plain(self, c: str, nxt: str) -> None:
        if c in "*?[":
            self.glob = True
        elif c in "{}":
            self.brace = True
        if self.eq is None and self.plain:
            if c in _NAME_CHARS:
                if not self.buf and c.isdigit():
                    self.plain = False
            elif c == "=" and self.buf:
                self.eq = len(self.buf)
                self.plain = False
            elif c == "+" and nxt == "=" and self.buf:
                pass
            else:
                self.plain = False
        self.buf.append(c)

    def add_quoted(self, text: str) -> None:
        self.quoted = True
        if self.eq is None:
            self.plain = False
        self.buf.append(text)

    def add_expansion(self, text: str) -> None:
        self.expands = True
        if self.eq is None:
            self.plain = False
        self.buf.append(text)

    def build(self) -> Word:
        text = "".join(self.buf)
        expands = self.expands or (self.brace and text not in ("{", "}"))
        glob = self.glob and text != "["
        return Word(text, expands, glob, self.quoted, self.eq is not None)


# --------------------------------------------------------------------------
# The parser
# --------------------------------------------------------------------------


class _Budget:
    __slots__ = ("parts", "work")

    def __init__(self) -> None:
        self.parts = 0
        self.work = 0


class _Parser:
    def __init__(self, s: str, depth: int, budget: _Budget) -> None:
        self.s = s
        self.n = len(s)
        self.i = 0
        self.depth = depth
        self.budget = budget
        self.parts: list[Part] = []
        self.unknown: list[str] = []
        # the command being built
        self.words: list[Word] = []
        self.assigns: list[Word] = []
        self.redirs: list[Redirect] = []
        self.cur_unknown: list[str] = []
        self.pending: tuple[str, str | None] | None = None
        self.last_op: str | None = None
        self.groups: list[str] = []
        self.heredocs: list[tuple[str, bool, bool]] = []
        self.last_word_end = -1
        self.last_word: Word | None = None
        self.ctl_seen = False

    # -- bookkeeping -------------------------------------------------------

    def unk(self, reason: str) -> None:
        self.unknown.append(reason)

    def cunk(self, reason: str) -> None:
        self.cur_unknown.append(reason)

    def _emit(self, part: Part) -> None:
        self.budget.parts += 1
        if self.budget.parts > MAX_PARTS:
            if "too many commands" not in self.unknown:
                self.unk("too many commands")
            return
        self.parts.append(part)

    # -- driver ------------------------------------------------------------

    def run(self) -> _Parser:
        self.budget.work += self.n
        if self.budget.work > _MAX_WORK:
            self.unk("input too complex")
            return self
        s, n = self.s, self.n
        if any((ord(c) < 32 and c not in "\t\n") or ord(c) == 127 for c in s):
            self.unk("control character in command")
        while self.i < n:
            if self.budget.parts > MAX_PARTS:
                break
            c = s[self.i]
            nxt = s[self.i + 1 : self.i + 2]
            if c in " \t":
                self.i += 1
            elif c == "\\" and nxt == "\n":
                self.i += 2
            elif c == "\n":
                self.i += 1
                self._end("newline")
                self._skip_heredoc_bodies()
            elif c == "#":
                k = s.find("\n", self.i)
                self.i = n if k < 0 else k
            elif c == ";":
                if nxt in (";", "&"):
                    self.unk("case terminator")
                    self.i += 2 + (1 if s[self.i + 2 : self.i + 3] == "&" else 0)
                else:
                    self.i += 1
                self._end(";")
            elif c == "&":
                if nxt == "&":
                    self.i += 2
                    self._end("&&")
                elif nxt == ">":
                    op = "&>>" if s[self.i + 2 : self.i + 3] == ">" else "&>"
                    self.i += len(op)
                    self._begin_redirect(op, None)
                else:
                    self.i += 1
                    self._end("&")
            elif c == "|":
                if nxt == "|":
                    self.i += 2
                    self._end("||")
                elif nxt == "&":
                    self.i += 2
                    self._end("|&")
                else:
                    self.i += 1
                    self._end("|")
            elif c == "(":
                self._open_paren()
            elif c == ")":
                self.i += 1
                if not self.groups or self.groups[-1] != "(":
                    self.unk("unbalanced )")
                    self._end(";")
                else:
                    self._close(")")
            elif c in "<>":
                self._redirect_or_procsub()
            else:
                w = self._word()
                self._add_word(w)
        self._end("eof")
        if self.pending is not None:
            self.unk("redirection without a target")
        if self.groups:
            self.unk("unbalanced ( or {")
        if self.heredocs:
            self.unk("unterminated here-document")
        return self

    # -- command assembly --------------------------------------------------

    def _at_command_start(self) -> bool:
        return not (self.words or self.assigns or self.redirs or self.pending)

    def _open_paren(self) -> None:
        prev_paren = self.i > 0 and self.s[self.i - 1] == "("
        self.i += 1
        if not self._at_command_start():
            self.unk("unexpected ( inside a command")
            return
        if prev_paren:
            self.unk("arithmetic command")
        self.groups.append("(")
        self.last_op = "("

    def _close(self, kind: str) -> None:
        if self.pending is not None:
            self.unk("redirection without a target")
            self.pending = None
        has = bool(self.words or self.assigns or self.redirs)
        if has:
            self._flush()
        elif self.last_op in _STRONG or self.last_op in ("(", "{"):
            self.unk("empty or incomplete group")
        if self.groups:
            self.groups.pop()
        self.last_op = kind

    def _end(self, op: str) -> None:
        if self.pending is not None:
            self.unk("redirection without a target")
            self.pending = None
        has = bool(self.words or self.assigns or self.redirs)
        if not has:
            if op in ("newline", "eof"):
                if op == "eof" and self.last_op in _STRONG:
                    self.unk("incomplete command")
                return
            if self.last_op in (")", "}"):
                self.last_op = op
                return
            self.unk("empty command")
            self.last_op = op
            return
        self._flush()
        self.last_op = op

    def _add_word(self, w: Word) -> None:
        self.last_word_end = self.i
        if self.pending is not None:
            op, fd = self.pending
            self.pending = None
            self.redirs.append(Redirect(op, fd, w))
            self.last_word = None
            if op in ("<<", "<<-"):
                self.heredocs.append((w.text, op == "<<-", w.quoted))
                self.cunk("here-document")
            elif op == "<<<":
                self.cunk("here-string")
            return
        if self._at_command_start() and not w.quoted and not w.expands and not w.glob:
            if w.text == "{":
                self.groups.append("{")
                self.last_op = "{"
                return
            if w.text == "}":
                if self.groups and self.groups[-1] == "{":
                    self._close("}")
                else:
                    self.unk("unbalanced }")
                return
        if not self.words and w.is_assign:
            self.assigns.append(w)
            self.last_word = None
            return
        self.words.append(w)
        self.last_word = w

    def _flush(self) -> None:
        words, assigns, redirs, unknown = self.words, self.assigns, self.redirs, self.cur_unknown
        self.words, self.assigns, self.redirs, self.cur_unknown = [], [], [], []
        self.last_word = None
        while words and not assigns and not words[0].quoted and words[0].literal:
            t = words[0].text
            if t == "!":
                words = words[1:]
            elif t in _REDUCERS:
                unknown.append("compound command")
                words = words[1:]
            else:
                break
        words, assigns = self._peel_assignments(words, assigns)
        self._classify(tuple(words), tuple(assigns), tuple(redirs), list(unknown), ())

    @staticmethod
    def _peel_assignments(words, assigns):
        """``! x=1 cmd`` and ``time x=1 cmd``: once ``!`` or a wrapper is gone, the
        leading NAME=value words are assignments, not the command."""
        words = list(words)
        assigns = list(assigns)
        while words and words[0].is_assign and not words[0].quoted:
            assigns.append(words.pop(0))
        return words, assigns

    # -- redirections ------------------------------------------------------

    _REDIR_OPS = ("<<<", "<<-", "<<", "<>", "<&", "<", ">>", ">&", ">|", ">")

    def _redirect_or_procsub(self) -> None:
        s, i = self.s, self.i
        if s[i + 1 : i + 2] == "(":
            k, amb = _find_close(s, i + 2)
            if k < 0:
                self.unk("unterminated process substitution")
                self.i = self.n
                return
            self.cunk("process substitution")
            if amb:
                self.cunk("comment or case inside a substitution")
            self._sub(s[i + 2 : k])
            self.i = k + 1
            self._add_word(Word(s[i : k + 1], expands=True))
            return
        op = next(o for o in self._REDIR_OPS if s.startswith(o, i))
        fd = None
        lw = self.last_word
        if (
            lw is not None
            and self.last_word_end == i
            and self.words
            and self.words[-1] is lw
            and _ascii_digits(lw.text)
            and not lw.quoted
        ):
            fd = lw.text
            self.words.pop()
            self.last_word = None
        self.i = i + len(op)
        self._begin_redirect(op, fd)

    def _begin_redirect(self, op: str, fd: str | None) -> None:
        if self.pending is not None:
            self.unk("redirection without a target")
        self.pending = (op, fd)

    def _skip_heredoc_bodies(self) -> None:
        s, n = self.s, self.n
        for delim, strip, quoted in self.heredocs:
            found = False
            body: list[str] = []
            while self.i < n:
                k = s.find("\n", self.i)
                line = s[self.i : n if k < 0 else k]
                self.i = n if k < 0 else k + 1
                if (line.lstrip("\t") if strip else line) == delim:
                    found = True
                    break
                body.append(line)
            if not found:
                self.unk("unterminated here-document")
            if not quoted:
                # an unquoted delimiter lets the shell expand $(...) and `...` in
                # the body, so those commands really run: surface them as parts
                self._nested_subs("\n".join(body))
        self.heredocs = []

    # -- words -------------------------------------------------------------

    def _ctl(self) -> None:  # placeholder kept for symmetry; control chars are checked once in run()
        return

    def _word(self) -> Word:
        s, n = self.s, self.n
        wb = _WordBuilder()
        while self.i < n:
            c = s[self.i]
            if c in " \t\n;&|<>()":
                break
            if c == "\\":
                nxt = s[self.i + 1 : self.i + 2]
                if nxt == "":
                    self.unk("trailing backslash")
                    self.i += 1
                    break
                if nxt == "\n":
                    self.i += 2
                    continue
                wb.add_quoted(nxt)
                self.i += 2
            elif c == "'":
                k = s.find("'", self.i + 1)
                if k < 0:
                    self.unk("unterminated single quote")
                    wb.add_quoted(s[self.i + 1 :])
                    self.i = n
                    break
                wb.add_quoted(s[self.i + 1 : k])
                self.i = k + 1
            elif c == '"':
                self._dq(wb)
            elif c == "`":
                self._backtick(wb, False)
            elif c == "$":
                self._dollar(wb, False)
            else:
                wb.add_plain(c, s[self.i + 1 : self.i + 2])
                self.i += 1
        return wb.build()

    def _dq(self, wb: _WordBuilder) -> None:
        s, n = self.s, self.n
        wb.quoted = True
        if wb.eq is None:
            wb.plain = False
        self.i += 1
        while self.i < n:
            c = s[self.i]
            if c == '"':
                self.i += 1
                return
            if c == "\\":
                nxt = s[self.i + 1 : self.i + 2]
                if nxt == "":
                    break
                if nxt == "\n":
                    self.i += 2
                elif nxt in '$`"\\':
                    wb.add_quoted(nxt)
                    self.i += 2
                else:
                    wb.add_quoted("\\" + nxt)
                    self.i += 2
            elif c == "$":
                self._dollar(wb, True)
            elif c == "`":
                self._backtick(wb, True)
            else:
                wb.add_quoted(c)
                self.i += 1
        self.unk("unterminated double quote")
        self.i = n

    def _backtick(self, wb: _WordBuilder, in_dq: bool) -> None:
        s = self.s
        k = _find_backtick(s, self.i + 1)
        if k < 0:
            self.unk("unterminated backtick substitution")
            wb.add_expansion(s[self.i :])
            self.i = self.n
            return
        wb.add_expansion(s[self.i : k + 1])
        self._sub(_unescape_backtick(s[self.i + 1 : k], in_dq))
        self.i = k + 1

    def _dollar(self, wb: _WordBuilder, in_dq: bool) -> None:
        s, n, j = self.s, self.n, self.i
        nxt = s[j + 1 : j + 2]
        if nxt == "\\" and s[j + 2 : j + 3] == "\n":
            # line continuation after the dollar sign: `$ BACKSLASH NEWLINE (cmd)`: the shell joins the lines and runs cmd
            self.unk("line continuation inside an expansion")
        if nxt == "(":
            k, amb = _find_close(s, j + 2)
            if k < 0:
                self.unk("unterminated command substitution")
                wb.add_expansion(s[j:])
                self.i = n
                return
            if s[j + 2 : j + 3] == "(":
                self.cunk("arithmetic expansion")
            if amb:
                self.cunk("comment or case inside a substitution")
            wb.add_expansion(s[j : k + 1])
            self._sub(s[j + 2 : k])
            self.i = k + 1
        elif nxt == "{":
            k = _find_brace_close(s, j + 2)
            if k < 0:
                self.unk("unterminated parameter expansion")
                wb.add_expansion(s[j:])
                self.i = n
                return
            body = s[j + 2 : k]
            if body.startswith("!") or "@P" in body:
                self.cunk("indirect or prompt expansion")
            if "[" in body:
                sub = body[body.index("[") + 1 :].split("]", 1)[0]
                if sub not in ("@", "*") and not _ascii_digits(sub):
                    self.cunk("array subscript evaluated as arithmetic")
            wb.add_expansion(s[j : k + 1])
            if any(ch in body for ch in "'\"`\\") or "$(" in body:
                self.cunk("complex parameter expansion")
                self._nested_subs(body)
            self.i = k + 1
        elif nxt == "[":
            self.cunk("arithmetic expansion")
            wb.add_expansion("$")
            self.i = j + 1
        elif nxt == "'" and not in_dq:
            k = _find_ansi_quote(s, j + 2)
            if k < 0:
                self.unk("unterminated $'...' string")
                wb.add_expansion(s[j:])
                self.i = n
                return
            wb.add_expansion(s[j : k + 1])
            self.i = k + 1
        elif nxt == '"' and not in_dq:
            wb.add_expansion("$")
            self.i = j + 1
        elif nxt != "" and (nxt in _NAME_CHARS and not nxt.isdigit() or nxt in _SPECIAL_PARAMS):
            if nxt in _NAME_CHARS and not nxt.isdigit():
                e = j + 1
                while e < n and s[e] in _NAME_CHARS:
                    e += 1
            else:
                e = j + 2
            wb.add_expansion(s[j:e])
            self.i = e
        else:
            if in_dq:
                wb.add_quoted("$")
            else:
                wb.add_plain("$", nxt)
            self.i = j + 1

    def _nested_subs(self, text: str) -> None:
        """Find ``$(...)`` and backticks anywhere in ``text`` (quotes ignored on
        purpose: over-finding is safe, under-finding is not)."""
        j, n = 0, len(text)
        while j < n:
            c = text[j]
            if c == "\\":
                j += 2
            elif c == "$" and text[j + 1 : j + 2] == "(":
                k, _ = _find_close(text, j + 2)
                if k < 0:
                    self.unk("unterminated command substitution")
                    return
                self._sub(text[j + 2 : k])
                j = k + 1
            elif c == "`":
                k = _find_backtick(text, j + 1)
                if k < 0:
                    self.unk("unterminated backtick substitution")
                    return
                self._sub(_unescape_backtick(text[j + 1 : k], False))
                j = k + 1
            else:
                j += 1

    def _sub(self, inner: str, *, wrapped: bool = False) -> None:
        """Parse ``inner`` as a command list; its parts join ours."""
        if self.depth + 1 > MAX_DEPTH:
            self.unk("nesting too deep")
            return
        child = _Parser(inner, self.depth + 1, self.budget).run()
        self.parts.extend(child.parts)
        self.unknown.extend(child.unknown)

    # -- classification and wrappers ----------------------------------------

    def _classify(self, argv, assigns, redirs, unknown, via) -> None:
        if not argv:
            self._emit(Part((), assigns, redirs, tuple(unknown), None, via))
            return
        head = argv[0]
        if not head.literal:
            unknown.append("command word built from an expansion or glob")
            self._emit(Part(argv, assigns, redirs, tuple(unknown), None, via))
            return
        if head.text == "":
            unknown.append("empty command word")
            self._emit(Part(argv, assigns, redirs, tuple(unknown), None, via))
            return
        name = head.text.rsplit("/", 1)[-1]
        rest = list(argv[1:])

        def plain(extra: str | None = None, wrapper: str | None = None) -> Part:
            u = list(unknown)
            if extra:
                u.append(extra)
            return Part(argv, assigns, redirs, tuple(u), wrapper, via)

        if name in _KEYWORDS:
            self._emit(plain("shell keyword"))
        elif name == "eval":
            self._emit(plain("eval"))
            if rest and all(w.literal for w in rest):
                self._wrapped_string(" ".join(w.text for w in rest))
        elif name in ("source", "."):
            self._emit(plain("sourcing a script"))
        elif name in _OPAQUE:
            self._emit(plain("runs another program"))
        elif "/" not in head.text and (
            name in ("time", "nohup", "nice", "ionice", "setsid", "stdbuf", "timeout", "builtin")
            or (name == "command" and not (rest and rest[0].text in ("-v", "-V")))
        ):
            inner = self._neutral(name, rest)
            while inner and inner[0].literal and not inner[0].quoted and inner[0].text == "!":
                inner = inner[1:]  # `time ! cmd`: the shell negates cmd, it still runs
            if inner:
                inner, assigns = self._peel_assignments(inner, assigns)
                assigns = tuple(assigns)
            if inner is None:
                self._emit(plain("wrapper options not understood"))
            elif not inner:
                self._emit(plain())
            elif len(via) >= MAX_DEPTH:
                self._emit(plain("nesting too deep"))
            else:
                self._classify(tuple(inner), assigns, redirs, unknown, via + (name,))
        elif name in _SHELLS:
            self._shell(name, argv, rest, plain)
        elif name == "su":
            self._su(rest, plain)
        elif name == "env":
            self._env(argv, assigns, rest, plain)
        elif name in ("sudo", "doas", "pkexec"):
            self._privilege(name, rest, plain)
        elif name == "xargs":
            self._xargs(rest, plain)
        elif name == "find":
            self._find(argv, plain)
        elif name == "exec":
            if not rest:
                self._emit(plain())
            elif rest[0].text.startswith("-"):
                self._emit(plain("wrapper options not understood", "exec"))
            else:
                self._emit(plain(None, "exec"))
                self._wrapped_argv(rest)
        else:
            self._emit(plain())

    def _wrapped_argv(self, words: list[Word], extra_unknown: str | None = None, assigns: tuple = ()) -> None:
        if not words:
            return
        if self.depth + 1 > MAX_DEPTH:
            self.unk("nesting too deep")
            return
        child = _Parser("", self.depth + 1, self.budget)
        child._classify(tuple(words), assigns, (), [extra_unknown] if extra_unknown else [], ("wrapped",))
        self.parts.extend(child.parts)
        self.unknown.extend(child.unknown)

    def _wrapped_string(self, text: str) -> None:
        self._sub(text)

    # option skipping shared by the wrappers
    @staticmethod
    def _skip_options(rest, *, flags="", with_arg="", long_flags=(), long_eq=(), long_arg=()):
        i, n = 0, len(rest)
        while i < n:
            t = rest[i]
            if not t.literal:
                return None
            x = t.text
            if x == "--":
                i += 1
                break
            if not x.startswith("-") or x == "-":
                break
            if x.startswith("--"):
                if x in long_flags:
                    i += 1
                elif x in long_arg:
                    if i + 1 >= n:
                        return None
                    i += 2
                elif any(x.startswith(p) for p in long_eq):
                    i += 1
                else:
                    return None
                continue
            letters, j, takes_next = x[1:], 0, False
            while j < len(letters):
                ch = letters[j]
                if ch in with_arg:
                    takes_next = j == len(letters) - 1
                    break
                if ch in flags:
                    j += 1
                    continue
                return None
            i += 1
            if takes_next:
                if i >= n:
                    return None
                i += 1
        return rest[i:]

    def _neutral(self, name, rest):
        sk = self._skip_options
        if name == "nohup" or name == "builtin":
            return sk(rest)
        if name == "time":
            return sk(rest, flags="p")
        if name == "command":
            return sk(rest, flags="p")
        if name == "nice":
            if rest and re.fullmatch(r"-\d+", rest[0].text):
                return rest[1:]
            return sk(rest, with_arg="n", long_eq=("--adjustment=",))
        if name == "ionice":
            return sk(rest, flags="t", with_arg="cn", long_flags=("--ignore",), long_arg=("--class", "--classdata"))
        if name == "setsid":
            return sk(rest, flags="cfw", long_flags=("--ctty", "--fork", "--wait"))
        if name == "stdbuf":
            return sk(rest, with_arg="ioe", long_eq=("--input=", "--output=", "--error="))
        if name == "timeout":
            r = sk(
                rest,
                flags="v",
                with_arg="sk",
                long_flags=("--foreground", "--preserve-status", "--verbose"),
                long_eq=("--signal=", "--kill-after="),
                long_arg=("--signal", "--kill-after"),
            )
            if not r:
                return None if r is None else r
            if not r[0].literal or not re.fullmatch(r"[0-9]+(\.[0-9]+)?[smhd]?", r[0].text):
                return None
            return r[1:]
        return None

    def _shell(self, name, argv, rest, plain) -> None:
        """``sh -c STRING``.  Options may come before AND after ``-c`` (``bash -c -x
        STRING``, ``bash -ec -- STRING``): the string is the first word that is not
        an option.  A shell without ``-c`` runs a script or stdin: unknown."""
        i, n = 0, len(rest)
        c_seen = False
        ok = True
        while i < n:
            t = rest[i]
            if not t.literal:
                break
            x = t.text
            if x == "--":
                i += 1
                break
            if x in ("-o", "+o", "-O", "+O"):
                i += 2
                continue
            if x.startswith("--"):
                if x in ("--norc", "--noprofile", "--posix", "--login", "--restricted", "--noediting"):
                    i += 1
                    continue
                ok = False
                break
            if x.startswith(("-", "+")) and len(x) > 1:
                letters = x[1:]
                if any(ch not in "cilrxeuvEfhnTBHPm" for ch in letters):
                    ok = False
                    break
                if "c" in letters:
                    c_seen = True
                i += 1
                continue
            break
        if not c_seen or i >= n:
            self._emit(plain("shell runs a script or reads standard input", name))
            return
        string = rest[i]
        if not ok or not string.literal or string.text.startswith(("-", "+")):
            self._emit(plain("shell command string not understood", name))
            return
        self._emit(plain(None, name))
        self._wrapped_string(string.text)

    def _su(self, rest, plain) -> None:
        for i, w in enumerate(rest):
            if w.literal and w.text == "-c" and i + 1 < len(rest):
                s = rest[i + 1]
                if s.literal:
                    self._emit(plain(None, "su"))
                    self._wrapped_string(s.text)
                    return
                break
        self._emit(plain("su without a command string", "su"))

    def _env(self, argv, assigns, rest, plain) -> None:
        sk = self._skip_options
        if any(w.literal and (w.text == "--chdir" or w.text.startswith("--chdir=") or (w.text.startswith("-") and not w.text.startswith("--") and "C" in w.text)) for w in rest[:8]):
            self._emit(plain("wrapper changes the working directory", "env"))
            return
        inner = sk(rest, flags="i0", with_arg="u", long_flags=("--ignore-environment", "--null"), long_eq=("--unset=",))
        if inner is None:
            self._emit(plain("wrapper options not understood", "env"))
            return
        k = 0
        while k < len(inner) and inner[k].is_assign:
            k += 1
        cmd = inner[k:]
        if not cmd:
            self._emit(plain())
            return
        self._emit(plain(None, "env"))
        if self.depth + 1 > MAX_DEPTH:
            self.unk("nesting too deep")
            return
        child = _Parser("", self.depth + 1, self.budget)
        child._classify(tuple(cmd), tuple(inner[:k]), (), [], ("wrapped",))
        self.parts.extend(child.parts)
        self.unknown.extend(child.unknown)

    def _privilege(self, name, rest, plain) -> None:
        sk = self._skip_options
        if name == "sudo" and any(
            w.literal and (w.text.startswith("--chdir") or (w.text.startswith("-") and not w.text.startswith("--") and "D" in w.text))
            for w in rest[:8]
        ):
            self._emit(plain("wrapper changes the working directory", name))
            return
        if name == "sudo":
            inner = sk(
                rest, flags="nEHbSkKAPB", with_arg="ugCphrtT",
                long_flags=("--non-interactive", "--preserve-env", "--set-home", "--background", "--stdin", "--reset-timestamp", "--askpass"),
                long_eq=("--user=", "--group=", "--prompt=", "--preserve-env=", "--host=", "--role=", "--type="),
                long_arg=("--user", "--group"),
            )  # fmt: skip
        elif name == "doas":
            inner = sk(rest, flags="n", with_arg="uC")
        else:
            inner = sk(rest, long_flags=("--disable-internal-agent",), long_arg=("--user",), long_eq=("--user=",))
        if inner is None:
            self._emit(plain("wrapper options not understood", name))
            return
        if not inner:
            self._emit(plain(None, name))
            return
        self._emit(plain(None, name))
        k = 0
        while k < len(inner) and inner[k].is_assign:
            k += 1
        if k == len(inner):
            return
        self._wrapped_argv(inner[k:], None, tuple(inner[:k]))

    def _xargs(self, rest, plain) -> None:
        inner = self._skip_options(
            rest, flags="0rtpxo", with_arg="aEIdLnPs",
            long_flags=("--null", "--no-run-if-empty", "--verbose", "--interactive", "--open-tty", "--exit"),
            long_eq=("--max-args=", "--max-procs=", "--max-lines=", "--delimiter=", "--arg-file=", "--replace=", "--eof=", "--max-chars="),
        )  # fmt: skip
        if inner is None:
            self._emit(plain("wrapper options not understood", "xargs"))
        elif not inner:
            self._emit(plain("xargs with no command", "xargs"))
        else:
            self._emit(plain(None, "xargs"))
            self._wrapped_argv(inner, "arguments are supplied at run time")

    def _find(self, argv, plain) -> None:
        self._emit(plain())
        i, n = 1, len(argv)
        while i < n:
            w = argv[i]
            if w.literal and w.text in ("-exec", "-execdir", "-ok", "-okdir"):
                j = i + 1
                while j < n and not (argv[j].literal and argv[j].text in (";", "+")):
                    j += 1
                if j >= n:
                    self.unk("find -exec without a terminator")
                    return
                body = list(argv[i + 1 : j])
                if not body:
                    self.unk("find -exec without a command")
                else:
                    self._wrapped_argv(body, "arguments are supplied at run time")
                i = j + 1
            else:
                i += 1


def parse(command: object) -> ParseResult:
    """Parse ``command``.  Never raises; anything doubtful is in ``unknown``."""
    if not isinstance(command, str):
        return ParseResult((), ("command is not a string",))
    if len(command) > MAX_LENGTH:
        return ParseResult((), ("command is too long to analyse",))
    if "\x00" in command:
        return ParseResult((), ("command contains a NUL byte",))
    try:
        p = _Parser(command, 0, _Budget()).run()
        return ParseResult(tuple(p.parts), tuple(dict.fromkeys(p.unknown)))
    except Exception:  # noqa: BLE001 - RecursionError, MemoryError, or a bug: fail closed
        return ParseResult((), ("parser failure",))
