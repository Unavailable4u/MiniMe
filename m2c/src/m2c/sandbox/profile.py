"""
m2c/src/m2c/sandbox/profile.py -- Patch C11a: the bubblewrap profile.

SECURITY-CRITICAL.  ``build_profile`` turns "run a command for this project" into the
list of ``bwrap`` options that confine it.  It is a pure function: it looks at the
disk (to see what exists) and starts nothing, so every property below is testable on
a machine without bubblewrap.  What it returns is ARGV, a list of strings, never a
shell string: nothing here is ever joined, quoted or parsed by a shell.

What the sandbox is
-------------------
A new root filesystem (a tmpfs bubblewrap makes), into which these are mounted, in
this order (a LATER mount wins over an earlier one at the same place, which is why
the order is part of the contract and tests pin it):

1.  ``/usr`` read-only; ``/bin``, ``/sbin``, ``/lib``, ``/lib64`` as symlinks into
    it when the machine has the merged-``/usr`` layout, as read-only binds of the
    real directory when it does not (``SystemLayout``).
2.  A short, explicit list of ``/etc`` entries (``ETC_ENTRIES``), read-only, each
    only if it exists: passwd, group, ld.so.cache, alternatives, certificates and a
    few more.  NOT ``/etc`` as a whole: ``shadow``, ``ssh``, ``ssl/private`` and
    the system start-up files are simply not there.
3.  A minimal ``/proc`` (of the new PID namespace, so the host's processes are not
    in it), a minimal ``/dev``, and a private, empty, writable ``/tmp``.
4.  The home directory, as an EMPTY tmpfs, made read-only at the end.  The home
    secrets (``~/.ssh``, ``~/.aws``, ``~/.gnupg``, ``~/.config``, browser profiles,
    ``~/.m2c`` ...) are never mounted: they are not "hidden", they are absent.
5.  Read-only toolchain directories under the home, ONLY if they exist
    (``DEFAULT_HOME_TOOLCHAIN_DIRS``: ``.nvm``, ``.pyenv``, ``.cargo``, ``.rustup``,
    ``.local/bin``; the list is a parameter) and any extra read-only paths the
    caller configured.  A request that would expose a secret, the home itself or a
    system directory is REFUSED, not trimmed (``ProfileError``).  Known registry
    credential files inside a mounted toolchain directory (``.cargo/credentials``)
    are masked with an empty file.
6.  The project directory, READ-WRITE: the only writable path on the machine apart
    from ``/tmp``.  Inside it ``.git``, ``.m2c`` and the shell start-up files that
    exist (``PROTECTED_DIRS``, ``PROTECTED_RC_FILES``) are bound read-only ON TOP of
    it.  A read-only bind mount cannot be removed or renamed from inside either
    (``rmdir .git`` and ``mv .git x`` fail with "busy"), which is what stops the
    command from replacing a protected directory with its own.
7.  The home and the root are remounted read-only, after every mount (a remount
    before a later mount would leave that mount's parent writable).

Namespaces: new user (when the kernel offers it, ``--unshare-user-try``), PID, IPC,
UTS and NETWORK.  The network namespace is unshared and nothing here ever shares it
back: the command has only a loopback interface, so no connection leaves the
machine.  (The proxy that C11b adds is a separate patch.)  ``--die-with-parent`` and
``--new-session`` (no controlling terminal, so no TIOCSTI keystroke injection) are
always present, and every Linux capability is dropped.

The environment: ``--clearenv`` and then exactly the variables passed in (the
caller passes ShellConfig's sanitized environment), with ``HOME`` forced to the
empty home, ``TMPDIR`` to ``/tmp`` and ``PWD`` to the project.  Names that are not
plain identifiers and values with a NUL byte are dropped, never repaired.  The
working directory is the project root.

Why a project can be refused (``ProfileError``; the text is fixed and never repeats a
path, because a path can be a secret too): it must be an absolute, existing
directory; not ``/``; not under ``/mnt/<drive>`` (the Windows filesystem: slow, other
permission semantics, and what C01's doctor already says the sandbox refuses); not
a system directory; not the home or a parent of it; not containing, or inside, a
secret directory or the configured m2c directory; and none of its protected names
may be a symlink (a symlink cannot be mounted over, so the command could replace it).

Known limits, stated here so nobody has to discover them:

* A ``.git`` or ``.bashrc`` that does NOT exist when the command starts is not
  protected: bubblewrap can only bind what exists, and creating the file for the
  person would change their project.  A repository the command ``git init``s itself
  can therefore get hooks the person's own terminal later runs.
* Nested repositories (submodules, vendored clones) are not protected, only the
  top-level names.  The policy engine (C06) still guards those for the file tools.
* The project directory is writable, so the command can change anything inside it
  that is not listed above, including files the person's tools will run later.
  This is the price of a coding agent; the approval flow is what covers it.

Standard library only.  Place this file at: m2c/src/m2c/sandbox/profile.py
"""
from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_HOME_TOOLCHAIN_DIRS", "ETC_ENTRIES", "NEVER_MOUNT_HOME", "PROFILE_ERROR_MESSAGES", "PROTECTED_DIRS",
    "PROTECTED_RC_FILES", "ProfileError", "SystemLayout", "build_profile", "detect_system_layout",
]

