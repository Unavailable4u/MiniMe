"""
m2c/src/m2c/policy/rules.py -- Patch C06: permission rules, their layers, and the
purely lexical path checks the policy engine shares.

A rule is ``tool(pattern)`` and has an action, ``deny``, ``ask`` or ``allow``::

    bash(git status)        exact command
    bash(git diff:*)        "git diff" and anything after it (word boundary)
    read_file(src/**)       a path pattern
    edit_file(*.md)         a pattern without a slash also matches the file name

Matching (build plan C06, product guide 8.3):

* Exact strings first, then simple patterns.  ``*`` is the ONLY wildcard and
  matches any run of characters, ``/`` included, so ``src/**`` and ``src/*``
  mean the same.  There is no regular expression anywhere in this module.
* Evaluation order is deny, then ask, then allow, across the UNION of all
  layers (managed, then user, then project).  Because the order is applied to
  the union, a lower layer can never weaken a higher layer: a project ``allow``
  cannot beat a managed ``deny`` or ``ask``, whatever the layer order says.
* Project settings count only when the project is trusted
  (``merge_layers(..., project_trusted=True)``); when it is not, the project
  layer is dropped without being looked at.  The trust prompt itself is built
  later; this module only takes the flag.

Loading is the one place this module touches the disk, and only for the files
the caller names (``load_rule_file`` / ``load_layers`` take paths as arguments,
so tests never read the real home directory).  It FAILS CLOSED: a file that is
unreadable, too large, not valid TOML, has an unknown key under
``[permissions]`` or contains one malformed rule does not silently contribute
fewer rules (which would weaken a ``deny``).  It yields a ``RuleSet`` with
``invalid=True`` and the engine then refuses to ``allow`` anything.  A file
that does not exist is simply empty.

The second half of the module is the lexical path toolkit: normalising a path
against the project root WITHOUT touching the filesystem (no ``realpath``, no
``exists``; C05 checks the real filesystem and both checks apply), and the two
lists the engine needs, protected paths (read-only for edits and writes) and
the hard denylist of secrets (neither read nor written).

Standard library only (``tomllib`` is 3.11+, decision D1).  No logging and no
printing: nothing here ever records a command or a path.

Place this file at: m2c/src/m2c/policy/rules.py
"""
from __future__ import annotations

import posixpath
import re
import tomllib
from dataclasses import dataclass, replace
from typing import Iterable, Sequence

__all__ = [
    "ACTIONS",
    "LAYERS",
    "MAX_RULE_LENGTH",
    "MergedRules",
    "NormPath",
    "Rule",
    "RuleLayers",
    "RuleSet",
    "RuleSyntaxError",
    "SECRET_SAMPLES",
    "canon_options",
    "first_command_match",
    "first_path_match",
    "is_inside",
    "is_protected_path",
    "is_secret_path",
    "load_layers",
    "load_rule_file",
    "merge_layers",
    "normalize_path",
    "parse_rule",
    "pattern_matches",
]

ACTIONS = ("deny", "ask", "allow")
# Highest authority first.  Only used to label rules and order the union.
LAYERS = ("managed", "user", "project")

MAX_RULE_LENGTH = 1024
MAX_RULES_PER_FILE = 5000
MAX_FILE_BYTES = 1_000_000
MAX_PATH_LENGTH = 4096

_RULE_RE = re.compile(r"^([a-z][a-z0-9_.\-]*)\((.*)\)$", re.DOTALL)


class RuleSyntaxError(ValueError):
    """A rule string is not ``tool(pattern)``.  The message never echoes it."""


# --------------------------------------------------------------------------
# Rules and matching
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    """One ``tool(pattern)`` rule with its action and the layer it came from."""

    action: str
    tool: str
    pattern: str
    layer: str = "user"

    @property
    def raw(self) -> str:
        return f"{self.tool}({self.pattern})"

    @property
    def label(self) -> str:
        """``deny:managed:bash(curl:*)``: what a Decision reports as matched_rule."""
        return f"{self.action}:{self.layer}:{self.raw}"


def parse_rule(text: object, action: str, layer: str = "user") -> Rule:
    """Parse ``tool(pattern)``.  Raises RuleSyntaxError for anything else.

    A bare tool name (``bash``) is refused on purpose: an accidental
    "allow everything" must be spelled ``bash(*)``.
    """
    if action not in ACTIONS:
        raise RuleSyntaxError("unknown action")
    if layer not in LAYERS:
        raise RuleSyntaxError("unknown layer")
    if not isinstance(text, str):
        raise RuleSyntaxError("rule is not a string")
    if len(text) > MAX_RULE_LENGTH:
        raise RuleSyntaxError("rule is too long")
    if any(ord(c) < 32 or ord(c) == 127 for c in text):
        raise RuleSyntaxError("rule contains a control character")
    m = _RULE_RE.match(text.strip())
    if m is None:
        raise RuleSyntaxError("rule must look like tool(pattern)")
    tool, pattern = m.group(1), m.group(2).strip()
    if pattern == "":
        raise RuleSyntaxError("rule pattern is empty")
    return Rule(action=action, tool=tool, pattern=pattern, layer=layer)


