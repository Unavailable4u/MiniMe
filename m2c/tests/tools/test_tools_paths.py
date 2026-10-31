"""
m2c/tests/tools/test_tools_paths.py -- Patch C05: the path guard and the
two-form denylist (m2c.tools.paths).

The 14 tests ported from the daemon are in test_tools_path_guard_port.py; this
file is everything C05 adds: traversal and absolute paths, NUL bytes, symlink
escapes for reads, writes and deletes, the "never follow a symlink for the
final component of a write" rule, sibling-prefix names, over-broad roots, the
hard-coded denylist (every entry, case-insensitively, evaluated before the
filesystem is touched), the user denylist (pattern forms, validation, and that
it can never loosen the hard list), and the race between checking a path and
opening it (a component swapped for a symlink in between must be refused, not
followed).

Run: cd m2c && pytest tests/tools/test_tools_paths.py -v
"""
from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest
from tools_fixtures import PROTECTED_FILES, SECRET, write

from m2c.tools import ToolContext
from m2c.tools import paths as guard
from m2c.tools.paths import (
    Denylist,
    InvalidPathError,
    PathGuardError,
    ProtectedPathError,
    SymlinkError,
    ToolError,
    assert_safe_root,
    open_dir_chain,
    resolve_for_delete,
    resolve_for_read,
    resolve_for_write,
)


@pytest.fixture
def deny(proj) -> Denylist:
    return Denylist(root=proj)


# --------------------------------------------------------------------------
# Error types
# --------------------------------------------------------------------------
def test_every_guard_error_is_a_tool_error_with_a_stable_code():
    assert issubclass(PathGuardError, ToolError)
    assert PathGuardError("x").code == "outside_root"
    assert ProtectedPathError().code == "protected_path"
    assert InvalidPathError("x").code == "invalid_path"
    assert SymlinkError("x").code == "symlink_refused"
    assert ToolError("x", code="not_found").code == "not_found"
    assert str(ToolError("hello", code="io_error")) == "hello"


def test_protected_path_message_is_fixed_and_names_nothing():
    msg = str(ProtectedPathError())
    assert msg == "protected path"


# --------------------------------------------------------------------------
# Roots
# --------------------------------------------------------------------------
@pytest.mark.parametrize("broad", ["/", "/home", "/etc", "/usr", "/var", "/tmp", "/bin", "/root"])
def test_broad_system_roots_are_refused(broad):
    if not Path(broad).is_dir():
        pytest.skip(f"{broad} does not exist here")
    with pytest.raises(PathGuardError, match="too broad"):
        assert_safe_root(broad)


def test_home_is_resolved_at_call_time_not_import_time(tmp_path, monkeypatch):
    """The daemon froze the set of broad roots at import; with HOME changed
    per test (conftest) that would compare against the wrong directory."""
    fake_home = tmp_path / "somebody-else"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    with pytest.raises(PathGuardError, match="too broad"):
        assert_safe_root(fake_home)


def test_a_subdirectory_of_home_is_fine(tmp_path, monkeypatch, _hermetic_env):
    project = _hermetic_env / "code" / "app"
    project.mkdir(parents=True)
    assert assert_safe_root(project) == project.resolve()


def test_nul_in_a_root_is_an_error_not_a_crash():
    with pytest.raises(PathGuardError):
        assert_safe_root("/tmp/a\0b")


@pytest.mark.parametrize("name", [".ssh", ".aws", ".gnupg"])
def test_a_root_inside_a_credential_directory_is_refused(tmp_path, name):
    root = tmp_path / name / "proj"
    root.mkdir(parents=True)
    with pytest.raises(PathGuardError, match="protected"):
        ToolContext(root)


def test_the_m2c_state_directory_cannot_be_the_root(tmp_path, monkeypatch):
    state = tmp_path / "state-dir"
    state.mkdir()
    monkeypatch.setenv("M2C_HOME", str(state))
    with pytest.raises(PathGuardError):
        ToolContext(state)
    sub = state / "checkpoints"
    sub.mkdir()
    with pytest.raises(PathGuardError):
        ToolContext(sub)


# --------------------------------------------------------------------------
# Traversal, absolute paths, malformed paths
# --------------------------------------------------------------------------
def test_relative_path_resolves_under_the_root(proj, deny):
    write(proj, "a/b.txt")
    r = resolve_for_read(proj, "a/b.txt", deny)
    assert r.rel == ("a", "b.txt")
    assert r.path == proj / "a" / "b.txt"