# --------------------------------------------------------------------------
# What is protected, what is never mounted
# --------------------------------------------------------------------------
# Inside the project: bound read-only on top of the writable project, when they exist.
PROTECTED_DIRS: tuple[str, ...] = (".git", ".m2c")

# The shell start-up files and the like.  C06's protected basenames are the source of truth (a test fails if this
# list ever falls behind it); a few more are added.  A name here may be a file or a directory (.direnv).
PROTECTED_RC_FILES: tuple[str, ...] = (
    ".bashrc", ".bash_profile", ".bash_login", ".bash_logout", ".bash_aliases", ".profile",
    ".zshrc", ".zshenv", ".zprofile", ".zlogin", ".zlogout", ".cshrc", ".tcshrc", ".login", ".logout",
    ".kshrc", ".inputrc", ".xprofile", ".xinitrc", ".xsession", ".pam_environment",
    ".gitconfig", ".envrc", ".direnv",
)  # fmt: skip

# Relative to the home.  Never mounted by this module, and a configured extra path may not be one of these, inside
# one of these, or a parent of one of these.
NEVER_MOUNT_HOME: tuple[str, ...] = (
    ".ssh", ".aws", ".gnupg", ".config", ".m2c", ".kube", ".docker", ".azure", ".password-store", ".pki",
    ".mozilla", ".thunderbird", ".var", "snap", ".local/share", ".cache", ".netrc", ".git-credentials",
    ".gitconfig", ".npmrc", ".pypirc", ".bash_history", ".zsh_history", ".python_history", ".psql_history",
    ".mysql_history", ".lesshst", ".viminfo",
)  # fmt: skip

# Read-only toolchain directories under the home, mounted only if they exist.  Relative to the home.
DEFAULT_HOME_TOOLCHAIN_DIRS: tuple[str, ...] = (".nvm", ".pyenv", ".cargo", ".rustup", ".local/bin")

# Files that hold a registry login even inside an otherwise harmless toolchain directory: replaced by an empty file.
_MASKED_FILE_NAMES: tuple[str, ...] = ("credentials", "credentials.toml", ".npmrc", ".netrc", ".pypirc", "auth.json")

# The part of /etc the sandbox gets, each entry only if it exists.  Small and explicit on purpose.
ETC_ENTRIES: tuple[str, ...] = (
    "passwd", "group", "nsswitch.conf", "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "alternatives",
    "ssl/certs", "ca-certificates", "ca-certificates.conf", "pki/tls/certs", "pki/ca-trust",
    "localtime", "timezone", "os-release", "lsb-release", "hosts", "protocols", "services", "terminfo",
)  # fmt: skip

# Top-level system directories handled as "symlink into /usr" or "read-only bind of the real directory".
_SYSTEM_LINK_DIRS: tuple[str, ...] = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")

# A project or a home may not be, or be inside, one of these.
_SYSTEM_DIRS: tuple[str, ...] = (
    "/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/proc", "/dev", "/sys", "/boot",
)