def _wild(pattern: str, text: str) -> bool:
    """``*``-only wildcard match in linear time (no regex, no backtracking)."""
    parts = pattern.split("*")
    if len(parts) == 1:
        return text == pattern
    first, last = parts[0], parts[-1]
    if not text.startswith(first) or not text.endswith(last):
        return False
    pos, end = len(first), len(text) - len(last)
    if end < pos:
        return False
    for mid in parts[1:-1]:
        if mid == "":
            continue
        i = text.find(mid, pos, end)
        if i < 0:
            return False
        pos = i + len(mid)
    return True


def pattern_matches(pattern: str, text: str) -> bool:
    """Exact string first, then ``*`` patterns, then the ``prefix:*`` form."""
    if pattern == text:
        return True
    if pattern.endswith(":*"):
        prefix = pattern[:-2]
        if prefix == "":
            return True
        return _wild(prefix, text) or _wild(prefix + " *", text)
    if "*" in pattern:
        return _wild(pattern, text)
    return False


# long spellings of the flags a deny rule most often names (rm, cp, mv, chmod ...)
_LONG_AS_SHORT = {"--recursive": "r", "--force": "f", "--verbose": "v", "--interactive": "i"}


def _is_cluster(w: str) -> bool:
    return len(w) >= 2 and w[0] == "-" and w[1] != "-" and w[1:].isalpha() and w.isascii()


def canon_options(text: str) -> str:
    """Canonical spelling of short options: adjacent ``-x`` / ``-xy`` words are
    merged and their letters sorted, so ``rm -fr``, ``rm -rf`` and ``rm -r -f``
    are the same text.  Used for deny and ask rules only (stricter): a
    ``bash(rm -rf:*)`` deny must not be dodged by reordering flags."""
    words = text.split(" ")
    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            out.append("-" + "".join(sorted("".join(run).replace("R", "r"))))
            run.clear()

    for w in words:
        if w in _LONG_AS_SHORT:
            run.append(_LONG_AS_SHORT[w])
        elif len(w) >= 2 and w[0] == "-" and w[1] != "-" and w[1:].isalpha() and w.isascii():
            run.append(w[1:])
        else:
            flush()
            out.append(w)
    flush()
    return " ".join(out)


def _wild_one_word(pattern: str, text: str) -> bool:
    """Like ``_wild`` but a ``*`` never matches a space: ``rm build/*`` covers
    ``rm build/a`` and not ``rm build/a src/b`` (two operands)."""
    parts = pattern.split("*")
    if len(parts) == 1:
        return text == pattern
    first, last = parts[0], parts[-1]
    if not text.startswith(first) or not text.endswith(last):
        return False
    pos, end = len(first), len(text) - len(last)
    if end < pos:
        return False
    for mid in parts[1:-1]:
        if mid == "":
            continue
        i = text.find(mid, pos, end)
        if i < 0 or " " in text[pos:i]:
            return False
        pos = i + len(mid)
    return " " not in text[pos:end]


def _allow_matches(pattern: str, text: str) -> bool:
    if pattern == text or pattern == "*":
        return True
    if pattern.endswith(":*"):
        return pattern_matches(pattern, text)
    if "*" in pattern:
        return _wild_one_word(pattern, text)
    return False