def test_dot_is_the_root_itself(proj, deny):
    assert resolve_for_read(proj, ".", deny).rel == ()


@pytest.mark.parametrize(
    "raw", ["../outside.txt", "a/../../outside.txt", "../../../../etc/passwd", "a/b/../../../x"]
)
def test_dotdot_traversal_is_refused(proj, deny, raw):
    with pytest.raises(PathGuardError) as ei:
        resolve_for_read(proj, raw, deny)
    assert ei.value.code == "outside_root"


def test_dotdot_that_stays_inside_is_fine(proj, deny):
    write(proj, "a/b.txt")
    assert resolve_for_read(proj, "a/x/../b.txt", deny).rel == ("a", "b.txt")


def test_absolute_path_outside_the_root_is_refused(proj, deny):
    with pytest.raises(PathGuardError) as ei:
        resolve_for_read(proj, "/etc/passwd", deny)
    assert ei.value.code == "outside_root"


def test_absolute_path_inside_the_root_is_accepted(proj, deny):
    f = write(proj, "src/app.py")
    assert resolve_for_read(proj, str(f), deny).rel == ("src", "app.py")


def test_the_root_reached_through_a_symlinked_path_is_accepted(tmp_path, proj, deny):
    alias = tmp_path / "alias"
    alias.symlink_to(proj)
    write(proj, "x.txt")
    assert resolve_for_read(proj, str(alias / "x.txt"), deny).rel == ("x.txt",)


def test_tilde_is_not_expanded(proj, deny, _hermetic_env):
    """``~/x`` names a file called ``~`` inside the project, never the home."""
    (_hermetic_env / "x").write_text("home file")
    r = resolve_for_read(proj, "~/x", deny)
    assert r.rel == ("~", "x")


@pytest.mark.parametrize("raw", ["", "a\0b", "\0", "x" * 5000, 5, None, b"bytes", ["a"]])
def test_malformed_paths_are_invalid_path(proj, deny, raw):
    with pytest.raises(InvalidPathError) as ei:
        resolve_for_read(proj, raw, deny)
    assert ei.value.code == "invalid_path"


def test_sibling_prefix_directories_are_outside(tmp_path, proj):
    root = proj
    sibling = tmp_path / "proj-x"
    sibling.mkdir()
    (sibling / "f.txt").write_text("x")
    root = root.resolve()
    d = Denylist(root=root)
    for raw in (str(sibling / "f.txt"), "../proj-x/f.txt", str(root) + "-x/f.txt"):
        with pytest.raises(PathGuardError) as ei:
            resolve_for_read(root, raw, d)
        assert ei.value.code == "outside_root"