# A configured extra path may not be, or be inside, one of these (the sandbox provides its own /usr and /etc part).
_EXTRA_DENIED_SYSTEM: tuple[str, ...] = ("/etc", "/proc", "/dev", "/sys", "/boot", "/run")

_WINDOWS_DRIVE = re.compile(r"^/mnt/[A-Za-z](/|$)")
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_ENV_VALUE_CHARS = 100_000  # one execve argument may not exceed 128 KiB
_DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------
PROFILE_ERROR_MESSAGES: dict[str, str] = {
    "no_usr": "this machine has no /usr directory the sandbox can use",
    "project_invalid": "the project path must be an absolute path",
    "project_is_root": "the project may not be the root of the filesystem",
    "project_on_windows_drive": "a project on the Windows filesystem (/mnt/<drive>) cannot be sandboxed",
    "project_overlaps_system": "the project may not be, or be inside, a system directory",
    "project_missing": "the project directory does not exist",
    "project_contains_secrets": "the project may not be the home directory, contain it, or overlap a secret directory",
    "protected_symlink": "a protected name in the project (such as .git) is a symlink, which cannot be made read-only",
    "home_invalid": "the home directory is not usable for the sandbox",
    "extra_ro_invalid": "a read-only path in the sandbox settings is malformed",
    "extra_ro_denied": "a read-only path in the sandbox settings would expose a secret, the home or a system directory",
    # raised by bwrap.py, which builds on this module and shares its error type
    "bwrap_missing": "bubblewrap (bwrap) was not found",
    "no_shell": "no shell (bash or sh) was found for the sandbox",
    "command_invalid": "the command must be a string without a NUL byte",
}


class ProfileError(Exception):
    """The profile cannot be built safely.  ``code`` is a key of PROFILE_ERROR_MESSAGES; the text is fixed and never
    contains a path or a value from the caller."""

    def __init__(self, code: str) -> None:
        self.code = code if code in PROFILE_ERROR_MESSAGES else "project_invalid"
        self.message = PROFILE_ERROR_MESSAGES[self.code]
        super().__init__(self.message)


# --------------------------------------------------------------------------
# The machine's own directories
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class SystemLayout:
    """What the system part of the sandbox is made of, found by looking at a root directory.

    ``links``  (relative target, destination) pairs, mounted as ``--symlink`` (``("usr/bin", "/bin")``).
    ``binds``  (source, destination) read-only binds for top-level directories that are real directories.
    ``etc``    (source, destination) read-only binds for the ``ETC_ENTRIES`` that exist.
    ``root``   the directory it was read from ("/" on a real machine; a temporary directory in tests).
    """

    root: str
    usr: str
    links: tuple[tuple[str, str], ...]
    binds: tuple[tuple[str, str], ...]
    etc: tuple[tuple[str, str], ...]


def _is_within(path: str, base: str) -> bool:
    """True when ``path`` is ``base`` or inside it (whole components, so /usr2 is not inside /usr)."""
    if base == "/":
        return path.startswith("/")
    return path == base or path.startswith(base + "/")


def detect_system_layout(root: str = "/") -> SystemLayout:
    """Look at ``root`` and say how its system directories should be mounted.

    ``/bin``, ``/sbin``, ``/lib`` ... that are symlinks into ``/usr`` (merged-/usr, every current Debian, Ubuntu and
    Fedora) become symlinks in the sandbox; real directories, and links that point anywhere else, become read-only
    binds of what they really are.  Missing ones (often ``/lib64``, ``/lib32``) are left out.  A root without
    ``/usr`` cannot be sandboxed (``ProfileError("no_usr")``).
    """
    base = os.path.realpath(root)
    usr = os.path.join(base, "usr")
    if not os.path.isdir(usr):
        raise ProfileError("no_usr")
    real_usr = os.path.realpath(usr)
    links: list[tuple[str, str]] = []
    binds: list[tuple[str, str]] = []
    for name in _SYSTEM_LINK_DIRS:
        entry = os.path.join(base, name)
        if not os.path.lexists(entry):
            continue
        real = os.path.realpath(entry)
        if not os.path.isdir(real):
            continue
        if os.path.islink(entry) and _is_within(real, real_usr):
            links.append((os.path.relpath(real, base), "/" + name))
        else:
            binds.append((real, "/" + name))
    etc: list[tuple[str, str]] = []
    for name in ETC_ENTRIES:
        source = os.path.join(base, "etc", name)
        if os.path.lexists(source) and os.path.exists(source):  # a dangling link is not an entry
            etc.append((source, "/etc/" + name))
    return SystemLayout(base, real_usr, tuple(links), tuple(binds), tuple(etc))


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def _is_abs_path(value: object) -> bool:
    return isinstance(value, str) and bool(value) and "\x00" not in value and os.path.isabs(value)