def first_command_match(
    rules: Sequence[Rule], tool: str, texts: Iterable[str], *, canon: bool = False, word_bound: bool = False
) -> Rule | None:
    """First rule for ``tool`` whose pattern matches any of ``texts``.  With
    ``canon`` both sides are also compared in canonical option order.  With
    ``word_bound`` (used for ``allow``) a ``*`` inside a pattern stays within one
    word; only the ``prefix:*`` form and a bare ``*`` run across words."""
    candidates = tuple(texts)
    if word_bound:
        for rule in rules:
            if rule.tool == tool and any(_allow_matches(rule.pattern, t) for t in candidates):
                return rule
        return None
    canon_texts = tuple(canon_options(t) for t in candidates) if canon else ()
    for rule in rules:
        if rule.tool != tool:
            continue
        for t in candidates:
            if pattern_matches(rule.pattern, t):
                return rule
        if canon:
            pat = rule.pattern
            tail = ":*" if pat.endswith(":*") else ""
            cp = canon_options(pat[: len(pat) - len(tail)])
            pw = cp.split(" ")
            pi = next((i for i, w in enumerate(pw) if _is_cluster(w)), None)
            for t in canon_texts:
                if pattern_matches(cp + tail, t):
                    return rule
                if pi is not None:
                    # `rm -rf` also covers `rm -rfv`: the text's flags may be a superset
                    tw = t.split(" ")
                    if len(tw) > pi and _is_cluster(tw[pi]) and set(pw[pi][1:]) <= set(tw[pi][1:]):
                        tw[pi] = pw[pi]
                        if pattern_matches(cp + tail, " ".join(tw)):
                            return rule
    return None


# --------------------------------------------------------------------------
# Rule sets, layers, merging
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RuleSet:
    """The rules of one settings layer.

    ``invalid`` means the layer could not be trusted to be complete (it failed
    to load or held a malformed rule); the engine then never returns ``allow``.
    ``errors`` carries short fixed strings, never file content.
    """

    rules: tuple[Rule, ...] = ()
    invalid: bool = False
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleLayers:
    """The three settings layers, highest authority first."""

    managed: RuleSet = RuleSet()
    user: RuleSet = RuleSet()
    project: RuleSet = RuleSet()


@dataclass(frozen=True)
class MergedRules:
    """The union of the layers, split by action in evaluation order."""

    deny: tuple[Rule, ...] = ()
    ask: tuple[Rule, ...] = ()
    allow: tuple[Rule, ...] = ()
    invalid: bool = False
    project_ignored: bool = False


def merge_layers(layers: RuleLayers, project_trusted: bool) -> MergedRules:
    """Pure union of the layers.  Never raises; anything odd means ``invalid``.

    Every rule is re-labelled with the slot it was found in, so a rule object
    cannot claim a higher layer than the one it was loaded into.  An untrusted
    project layer is ignored entirely (even its ``invalid`` flag: the file was
    never read).
    """
    try:
        buckets: dict[str, list[Rule]] = {a: [] for a in ACTIONS}
        invalid = False
        ignored = False
        for name in LAYERS:
            rs = getattr(layers, name)
            if not isinstance(rs, RuleSet):
                invalid = True
                continue
            if name == "project" and project_trusted is not True:
                ignored = bool(rs.rules or rs.invalid)
                continue
            invalid = invalid or rs.invalid
            for r in rs.rules:
                if not isinstance(r, Rule) or r.action not in ACTIONS:
                    invalid = True
                    continue
                buckets[r.action].append(replace(r, layer=name))
        return MergedRules(
            deny=tuple(buckets["deny"]),
            ask=tuple(buckets["ask"]),
            allow=tuple(buckets["allow"]),
            invalid=invalid,
            project_ignored=ignored,
        )
    except Exception:  # noqa: BLE001 - fail closed, whatever the input was
        return MergedRules(invalid=True)


# --------------------------------------------------------------------------
# Loading (takes paths as arguments; never touches the real home directory)
# --------------------------------------------------------------------------

_INVALID_FILE = RuleSet(invalid=True, errors=("settings file could not be read safely",))


def _rules_from_toml_text(text: str, layer: str) -> RuleSet:
    try:
        doc = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, ValueError, RecursionError):
        return RuleSet(invalid=True, errors=("settings file is not valid TOML",))
    perms = doc.get("permissions", {})
    if not isinstance(perms, dict):
        return RuleSet(invalid=True, errors=("permissions is not a table",))
    unknown = set(perms) - set(ACTIONS)
    if unknown:
        return RuleSet(invalid=True, errors=("unknown key in permissions",))
    out: list[Rule] = []
    for action in ACTIONS:
        items = perms.get(action, [])
        if not isinstance(items, list):
            return RuleSet(invalid=True, errors=(f"permissions.{action} is not a list",))
        for item in items:
            try:
                out.append(parse_rule(item, action, layer))
            except RuleSyntaxError:
                return RuleSet(invalid=True, errors=(f"malformed rule in permissions.{action}",))
            if len(out) > MAX_RULES_PER_FILE:
                return RuleSet(invalid=True, errors=("too many rules",))
    return RuleSet(rules=tuple(out))


