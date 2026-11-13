"""
m2c/tests/sandbox/test_sandbox_profile.py -- Patch C11a: the bubblewrap profile
(``m2c.sandbox.profile``).

Nothing here starts bubblewrap or needs it: ``build_profile`` only builds a list
of arguments, so every property of that list is checked on any machine.  The
system directories come from a small fake root built in ``tmp_path`` (merged-/usr
or not, with or without /lib64), the "home" is a fake directory next to it, and
every secret is a canary built at run time.

What is pinned, because each one is a way a sandbox quietly stops being one:

* the list is argv, never a shell string, and the command is ONE element;
* exactly ONE read-write bind exists and it is the project;
* ``.git``, ``.m2c`` and the shell start-up files are bound read-only AFTER the
  project (a later mount wins), and only when they exist;
* the home is an empty tmpfs; the secrets under it are never mentioned, and a
  configured toolchain directory cannot be a way to mount them;
* the network namespace is unshared and ``--share-net`` never appears;
* the environment is rebuilt from what is passed, never inherited;
* unusual project paths pass through byte for byte;
* the list of protected shell files cannot silently fall behind the policy's.

Run: cd m2c && pytest tests/sandbox/test_sandbox_profile.py -v
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

import pytest

from m2c.sandbox import profile as prof
from m2c.sandbox.profile import (
    DEFAULT_HOME_TOOLCHAIN_DIRS,
    ETC_ENTRIES,
    NEVER_MOUNT_HOME,
    PROTECTED_DIRS,
    PROTECTED_RC_FILES,
    ProfileError,
    SystemLayout,
    build_profile,
    detect_system_layout,
)


# ==========================================================================
# helpers
# ==========================================================================
def make_root(tmp_path: Path, *, merged_usr: bool = True, lib64: bool = True, etc=("passwd", "group")) -> Path:
    """A fake machine root: /usr with bash and sh, either merged-/usr symlinks or real directories."""
    root = tmp_path / "sysroot"
    for sub in ("usr/bin", "usr/sbin", "usr/lib"):
        (root / sub).mkdir(parents=True)
    if lib64:
        (root / "usr/lib64").mkdir()
    for shell_name in ("bash", "sh"):
        f = root / "usr/bin" / shell_name
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    pairs = [("bin", "usr/bin"), ("sbin", "usr/sbin"), ("lib", "usr/lib")] + ([("lib64", "usr/lib64")] if lib64 else [])
    for name, target in pairs:
        if merged_usr:
            (root / name).symlink_to(target)
        else:
            (root / name).mkdir()
    if not merged_usr:
        for shell_name in ("bash", "sh"):
            f = root / "bin" / shell_name
            f.write_text("#!/bin/sh\n")
            f.chmod(0o755)
    (root / "etc").mkdir()
    for name in etc:
        (root / "etc" / name).write_text("x\n")
    return root


@pytest.fixture
def world(tmp_path):
    """(layout, project, home) with a project inside the home, the usual layout."""
    layout = detect_system_layout(os.fspath(make_root(tmp_path)))
    home = tmp_path / "home"
    project = home / "code" / "proj"
    project.mkdir(parents=True)
    return layout, os.path.realpath(project), os.path.realpath(home)


ENV = {"PATH": "/usr/bin:/bin", "HOME": "/ignored", "LANG": "C.UTF-8"}


def build(world, **kw):
    layout, project, home = world
    kw.setdefault("env", ENV)
    return build_profile(project=project, home=home, layout=layout, **kw)


def pairs(args, option):
    """Every (src, dest) following ``option`` (an option that takes two values)."""
    return [(args[i + 1], args[i + 2]) for i, a in enumerate(args) if a == option]


def index_of(args, option, dest):
    for i, a in enumerate(args):
        if a == option and args[i + 2] == dest:
            return i
    raise AssertionError(f"{option} ... {dest} not found")


def canary() -> str:
    return "m2c-canary-" + secrets.token_hex(8)


# ==========================================================================
# the shape of the list
# ==========================================================================
def test_the_profile_is_a_list_of_plain_strings(world):
    args = build(world)
    assert isinstance(args, list) and args and all(isinstance(a, str) for a in args)
    assert all("\x00" not in a for a in args)


def test_no_element_is_a_joined_shell_string(world):
    """A shell string would be one element holding a whole command line; every option here stands alone."""
    for a in build(world):
        assert not (" --" in a or a.startswith("bwrap") or "&&" in a or ";" in a and a.startswith("--")), a


def test_the_namespaces_and_process_flags_are_all_present(world):
    args = build(world)
    for flag in ("--unshare-user-try", "--unshare-pid", "--unshare-ipc", "--unshare-uts", "--unshare-net",
                 "--die-with-parent", "--new-session"):
        assert args.count(flag) == 1, flag


def test_the_network_is_never_shared(world):
    args = build(world)
    assert "--share-net" not in args
    assert "--unshare-all" not in args, "an umbrella flag hides which namespaces are really unshared"


def test_every_capability_is_dropped(world):
    args = build(world)
    i = args.index("--cap-drop")
    assert args[i + 1] == "ALL" and "--cap-add" not in args


# ==========================================================================
# the project: the only writable place, with protected files read-only on top
# ==========================================================================
def test_exactly_one_read_write_bind_exists_and_it_is_the_project(world):
    layout, project, home = world
    args = build(world)
    rw = pairs(args, "--bind") + pairs(args, "--bind-try")
    assert rw == [(project, project)]
    assert "--dev-bind" not in args and "--dev-bind-try" not in args


def test_protected_directories_and_rc_files_that_exist_are_read_only_after_the_project(world):
    layout, project, home = world
    p = Path(project)
    (p / ".git").mkdir()
    (p / ".m2c").mkdir()
    for name in (".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile"):
        (p / name).write_text("# rc\n")
    args = build(world)
    project_at = index_of(args, "--bind", project)
    for name in (".git", ".m2c", ".bashrc", ".zshrc", ".profile", ".bash_profile", ".zprofile"):
        dest = f"{project}/{name}"
        assert (dest, dest) in pairs(args, "--ro-bind"), name
        assert index_of(args, "--ro-bind", dest) > project_at, f"{name} must come after the project bind (a later mount wins)"


def test_only_protected_paths_that_exist_are_bound(world):
    layout, project, home = world
    (Path(project) / ".git").mkdir()
    args = build(world)
    bound = {d for _, d in pairs(args, "--ro-bind")}
    assert f"{project}/.git" in bound
    for missing in (".m2c", ".bashrc", ".zshrc", ".envrc"):
        assert f"{project}/{missing}" not in bound


@pytest.mark.parametrize("name", [".git", ".m2c", ".bashrc", ".profile"])
@pytest.mark.parametrize("kind", ["into_project", "out_of_project", "dangling"])
def test_a_protected_name_that_is_a_symlink_refuses_the_sandbox(world, tmp_path, name, kind):
    """A symlink cannot be mounted over, so the command could replace it (``rm .git; mkdir .git``).  Whatever it
    points at, the only safe answer is to refuse: fail closed, with a fixed error."""
    layout, project, home = world
    p = Path(project)
    if kind == "into_project":
        (p / "real-target").mkdir()
        target = "real-target"
    elif kind == "out_of_project":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        target = str(elsewhere)
    else:
        target = "nowhere"
    (p / name).symlink_to(target)
    with pytest.raises(ProfileError) as info:
        build(world)
    assert info.value.code == "protected_symlink"
    assert target not in info.value.message


def test_the_list_of_protected_shell_files_covers_the_policys(world):
    """C06's protected basenames are the source of truth for "which files are start-up files"; this list may be
    longer, never shorter."""
    from m2c.policy import rules
    assert set(rules._PROTECTED_BASENAMES) <= set(PROTECTED_RC_FILES) | {".git", ".m2c"}
    assert set(rules._PROTECTED_COMPONENTS) <= set(PROTECTED_DIRS)


# ==========================================================================
# the home directory: empty, and the secrets never mounted
# ==========================================================================
def test_the_home_is_an_empty_tmpfs_mounted_before_anything_under_it(world):
    layout, project, home = world
    args = build(world)
    tmpfs_at = next(i for i, a in enumerate(args) if a == "--tmpfs" and args[i + 1] == home)
    assert tmpfs_at < index_of(args, "--bind", project), "the project lives under the home: the tmpfs must come first"


def test_the_home_and_the_root_are_made_read_only_after_every_mount(world):
    layout, project, home = world
    (Path(home) / ".nvm").mkdir()
    args = build(world)
    ro_home = args.index("--remount-ro")
    remounts = [(i, args[i + 1]) for i, a in enumerate(args) if a == "--remount-ro"]
    assert [d for _, d in remounts] == [home, "/"]
    last_mount = max(i for i, a in enumerate(args) if a in ("--bind", "--ro-bind", "--tmpfs", "--symlink", "--proc", "--dev"))
    assert ro_home > last_mount, "a remount before a later mount would leave that mount's parent writable"


def test_secrets_under_the_home_are_never_mentioned(world):
    layout, project, home = world
    h = Path(home)
    planted = {}
    for rel in (".ssh", ".aws", ".gnupg", ".config", ".m2c", ".mozilla", ".kube", ".docker", ".password-store"):
        (h / rel).mkdir(exist_ok=True)
        planted[rel] = canary()
        (h / rel / "secret").write_text(planted[rel])
    for rel in (".netrc", ".git-credentials", ".npmrc", ".bashrc"):
        (h / rel).write_text(canary())
    args = build(world)
    joined = "\n".join(args)
    for rel in list(planted) + [".netrc", ".git-credentials", ".npmrc", ".bashrc"]:
        assert f"{home}/{rel}" not in joined, rel
    assert not any(text in joined for text in planted.values())


def test_toolchain_directories_are_mounted_read_only_and_only_if_they_exist(world):
    layout, project, home = world
    h = Path(home)
    (h / ".nvm").mkdir()
    (h / ".local" / "bin").mkdir(parents=True)
    args = build(world)
    ro = pairs(args, "--ro-bind")
    assert (f"{home}/.nvm", f"{home}/.nvm") in ro
    assert (f"{home}/.local/bin", f"{home}/.local/bin") in ro
    for absent in (".pyenv", ".cargo", ".rustup"):
        assert not any(d == f"{home}/{absent}" for _, d in ro), absent


def test_the_default_toolchain_list_is_the_one_in_the_work_package():
    assert set(DEFAULT_HOME_TOOLCHAIN_DIRS) == {".nvm", ".pyenv", ".cargo", ".rustup", ".local/bin"}


def test_the_toolchain_list_is_a_parameter(world):
    layout, project, home = world
    (Path(home) / ".sdkman").mkdir()
    (Path(home) / ".nvm").mkdir()
    args = build(world, toolchain_dirs=(".sdkman",))
    bound = {d for _, d in pairs(args, "--ro-bind")}
    assert f"{home}/.sdkman" in bound and f"{home}/.nvm" not in bound


def test_registry_credentials_inside_a_mounted_toolchain_are_masked(world):
    layout, project, home = world
    cargo = Path(home) / ".cargo"
    cargo.mkdir()
    (cargo / "credentials.toml").write_text(canary())
    (cargo / "bin").mkdir()
    args = build(world)
    mask = (os.devnull, f"{home}/.cargo/credentials.toml")
    assert mask in pairs(args, "--ro-bind")
    assert index_of(args, "--ro-bind", mask[1]) > index_of(args, "--ro-bind", f"{home}/.cargo")


def test_a_credentials_file_that_does_not_exist_is_not_masked(world):
    layout, project, home = world
    (Path(home) / ".cargo").mkdir()
    assert not any(a.endswith("credentials.toml") for a in build(world))


# ==========================================================================
# the extra read-only list
# ==========================================================================
def test_extra_read_only_directories_relative_to_the_home_or_absolute(world, tmp_path):
    layout, project, home = world
    (Path(home) / "tools").mkdir()
    opt = tmp_path / "opt-toolchain"
    opt.mkdir()
    args = build(world, extra_ro=("tools", "~/tools", str(opt), "does-not-exist"))
    bound = [d for _, d in pairs(args, "--ro-bind")]
    assert bound.count(f"{home}/tools") == 1, "a repeated entry is mounted once"
    assert os.path.realpath(opt) in bound
    assert not any("does-not-exist" in a for a in args)


@pytest.mark.parametrize("entry", [
    "~/.ssh", ".aws", "~/.config/gcloud", ".gnupg/private-keys-v1.d", ".m2c", "~/.mozilla/firefox",
    "~", ".", "/", "/etc", "/proc", "/dev", "/sys", "..",
])
def test_an_extra_entry_that_would_expose_a_secret_or_the_whole_home_is_refused(world, entry):
    layout, project, home = world
    for rel in (".ssh", ".aws", ".config/gcloud", ".gnupg/private-keys-v1.d", ".m2c", ".mozilla/firefox"):
        (Path(home) / rel).mkdir(parents=True, exist_ok=True)
    with pytest.raises(ProfileError) as info:
        build(world, extra_ro=(entry,))
    assert info.value.code == "extra_ro_denied"


def test_the_configured_directory_of_m2c_is_denied_wherever_it_lives(world, tmp_path):
    layout, project, home = world
    m2c_home = tmp_path / "somewhere-else" / "m2c-state"
    m2c_home.mkdir(parents=True)
    with pytest.raises(ProfileError) as info:
        build(world, extra_ro=(str(m2c_home),), denied=(str(m2c_home),))
    assert info.value.code == "extra_ro_denied"
    with pytest.raises(ProfileError):
        build(world, extra_ro=(str(m2c_home.parent),), denied=(str(m2c_home),))


def test_an_extra_entry_that_is_a_symlink_to_a_secret_is_refused(world):
    layout, project, home = world
    h = Path(home)
    (h / ".ssh").mkdir()
    (h / "innocent").symlink_to(h / ".ssh")
    with pytest.raises(ProfileError):
        build(world, extra_ro=("innocent",))


@pytest.mark.parametrize("bad", ["", "a\x00b", 5, None])
def test_a_malformed_extra_entry_is_refused(world, bad):
    with pytest.raises(ProfileError):
        build(world, extra_ro=(bad,))


def test_the_denied_list_is_the_documented_one():
    for name in (".ssh", ".aws", ".gnupg", ".config", ".m2c"):
        assert name in NEVER_MOUNT_HOME


# ==========================================================================
# the system directories
# ==========================================================================
def test_a_merged_usr_machine_gets_symlinks_for_bin_sbin_lib_and_lib64(world):
    args = build(world)
    links = pairs(args, "--symlink")
    assert ("usr/bin", "/bin") in links and ("usr/sbin", "/sbin") in links
    assert ("usr/lib", "/lib") in links and ("usr/lib64", "/lib64") in links
    ro = {d for _, d in pairs(args, "--ro-bind")}
    assert "/usr" in ro and not ({"/bin", "/sbin", "/lib", "/lib64"} & ro)


def test_a_machine_with_real_directories_gets_read_only_binds_instead(tmp_path):
    layout = detect_system_layout(os.fspath(make_root(tmp_path, merged_usr=False)))
    home = tmp_path / "home"
    (home / "p").mkdir(parents=True)
    args = build_profile(project=os.path.realpath(home / "p"), home=os.path.realpath(home), env=ENV, layout=layout)
    ro = {d for _, d in pairs(args, "--ro-bind")}
    assert {"/usr", "/bin", "/sbin", "/lib", "/lib64"} <= ro
    assert pairs(args, "--symlink") == []


def test_a_missing_lib64_is_simply_left_out(tmp_path):
    layout = detect_system_layout(os.fspath(make_root(tmp_path, lib64=False)))
    home = tmp_path / "home"
    (home / "p").mkdir(parents=True)
    args = build_profile(project=os.path.realpath(home / "p"), home=os.path.realpath(home), env=ENV, layout=layout)
    assert not any(a == "/lib64" for a in args)


def test_a_machine_without_usr_cannot_be_sandboxed(tmp_path):
    root = tmp_path / "empty-root"
    root.mkdir()
    with pytest.raises(ProfileError) as info:
        detect_system_layout(os.fspath(root))
    assert info.value.code == "no_usr"


def test_usr_is_bound_read_only_with_no_write_access(world):
    assert ("/usr" in {d for _, d in pairs(build(world), "--ro-bind")})
    assert "/usr" not in {d for _, d in pairs(build(world), "--bind")}


def test_only_the_listed_etc_entries_are_bound(tmp_path):
    names = ("passwd", "group", "ld.so.cache", "shadow", "sudoers", "ssh", "gshadow")
    root = make_root(tmp_path, etc=names)
    for sub in ("ssl/certs", "ssl/private"):
        (root / "etc" / sub).mkdir(parents=True)
    layout = detect_system_layout(os.fspath(root))
    home = tmp_path / "home"
    (home / "p").mkdir(parents=True)
    args = build_profile(project=os.path.realpath(home / "p"), home=os.path.realpath(home), env=ENV, layout=layout)
    etc_dests = {d for _, d in pairs(args, "--ro-bind") if d.startswith("/etc")}
    assert etc_dests <= {"/etc/" + e for e in ETC_ENTRIES}
    assert {"/etc/passwd", "/etc/group", "/etc/ld.so.cache", "/etc/ssl/certs"} <= etc_dests
    for forbidden in ("/etc/shadow", "/etc/sudoers", "/etc/ssh", "/etc/gshadow", "/etc/ssl", "/etc/ssl/private"):
        assert forbidden not in etc_dests


def test_the_etc_list_is_small_explicit_and_has_no_private_keys():
    assert "ssl/private" not in ETC_ENTRIES and "ssl" not in ETC_ENTRIES
    assert {"passwd", "group", "ld.so.cache", "alternatives", "ssl/certs"} <= set(ETC_ENTRIES)
    assert len(ETC_ENTRIES) <= 25


def test_proc_dev_and_a_private_tmp_are_present(world):
    args = build(world)
    assert pairs(args, "--tmpfs") == [] or "/tmp" in [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
    assert "--proc" in args and args[args.index("--proc") + 1] == "/proc"
    assert "--dev" in args and args[args.index("--dev") + 1] == "/dev"
    tmp = [args[i + 1] for i, a in enumerate(args) if a == "--tmpfs"]
    assert "/tmp" in tmp
    assert "/proc" not in {d for _, d in pairs(args, "--ro-bind")}, "the real /proc must never be bound"


def test_a_project_under_tmp_is_mounted_after_the_private_tmp(tmp_path):
    layout = detect_system_layout(os.fspath(make_root(tmp_path)))
    proj = os.path.realpath(tmp_path / "proj")
    os.makedirs(proj)
    home = os.path.realpath(tmp_path / "home")
    os.makedirs(home, exist_ok=True)
    args = build_profile(project=proj, home=home, env=ENV, layout=layout)
    tmp_at = next(i for i, a in enumerate(args) if a == "--tmpfs" and args[i + 1] == "/tmp")
    assert tmp_at < index_of(args, "--bind", proj)
    if proj.startswith("/tmp/"):
        assert tmp_at < next(i for i, a in enumerate(args) if a == "--tmpfs" and args[i + 1] == home)


# ==========================================================================
# the environment
# ==========================================================================
def test_the_environment_is_cleared_and_rebuilt_from_what_is_passed(world):
    layout, project, home = world
    args = build(world)
    assert args.count("--clearenv") == 1
    after = args[args.index("--clearenv"):]
    sets = {after[i + 1]: after[i + 2] for i, a in enumerate(after) if a == "--setenv"}
    assert sets["PATH"] == "/usr/bin:/bin" and sets["LANG"] == "C.UTF-8"
    assert sets["HOME"] == home, "HOME points at the empty home, not at what the caller passed"
    assert sets["TMPDIR"] == "/tmp"
    assert sets["PWD"] == project
    assert set(sets) == {"PATH", "LANG", "HOME", "TMPDIR", "PWD"}


def test_the_environment_is_never_inherited_by_omission(world):
    """Whatever the caller does not pass must not reach the command: the profile clears first and sets only
    what it was given."""
    layout, project, home = world
    args = build(world, env={"PATH": "/usr/bin"})
    assert args.index("--clearenv") < args.index("--setenv")
    names = [args[i + 1] for i, a in enumerate(args) if a == "--setenv"]
    assert sorted(names) == ["HOME", "PATH", "PWD", "TMPDIR"]


def test_invalid_environment_names_and_values_are_skipped_not_passed(world):
    args = build(world, env={"PATH": "/usr/bin", "A=B": "x", "": "x", "OK": "va\x00lue", "FINE": "multi\nline"})
    names = [args[i + 1] for i, a in enumerate(args) if a == "--setenv"]
    assert "A=B" not in names and "" not in names and "OK" not in names
    assert "FINE" in names


def test_the_environment_is_emitted_in_a_stable_order(world):
    env = {"PATH": "/usr/bin", "ZED": "1", "ALPHA": "2", "MID": "3"}
    assert build(world, env=env) == build(world, env=dict(reversed(list(env.items()))))


def test_the_working_directory_is_the_project_root(world):
    layout, project, home = world
    args = build(world)
    assert args[args.index("--chdir") + 1] == project


# ==========================================================================
# unusual project paths pass through byte for byte
# ==========================================================================
@pytest.mark.parametrize("name", [
    "my project",
    "semi;colon && rm -rf $HOME",
    "back`tick`",
    "dollar$(id)",
    "quote'single\"double",
    "-leading-dash",
    "glob*?[x]",
    "unicode-ünï-中文",
    "new\nline",
    "tab\there",
    "  spaces  ",
    "back\\slash",
])
def test_an_unusual_project_path_is_one_element_and_unchanged(tmp_path, name):
    layout = detect_system_layout(os.fspath(make_root(tmp_path)))
    home = tmp_path / "home"
    project = home / "code" / name
    project.mkdir(parents=True)
    project_s, home_s = os.path.realpath(project), os.path.realpath(home)
    args = build_profile(project=project_s, home=home_s, env=ENV, layout=layout)
    assert (project_s, project_s) in pairs(args, "--bind")
    assert args[args.index("--chdir") + 1] == project_s
    assert len([a for a in args if a == project_s]) >= 3, "bind source, bind destination and chdir"
    assert all(isinstance(a, str) for a in args)


def test_a_protected_file_with_an_unusual_name_inside_such_a_project(tmp_path):
    layout = detect_system_layout(os.fspath(make_root(tmp_path)))
    home = tmp_path / "home"
    project = home / "my proj; $(x)"
    project.mkdir(parents=True)
    (project / ".git").mkdir()
    ps = os.path.realpath(project)
    args = build_profile(project=ps, home=os.path.realpath(home), env=ENV, layout=layout)
    assert (f"{ps}/.git", f"{ps}/.git") in pairs(args, "--ro-bind")


# ==========================================================================
# projects and homes that cannot be sandboxed safely
# ==========================================================================
def test_a_relative_or_missing_project_is_refused(world):
    layout, project, home = world
    for bad, code in (("relative/path", "project_invalid"), (project + "/nope", "project_missing"), ("", "project_invalid")):
        with pytest.raises(ProfileError) as info:
            build_profile(project=bad, home=home, env=ENV, layout=layout)
        assert info.value.code == code, bad


def test_a_project_with_a_nul_byte_is_refused(world):
    layout, project, home = world
    with pytest.raises(ProfileError):
        build_profile(project=project + "\x00x", home=home, env=ENV, layout=layout)


def test_the_project_may_not_be_the_home_or_contain_it(world):
    """Binding the home read-write would expose every secret in it."""
    layout, project, home = world
    for bad in (home, os.path.dirname(home), "/"):
        with pytest.raises(ProfileError) as info:
            build_profile(project=bad, home=home, env=ENV, layout=layout)
        assert info.value.code in ("project_contains_secrets", "project_is_root"), bad


def test_the_project_may_not_contain_a_denied_directory(world, tmp_path):
    layout, project, home = world
    state = Path(project) / "state"
    state.mkdir()
    with pytest.raises(ProfileError) as info:
        build_profile(project=project, home=home, env=ENV, layout=layout, denied=(str(state),))
    assert info.value.code == "project_contains_secrets"


@pytest.mark.parametrize("path", ["/mnt/c/Users/someone/code/proj", "/mnt/d/work", "/mnt/c"])
def test_a_project_on_a_windows_drive_is_refused(world, path):
    layout, project, home = world
    with pytest.raises(ProfileError) as info:
        build_profile(project=path, home=home, env=ENV, layout=layout)
    assert info.value.code == "project_on_windows_drive"


def test_a_project_that_overlaps_a_system_directory_is_refused(world):
    layout, project, home = world
    for bad in ("/usr", "/usr/local/src", "/etc", "/proc", "/dev", "/tmp"):
        with pytest.raises(ProfileError):
            build_profile(project=bad, home=home, env=ENV, layout=layout)


def test_an_unusable_home_is_refused(world):
    layout, project, home = world
    for bad in ("/", "", "relative", "/tmp", "/usr"):
        with pytest.raises(ProfileError):
            build_profile(project=project, home=bad, env=ENV, layout=layout)


def test_errors_carry_fixed_text_and_never_a_path(world):
    layout, project, home = world
    secret_dir = "/very/secret/place-" + secrets.token_hex(4)
    with pytest.raises(ProfileError) as info:
        build_profile(project=secret_dir, home=home, env=ENV, layout=layout)
    assert secret_dir not in str(info.value) and secret_dir not in info.value.message
    assert info.value.code in prof.PROFILE_ERROR_MESSAGES
    assert info.value.message == prof.PROFILE_ERROR_MESSAGES[info.value.code]


def test_the_profile_does_not_touch_anything_it_only_looks(world):
    layout, project, home = world
    before = sorted(str(p) for p in Path(home).rglob("*"))
    build(world)
    assert before == sorted(str(p) for p in Path(home).rglob("*"))