def _overlaps_system(path: str) -> bool:
    return any(_is_within(path, d) for d in _SYSTEM_DIRS)


def _check_home(home: object) -> str:
    if not _is_abs_path(home):
        raise ProfileError("home_invalid")
    real = os.path.realpath(home)  # type: ignore[arg-type]
    if real in ("/", "/tmp") or _overlaps_system(real) or not os.path.isdir(real):
        raise ProfileError("home_invalid")
    return real


def _secret_paths(home: str, denied: Iterable[str]) -> list[str]:
    """Every absolute path nothing may mount, contain or sit inside: the home secrets and what the caller names."""
    out = [os.path.join(home, name) for name in NEVER_MOUNT_HOME]
    for item in denied:
        if isinstance(item, str) and item and "\x00" not in item and os.path.isabs(item):
            out.append(os.path.normpath(item))
            out.append(os.path.realpath(item))
    return out


def _check_project(project: object, home: str, secrets: Sequence[str]) -> str:
    if not _is_abs_path(project):
        raise ProfileError("project_invalid")
    lexical = os.path.normpath(project)  # type: ignore[arg-type]
    real = os.path.realpath(project)  # type: ignore[arg-type]
    if real == "/" or lexical == "/":
        raise ProfileError("project_is_root")
    if _WINDOWS_DRIVE.match(lexical) or _WINDOWS_DRIVE.match(real):
        raise ProfileError("project_on_windows_drive")
    if real == "/tmp" or _overlaps_system(real):
        raise ProfileError("project_overlaps_system")
    if _is_within(home, real):  # the home itself, or a directory that contains it
        raise ProfileError("project_contains_secrets")
    for secret in secrets:
        if _is_within(secret, real) or _is_within(real, secret):
            raise ProfileError("project_contains_secrets")
    if not os.path.isdir(real):
        raise ProfileError("project_missing")
    return real


def _extra_destination(entry: object, home: str) -> str:
    """Where an extra read-only path appears inside the sandbox: absolute as given, otherwise relative to the home."""
    if not isinstance(entry, str) or not entry or "\x00" in entry:
        raise ProfileError("extra_ro_invalid")
    if entry == "~":
        return home
    if entry.startswith("~/"):
        return os.path.normpath(os.path.join(home, entry[2:]))
    if os.path.isabs(entry):
        return os.path.normpath(entry)
    return os.path.normpath(os.path.join(home, entry))


def _check_extra_allowed(dest: str, real: str, home: str, secrets: Sequence[str]) -> None:
    for candidate in {dest, real}:
        if _is_within(home, candidate):  # the home, a parent of it, or "/"
            raise ProfileError("extra_ro_denied")
        if candidate == "/tmp" or any(_is_within(candidate, d) for d in _EXTRA_DENIED_SYSTEM):
            raise ProfileError("extra_ro_denied")
        for secret in secrets:
            if _is_within(candidate, secret) or _is_within(secret, candidate):
                raise ProfileError("extra_ro_denied")


def _read_only_mounts(entries: Iterable[object], home: str, secrets: Sequence[str], seen: set[str]) -> list[tuple[str, str]]:
    """(source, destination) for every entry that exists; a bad or dangerous entry raises, a missing one is skipped."""
    out: list[tuple[str, str]] = []
    for entry in entries:
        dest = _extra_destination(entry, home)
        if dest in seen:
            continue
        seen.add(dest)
        real = os.path.realpath(dest)
        _check_extra_allowed(dest, real, home, secrets)
        if os.path.exists(real):
            out.append((real, dest))
    return out