def load_rule_file(path: str | None, layer: str) -> RuleSet:
    """Read one settings file.  Missing file or ``None``: empty.  Anything else
    that goes wrong: an ``invalid`` RuleSet (fail closed), never an exception."""
    if path is None:
        return RuleSet()
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_FILE_BYTES + 1)
    except FileNotFoundError:
        return RuleSet()
    except (OSError, ValueError):
        return _INVALID_FILE
    if len(data) > MAX_FILE_BYTES:
        return RuleSet(invalid=True, errors=("settings file is too large",))
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return RuleSet(invalid=True, errors=("settings file is not UTF-8",))
    return _rules_from_toml_text(text, layer)


def load_layers(
    managed: str | None = None,
    user: str | None = None,
    project: str | None = None,
    *,
    project_trusted: bool = False,
) -> RuleLayers:
    """Load the three layers from the given files.

    The project file is NOT opened unless ``project_trusted`` is true: an
    untrusted repository's settings are never even parsed.
    """
    return RuleLayers(
        managed=load_rule_file(managed, "managed"),
        user=load_rule_file(user, "user"),
        project=load_rule_file(project, "project") if project_trusted is True else RuleSet(),
    )


# --------------------------------------------------------------------------
# Lexical paths: no filesystem access, ever
# --------------------------------------------------------------------------

# Where "~" points when the caller does not say.  It is outside every project.
_UNKNOWN_HOME = "/~home"


@dataclass(frozen=True)
class NormPath:
    """A path normalised against the project root, lexically.

    ``abs`` has no ``..`` or ``.`` and no duplicate slashes; ``rel`` is the
    project-relative form (``"."`` for the root itself) or None outside it.
    """

    raw: str
    abs: str
    rel: str | None

    @property
    def inside(self) -> bool:
        return self.rel is not None


def is_inside(abs_path: str, root: str) -> bool:
    return abs_path == root or abs_path.startswith(root.rstrip("/") + "/")


def normalize_path(
    raw: object, root: str, *, cwd: str | None = None, home: str | None = None
) -> NormPath | None:
    """Normalise ``raw`` against ``cwd`` (default: ``root``).  None if malformed
    (not a string, empty, NUL, absurdly long).  Purely string work: a symlink
    that points out of the project looks inside here, and C05 catches it."""
    if not isinstance(raw, str) or raw == "" or "\x00" in raw or len(raw) > MAX_PATH_LENGTH:
        return None
    p = raw
    if p == "~" or p.startswith("~/"):
        p = (home or _UNKNOWN_HOME) + p[1:]
    elif p.startswith("~"):
        p = _UNKNOWN_HOME + "/" + p  # ~user: someone else's home, outside
    if not p.startswith("/"):
        p = (cwd or root) + "/" + p
    n = posixpath.normpath(p)
    if n.startswith("//"):  # POSIX keeps exactly two leading slashes
        n = "/" + n.lstrip("/")
    rel: str | None = None
    if is_inside(n, root):
        rel = "." if n == root else n[len(root.rstrip("/")) + 1 :]
    return NormPath(raw=raw, abs=n, rel=rel)


def match_path_rule_texts(pattern: str, np: NormPath, home: str | None, *, raw: bool = True) -> bool:
    """Does a path rule pattern match this path?  A pattern without a slash is
    matched against the file name as well (``.env*`` finds ``a/b/.env.local``)."""
    pat = pattern[2:] if pattern.startswith("./") else pattern
    texts: list[str] = [np.abs]
    if np.rel is not None:
        texts.append(np.rel)
    if raw:
        # The path as written.  Used for deny and ask rules only (stricter, so
        # ``secrets/../secrets/x`` is caught either way).  An ALLOW rule must see
        # only the normalised forms: ``src/../docs/x`` is not under ``src/``.
        texts.append(np.raw[2:] if np.raw.startswith("./") else np.raw)
    if home and (np.abs == home or np.abs.startswith(home.rstrip("/") + "/")):
        texts.append("~" + np.abs[len(home.rstrip("/")) :])
    if "/" not in pat:
        texts.append(posixpath.basename(np.abs))
    return any(pattern_matches(pat, t) for t in texts)


def first_path_match(
    rules: Sequence[Rule], tools: Iterable[str], np: NormPath, home: str | None, *, raw: bool = True
) -> Rule | None:
    wanted = set(tools)
    for rule in rules:
        if rule.tool in wanted:
            if match_path_rule_texts(rule.pattern, np, home, raw=raw):
                return rule
    return None


# --- protected paths: read-only for edits and writes ----------------------