# --------------------------------------------------------------------------
# Symlinks: reads
# --------------------------------------------------------------------------
def test_symlink_to_an_outside_file_is_refused_for_reads(proj, deny, outside):
    (proj / "link.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(PathGuardError) as ei:
        resolve_for_read(proj, "link.txt", deny)
    assert ei.value.code == "outside_root"


def test_symlinked_directory_to_outside_is_refused_for_reads(proj, deny, outside):
    (proj / "dir").symlink_to(outside)
    with pytest.raises(PathGuardError):
        resolve_for_read(proj, "dir/secret.txt", deny)


def test_a_chain_of_symlinks_ending_outside_is_refused(proj, deny, outside):
    (proj / "b").symlink_to(outside / "secret.txt")
    (proj / "a").symlink_to("b")
    with pytest.raises(PathGuardError):
        resolve_for_read(proj, "a", deny)


def test_symlink_inside_the_root_resolves_to_its_target(proj, deny):
    write(proj, "real/f.txt")
    (proj / "alias").symlink_to("real")
    assert resolve_for_read(proj, "alias/f.txt", deny).rel == ("real", "f.txt")


def test_a_symlink_loop_is_not_followed_forever(proj, deny):
    (proj / "a").symlink_to("b")
    (proj / "b").symlink_to("a")
    # Either the guard refuses it, or the later no-follow open does; what must
    # never happen is a hang or an unhandled exception type.
    try:
        r = resolve_for_read(proj, "a", deny)
    except ToolError:
        return
    with pytest.raises(SymlinkError):
        fd = open_dir_chain(proj, r.rel)
        os.close(fd)


# --------------------------------------------------------------------------
# Symlinks: writes
# --------------------------------------------------------------------------
def test_final_component_symlink_is_never_followed_for_a_write(proj, deny, outside):
    (proj / "out.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(SymlinkError) as ei:
        resolve_for_write(proj, "out.txt", deny)
    assert ei.value.code == "symlink_refused"
    assert (outside / "secret.txt").read_text() == f"{SECRET} outside\n"


def test_final_symlink_to_a_file_inside_the_root_is_refused_too(proj, deny):
    write(proj, "real.txt")
    (proj / "alias.txt").symlink_to("real.txt")
    with pytest.raises(SymlinkError):
        resolve_for_write(proj, "alias.txt", deny)


def test_dangling_final_symlink_is_refused_for_a_write(proj, deny, outside):
    (proj / "dangling.txt").symlink_to(outside / "does-not-exist")
    with pytest.raises(SymlinkError):
        resolve_for_write(proj, "dangling.txt", deny)
    assert not (outside / "does-not-exist").exists()


def test_parent_directory_symlinked_outside_is_refused_for_a_write(proj, deny, outside):
    (proj / "esc").symlink_to(outside)
    with pytest.raises(PathGuardError) as ei:
        resolve_for_write(proj, "esc/new.txt", deny)
    assert ei.value.code == "outside_root"
    assert not (outside / "new.txt").exists()


@pytest.mark.parametrize("raw", ["", ".", "..", "a/..", "dir/", "dir/."])
def test_write_targets_must_be_a_plain_file_name(proj, deny, raw):
    (proj / "dir").mkdir(exist_ok=True)
    with pytest.raises(ToolError):
        resolve_for_write(proj, raw, deny)


def test_write_to_a_new_nested_path_resolves_without_creating_anything(proj, deny):
    r = resolve_for_write(proj, "new/deep/file.txt", deny)
    assert r.rel == ("new", "deep", "file.txt")
    assert not (proj / "new").exists()


# --------------------------------------------------------------------------
# Symlinks: deletes
# --------------------------------------------------------------------------
def test_deleting_a_symlink_that_points_outside_is_refused(proj, deny, outside):
    (proj / "out.txt").symlink_to(outside / "secret.txt")
    with pytest.raises(PathGuardError) as ei:
        resolve_for_delete(proj, "out.txt", deny)
    assert ei.value.code == "outside_root"


def test_deleting_a_symlink_that_points_inside_names_the_link_itself(proj, deny):
    write(proj, "real.txt")
    (proj / "alias.txt").symlink_to("real.txt")
    r = resolve_for_delete(proj, "alias.txt", deny)
    assert r.rel == ("alias.txt",)


def test_delete_through_a_symlinked_parent_that_leaves_the_root_is_refused(proj, deny, outside):
    (proj / "esc").symlink_to(outside)
    with pytest.raises(PathGuardError):
        resolve_for_delete(proj, "esc/secret.txt", deny)


# --------------------------------------------------------------------------
# The hard-coded denylist
# --------------------------------------------------------------------------
HARD_HITS = PROTECTED_FILES + [
    ".envrc",
    ".ssh/known_hosts",
    ".ssh/deeply/nested/file",
    "home/user/.python_history",
    ".gnupg/pubring.kbx",
    ".config/gh/hosts.yml",
    ".terraform.d/credentials.tfrc.json",
    "backup/id_ecdsa",
    "x.pfx",
    "x.jks",
    ".pgpass",
    ".npmrc",
    ".pypirc",
    "vault.kdbx",
    ".azure/accessTokens.json",
    ".env/lib/python3/site.py",  # a virtualenv named .env is hidden as a whole
]


@pytest.mark.parametrize("rel", HARD_HITS)
def test_hard_denylist_matches(deny, rel):
    assert deny.is_protected(tuple(rel.split("/"))) is True


@pytest.mark.parametrize("rel", [r.upper() for r in HARD_HITS[:12]])
def test_hard_denylist_ignores_case(deny, rel):
    assert deny.is_protected(tuple(rel.split("/"))) is True


@pytest.mark.parametrize(
    "rel",
    [
        "README.md",
        "src/envelope.py",
        "environment.md",
        "docs/keys.md",
        "src/monkey.py",
        "turkey.txt",
        "docker/config.json",
        "kube/config",
        ".config/other/x",
        "ssh/config",
        "notes/identity.md",
        ".github/workflows/ci.yml",
        "src/envs/x",
        "src/.envelope/x",
    ],
)
def test_hard_denylist_does_not_over_match(deny, rel):
    assert deny.is_protected(tuple(rel.split("/"))) is False


@pytest.mark.parametrize("rel", PROTECTED_FILES)
def test_protected_paths_are_refused_even_when_they_do_not_exist(proj, deny, rel):
    """No existence oracle: the check runs before the filesystem is touched."""
    for resolver in (resolve_for_read, resolve_for_write, resolve_for_delete):
        with pytest.raises(ProtectedPathError) as ei:
            resolver(proj, rel, deny)
        assert ei.value.code == "protected_path"
        assert str(ei.value) == "protected path"
        assert rel not in str(ei.value)


@pytest.mark.parametrize("rel", PROTECTED_FILES)
def test_protected_paths_are_refused_when_they_exist(proj, deny, rel):
    write(proj, rel, f"{SECRET}\n")
    with pytest.raises(ProtectedPathError):
        resolve_for_read(proj, rel, deny)


def test_a_symlink_pointing_at_a_protected_file_is_refused(proj, deny):
    write(proj, ".env", f"{SECRET}\n")
    (proj / "notes.txt").symlink_to(".env")
    with pytest.raises(ProtectedPathError):
        resolve_for_read(proj, "notes.txt", deny)


def test_a_symlink_named_like_a_protected_file_is_refused(proj, deny):
    write(proj, "harmless.txt")
    (proj / ".env").symlink_to("harmless.txt")
    with pytest.raises(ProtectedPathError):
        resolve_for_read(proj, ".env", deny)


def test_a_symlinked_directory_that_leads_into_a_protected_directory_is_refused(proj, deny):
    write(proj, ".ssh/config", f"{SECRET}\n")
    (proj / "shortcut").symlink_to(".ssh")
    with pytest.raises(ProtectedPathError):
        resolve_for_read(proj, "shortcut/config", deny)


def test_protected_names_hidden_behind_dotdot_are_still_refused(proj, deny):
    """``sub/../.env`` and ``.ssh/../README.md`` both name a protected
    component lexically; the guard errs on the side of refusing."""
    (proj / "sub").mkdir()
    for raw in ("sub/../.env", ".ssh/../README.md"):
        with pytest.raises(ProtectedPathError):
            resolve_for_read(proj, raw, deny)


def test_the_hard_list_is_a_tuple_of_strings_not_something_a_caller_can_extend():
    assert isinstance(guard.HARD_NAME_PATTERNS, tuple)
    assert isinstance(guard.HARD_SEQUENCE_PATTERNS, tuple)
    assert all(isinstance(p, str) for p in guard.HARD_NAME_PATTERNS)


def test_state_inside_the_project_is_protected_when_m2c_home_is_there(proj, monkeypatch):
    state = proj / ".state"
    state.mkdir()
    (state / "credentials.json").write_text(f"{SECRET}\n")
    (state / "state.db").write_text("x")
    monkeypatch.setenv("M2C_HOME", str(state))
    context = ToolContext(proj)
    for rel in (".state/credentials.json", ".state/state.db", ".state"):
        with pytest.raises(ProtectedPathError):
            resolve_for_read(proj, rel, context.denylist)


def test_default_m2c_directory_is_protected_as_well_as_m2c_home(tmp_path, monkeypatch, _hermetic_env):
    """M2C_HOME points elsewhere, but ``~/.m2c`` (another profile's
    credentials) is still on the list."""
    other = tmp_path / "other-profile"
    other.mkdir()
    monkeypatch.setenv("M2C_HOME", str(other))
    root = tmp_path / "proj"
    root.mkdir()
    context = ToolContext(root)
    protected = {str(p) for p in context.denylist.protected_dirs}
    assert str(other.resolve()) in protected
    assert str((_hermetic_env / ".m2c").resolve()) in protected


# --------------------------------------------------------------------------
# The user denylist
# --------------------------------------------------------------------------
def _user(proj, *patterns) -> Denylist:
    return Denylist(patterns, root=proj)


def test_user_pattern_without_a_slash_matches_any_component(proj):
    d = _user(proj, "secrets")
    assert d.is_protected(("secrets",))
    assert d.is_protected(("a", "secrets", "k.txt"))
    assert not d.is_protected(("a", "not-secrets", "k.txt"))


def test_user_pattern_with_a_wildcard(proj):
    d = _user(proj, "*.secret")
    assert d.is_protected(("a", "db.secret"))
    assert not d.is_protected(("a", "db.secret.md"))


def test_leading_slash_anchors_a_user_pattern_to_the_project_root(proj):
    d = _user(proj, "/private")
    assert d.is_protected(("private", "x"))
    assert not d.is_protected(("sub", "private", "x"))


def test_multi_segment_user_pattern(proj):
    d = _user(proj, "config/prod.yml")
    assert d.is_protected(("config", "prod.yml"))
    assert d.is_protected(("deploy", "config", "prod.yml"))
    assert not d.is_protected(("config", "dev.yml"))


def test_user_pattern_double_star(proj):
    d = _user(proj, "docs/**/draft*")
    assert d.is_protected(("docs", "draft1.md"))
    assert d.is_protected(("docs", "a", "b", "draft-x.md"))
    assert not d.is_protected(("docs", "a", "final.md"))


def test_trailing_slash_protects_the_directory_and_everything_below(proj):
    d = _user(proj, "vault/")
    assert d.is_protected(("vault",))
    assert d.is_protected(("vault", "a", "b"))


def test_user_patterns_ignore_case(proj):
    assert _user(proj, "Secrets").is_protected(("sECRETS", "x"))


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "!keep", "/", "//", "a/../b", "./x", "a\0b", "x" * 300, 5, None, b"x"],
)
def test_bad_user_patterns_are_rejected_loudly(proj, bad):
    """A pattern that cannot be understood must stop the session from
    starting; silently ignoring it would leave the user believing a file is
    protected when it is not (fail closed)."""
    with pytest.raises(ValueError):
        Denylist([bad], root=proj)