def _masked_files(mounts: Iterable[tuple[str, str]]) -> list[str]:
    """Destinations of registry-login files inside the mounted directories (top level only): they get an empty file."""
    out: list[str] = []
    for source, dest in mounts:
        if not os.path.isdir(source):
            continue
        for name in _MASKED_FILE_NAMES:
            if os.path.isfile(os.path.join(source, name)):
                out.append(os.path.join(dest, name))
    return out


def _protected_paths(project: str) -> list[str]:
    out: list[str] = []
    for name in dict.fromkeys(PROTECTED_DIRS + PROTECTED_RC_FILES):
        path = os.path.join(project, name)
        if not os.path.lexists(path):
            continue
        if os.path.islink(path):
            raise ProfileError("protected_symlink")
        out.append(path)
    return out


def _environment_args(env: Mapping[str, str], home: str, project: str) -> list[str]:
    fixed = {"HOME": home, "TMPDIR": "/tmp", "PWD": project}
    chosen: dict[str, str] = {}
    for name, value in env.items():
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if not _ENV_NAME.match(name) or "\x00" in value or len(value) > _MAX_ENV_VALUE_CHARS:
            continue
        chosen[name] = value
    chosen.update(fixed)
    chosen.setdefault("PATH", _DEFAULT_PATH)
    args = ["--clearenv"]
    for name in sorted(chosen):
        args += ["--setenv", name, chosen[name]]
    return args


# --------------------------------------------------------------------------
# The profile
# --------------------------------------------------------------------------
def build_profile(
    *,
    project: str,
    home: str,
    env: Mapping[str, str],
    layout: SystemLayout | None = None,
    toolchain_dirs: Sequence[str] = DEFAULT_HOME_TOOLCHAIN_DIRS,
    extra_ro: Sequence[str] = (),
    denied: Sequence[str] = (),
) -> list[str]:
    """The ``bwrap`` options (without the program name and without the command) that confine a command to ``project``.

    ``project``       absolute path of the project directory (the one writable place).
    ``home``          absolute path of the home directory; it appears in the sandbox as an empty read-only tmpfs.
    ``env``           the environment the command gets (ShellConfig's sanitized one); nothing else is inherited.
    ``layout``        the system directories (``detect_system_layout()``); tests pass a fake root.
    ``toolchain_dirs``/``extra_ro``  read-only paths mounted if they exist (relative to the home, ``~/x`` or absolute).
    ``denied``        absolute paths no mount may expose and the project may not contain (the m2c config directory).

    Raises ``ProfileError`` (fixed text, never a path) when the sandbox cannot be built safely for these inputs.  The
    caller appends ``--`` and the command.  Only the disk is read; nothing is created or changed.
    """
    home_real = _check_home(home)
    secrets = _secret_paths(home_real, denied)
    project_real = _check_project(project, home_real, secrets)
    layout = layout if layout is not None else detect_system_layout("/")

    seen: set[str] = set()
    toolchain = _read_only_mounts(toolchain_dirs, home_real, secrets, seen)
    extras = _read_only_mounts(extra_ro, home_real, secrets, seen)
    protected = _protected_paths(project_real)
    masks = _masked_files(toolchain + extras)

    args: list[str] = [
        "--unshare-user-try", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net",
        "--unshare-cgroup-try", "--die-with-parent", "--new-session", "--cap-drop", "ALL",
    ]  # fmt: skip
    args += ["--ro-bind", layout.usr, "/usr"]
    for target, dest in layout.links:
        args += ["--symlink", target, dest]
    for source, dest in layout.binds:
        args += ["--ro-bind", source, dest]
    for source, dest in layout.etc:
        args += ["--ro-bind", source, dest]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", home_real]
    for source, dest in toolchain + extras:
        args += ["--ro-bind", source, dest]
    for dest in masks:
        args += ["--ro-bind", os.devnull, dest]
    args += ["--bind", project_real, project_real]
    for path in protected:  # AFTER the project bind: a later mount wins
        args += ["--ro-bind", path, path]
    args += ["--remount-ro", home_real, "--remount-ro", "/"]
    args += _environment_args(env, home_real, project_real)
    args += ["--chdir", project_real]
    return args