_PROTECTED_COMPONENTS = frozenset({".git", ".m2c"})
_PROTECTED_BASENAMES = frozenset(
    {
        ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".profile",
        ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".zlogout", ".cshrc",
        ".tcshrc", ".login", ".logout", ".kshrc", ".inputrc", ".xprofile",
        ".xinitrc", ".xsession", ".gitconfig", ".envrc", ".direnv",
    }
)  # fmt: skip
_PROTECTED_ABS = frozenset({"/etc/profile", "/etc/bash.bashrc", "/etc/zshrc", "/etc/environment"})
_PROTECTED_TAILS = ("/.config/fish/config.fish", "/etc/profile.d")


def is_protected_path(np: NormPath) -> bool:
    """True for ``.git/``, ``.m2c/``, shell start-up files and similar.  These
    are read-only for edits, writes, deletes and shell redirection."""
    parts = np.abs.casefold().split("/")
    if any(p in _PROTECTED_COMPONENTS for p in parts):
        return True
    low = np.abs.casefold()
    if parts[-1] in _PROTECTED_BASENAMES or low in _PROTECTED_ABS:
        return True
    return any(low.endswith(t) or ("/" + t.lstrip("/") + "/") in low + "/" for t in _PROTECTED_TAILS)


# --- hard denylist: secrets are neither read nor written -------------------

_SECRET_DIRS = frozenset({".ssh", ".gnupg", ".aws", ".kube", ".azure", ".password-store"})
_SECRET_NAMES = frozenset(
    {".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", "authorized_keys", ".vault-token",
     ".htpasswd", ".boto", ".s3cfg", ".my.cnf", ".dockercfg", ".secrets", "secrets.yml", "secrets.yaml",
     "secrets.json", "kubeconfig", "credentials.json", "terraform.tfstate"}
)  # fmt: skip
_SECRET_SUFFIXES = (".pem", ".p12", ".pfx", ".jks", ".keystore", ".key", ".ppk", ".kdbx", ".gpg", ".asc", ".tfstate")
_BACKUP_SUFFIXES = (".bak", ".old", ".orig", ".save", ".swp", ".swo", ".tmp", ".backup", "~")
_KEY_PREFIXES = ("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_xmss")
_ENV_EXEMPT_SUFFIXES = (".example", ".sample", ".template", ".dist")
_SECRET_ABS = frozenset({"/etc/shadow", "/etc/gshadow", "/etc/sudoers", "/etc/security/opasswd"})


# Names a shell glob must not be able to reach in a command whose arguments we
# cannot fully predict (decide.py).  Derived from the denylist above plus the
# common examples of each pattern, so the two cannot drift apart.
SECRET_SAMPLES: tuple[str, ...] = tuple(
    sorted(
        set(_SECRET_DIRS)
        | set(_SECRET_NAMES)
        | {".env", ".env.local", ".env.production", ".env-prod", ".envrc", ".docker", ".vault-token",
           "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa", "key.pem", "cert.p12", "cert.pfx", "server.key",
           "store.jks", "app.keystore", "key.ppk", "db.kdbx", "credentials", "x.gpg", "x.asc", "x.tfstate"}
    )
)  # fmt: skip


def is_secret_path(np: NormPath) -> bool:
    """The hard denylist: private keys, ``.env`` files, credential stores,
    ``/etc/shadow`` and friends.  Not overridable by any rule or mode.
    ``.env.example``-style template files are the one exemption."""
    low = np.abs.casefold()
    parts = [p for p in low.split("/") if p]
    if not parts:
        return False
    # an editor or backup copy of a secret is still a secret: key.pem.bak, .netrc~
    stripped = True
    while stripped:
        stripped = False
        for suf in _BACKUP_SUFFIXES:
            if len(parts[-1]) > len(suf) and parts[-1].endswith(suf):
                parts[-1] = parts[-1][: -len(suf)]
                stripped = True
    base = parts[-1]
    low = "/" + "/".join(parts)
    if low in _SECRET_ABS or low.startswith("/etc/sudoers.d/"):
        return True
    if any(p in _SECRET_DIRS for p in parts):
        return True
    for i, p in enumerate(parts[:-1]):
        if p == ".config" and parts[i + 1] == "gcloud":
            return True
        if p == ".config" and parts[i + 1] == "gh":
            return True
        if p == ".docker" and base == "config.json":
            return True
        if p == ".m2c" and base.startswith("credential"):
            return True
    if base.startswith(".env") and not base.endswith(_ENV_EXEMPT_SUFFIXES):
        return True  # .env, .env.local, .env-prod, .envrc ...
    if base in _SECRET_NAMES or base.endswith(_SECRET_SUFFIXES):
        return True
    if base.startswith(_KEY_PREFIXES) and not base.endswith(".pub"):
        return True
    if parts[0] == "proc" and base in {"environ", "mem"}:
        return True
    return False