def test_a_user_pattern_cannot_loosen_the_hard_list(proj):
    d = _user(proj, "unrelated")
    assert d.is_protected((".env",))
    with pytest.raises(ValueError):
        Denylist(["!.env"], root=proj)


def test_context_copies_the_user_list(proj):
    mine = ["secrets"]
    context = ToolContext(proj, user_denylist=mine)
    mine.clear()
    mine.append("something-else")
    assert context.denylist.is_protected(("secrets", "x"))
    assert not context.denylist.is_protected(("something-else",))


def test_user_denylist_is_enforced_by_the_resolvers(proj):
    context = ToolContext(proj, user_denylist=["*.secret", "/private/"])
    for rel in ("db.secret", "a/db.secret", "private/x.txt"):
        for resolver in (resolve_for_read, resolve_for_write, resolve_for_delete):
            with pytest.raises(ProtectedPathError):
                resolver(proj, rel, context.denylist)


# --------------------------------------------------------------------------
# Check-then-open races: the open itself must not follow symlinks
# --------------------------------------------------------------------------
def test_open_dir_chain_walks_real_directories(proj):
    (proj / "a" / "b").mkdir(parents=True)
    fd = open_dir_chain(proj, ("a", "b"))
    try:
        assert os.path.samestat(os.fstat(fd), (proj / "a" / "b").stat())
    finally:
        os.close(fd)


def test_open_dir_chain_refuses_a_component_swapped_for_a_symlink(proj, outside, monkeypatch):
    (proj / "a").mkdir()
    real_open = os.open

    def swap_then_open(name, flags, *args, **kwargs):
        if name == "a" and "dir_fd" in kwargs:
            (proj / "a").rmdir()
            (proj / "a").symlink_to(outside)
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swap_then_open)
    with pytest.raises(SymlinkError):
        open_dir_chain(proj, ("a",))


def test_open_dir_chain_can_create_missing_directories_inside_the_root(proj):
    fd = open_dir_chain(proj, ("x", "y", "z"), create=True)
    os.close(fd)
    assert (proj / "x" / "y" / "z").is_dir()


def test_open_dir_chain_does_not_create_by_default(proj):
    with pytest.raises(FileNotFoundError):
        open_dir_chain(proj, ("nope",))
    assert not (proj / "nope").exists()


def test_open_dir_chain_reports_a_file_in_the_way_as_not_a_directory(proj):
    write(proj, "f")
    with pytest.raises(NotADirectoryError):
        open_dir_chain(proj, ("f", "g"))


def test_errno_vocabulary_used_by_the_walker_is_what_this_kernel_returns(proj):
    """Documents the kernel behaviour the walker depends on: O_NOFOLLOW with
    O_DIRECTORY on a symlink gives ENOTDIR, a plain O_NOFOLLOW open gives
    ELOOP.  If a platform ever differs, this test says so."""
    (proj / "real").mkdir()
    (proj / "lnk").symlink_to("real")
    rfd = os.open(proj, os.O_RDONLY | os.O_DIRECTORY)
    try:
        with pytest.raises(OSError) as ei:
            os.open("lnk", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rfd)
        assert ei.value.errno in (errno.ENOTDIR, errno.ELOOP)
        with pytest.raises(OSError) as ei:
            os.open("lnk", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=rfd)
        assert ei.value.errno in (errno.ELOOP, errno.ENOTDIR)
    finally:
        os.close(rfd)
