"""
m2c/tests/sandbox/test_sandbox_escape.py -- Patch C11a: real escape attempts against the real bubblewrap.

Every test here runs a command through the shell tool in ``sandbox="required"`` mode (so a missing sandbox is an
error, never an unsandboxed run) and checks TWO things, because a command can lie or fail quietly:
what the command saw, and what is on the real disk or in the real process table afterwards.

The world: a fake home (``$HOME`` is the suite's temporary home) with canaries in ``~/.ssh``, ``~/.aws``,
``~/.config`` and ``~/.m2c``, a project inside it with ``.git``, ``.m2c`` and shell rc files, and an "outside"
directory under ``/var/tmp`` that the sandbox does not mount.  Every canary is built at run time.

These tests need bubblewrap and user namespaces.  When they are missing every test is SKIPPED with a loud
"SKIPPED: <reason>" line on stderr (and the reason in pytest's -rs summary); nothing here silently passes.

Run (Ubuntu / WSL2 Ubuntu):   sudo apt install bubblewrap && cd m2c && pytest tests/sandbox -v -rs

What is NOT proved here, on purpose: bubblewrap itself and the kernel are trusted.  A kernel exploit, a
vulnerable ``bwrap`` or a command that abuses the project directory (it IS writable) is outside this patch.
"""
from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from m2c import sandbox
from m2c.tools import ToolContext, ToolError
from m2c.tools.shell import AUTH_USER_APPROVED, ShellConfig, configure_shell, run_command

pytestmark = [
    pytest.mark.real_sandbox,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the sandbox is Linux only"),
]

_STATUS: dict = {}
RAN_BEGIN, RAN_END = "M2C_SCRIPT_BEGIN", "M2C_SCRIPT_END"


def _loud_skip(reason: str) -> None:
    sys.__stderr__.write(f"\nSKIPPED: real-sandbox escape tests: {reason}\n")
    sys.__stderr__.flush()
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def need_sandbox():
    if "result" not in _STATUS:
        _STATUS["result"] = sandbox.fresh_probe()
    result = _STATUS["result"]
    if not result.works:
        _loud_skip(result.describe())


def token(prefix="m2c-canary") -> str:
    return f"{prefix}-{secrets.token_hex(10)}"


# ==========================================================================
# the world
# ==========================================================================
class World:
    def __init__(self, tmp_path: Path):
        self.home = Path(os.environ["HOME"]).resolve()
        self.project = self.home / "proj"
        self.project.mkdir(parents=True)
        self.canaries = {}
        for rel in (".ssh/id_test", ".aws/credentials", ".config/tool/secret", ".m2c/state", ".bashrc", ".netrc"):
            path = self.home / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            self.canaries[rel] = token()
            path.write_text(self.canaries[rel])
        for rel in (".git/config", ".m2c/state", ".bashrc", ".profile", ".zshrc", "README.md"):
            path = self.project / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            self.canaries[f"proj/{rel}"] = token()
            path.write_text(self.canaries[f"proj/{rel}"])
        base = "/var/tmp" if os.path.isdir("/var/tmp") and os.access("/var/tmp", os.W_OK) else tempfile.gettempdir()
        self.outside = Path(tempfile.mkdtemp(prefix="m2c-outside-", dir=base))
        (self.outside / "keep").write_text(token())
        self.context = self.new_context()

    def new_context(self, **cfg) -> ToolContext:
        context = ToolContext(self.project)
        configure_shell(context, ShellConfig(sandbox="required", kill_grace_s=0.5, **cfg))
        return context

    def sh(self, command: str, timeout_s: int = 30, context=None, **kw):
        return run_command(context or self.context, command, timeout_s, authorization=AUTH_USER_APPROVED, **kw)

    def sh_ran(self, script: str, **kw):
        """Like ``sh``, but the script is bracketed by markers and the result is only returned when BOTH were printed, so an
        assertion that something is ABSENT from the output can never pass just because the script did not run."""
        r = self.sh(f"echo {RAN_BEGIN}; {script}\necho {RAN_END}", **kw)
        assert RAN_BEGIN in r.output and RAN_END in r.output, f"the script did not run to its end: {r.output}"
        return r

    def read(self, rel: str) -> str:
        return (self.project / rel).read_text()

    def assert_untouched(self, *rels):
        for rel in rels:
            assert self.read(rel) == self.canaries[f"proj/{rel}"], f"{rel} was changed"

    def close(self):
        shutil.rmtree(self.outside, ignore_errors=True)


@pytest.fixture
def w(tmp_path):
    world = World(tmp_path)
    yield world
    world.close()


def host_processes_with(needle: str) -> list[int]:
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if needle.encode() in (entry / "cmdline").read_bytes():
                found.append(int(entry.name))
        except OSError:
            continue
    return found


def wait_gone(needle: str, seconds=5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not [p for p in host_processes_with(needle) if _alive(p)]:
            return True
        time.sleep(0.05)
    return False


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


# ==========================================================================
# the shell tool works inside
# ==========================================================================
def test_a_normal_command_works_and_runs_inside_the_sandbox(w):
    r = w.sh("echo hello; pwd; cat /proc/1/comm")
    assert r.ok and "hello" in r.output and str(w.project) in r.output
    assert "bwrap" in r.output, "PID 1 inside is bubblewrap: the command really runs in its own PID namespace"


def test_the_project_is_writable_and_what_is_written_reaches_the_disk(w):
    r = w.sh("echo new > created.txt && mkdir -p sub/dir && echo deep > sub/dir/f && echo changed > README.md && mv created.txt renamed.txt && rm sub/dir/f")
    assert r.ok, r.output
    assert (w.project / "renamed.txt").read_text() == "new\n" and not (w.project / "created.txt").exists()
    assert (w.project / "sub" / "dir").is_dir() and not (w.project / "sub/dir/f").exists()
    assert w.read("README.md") == "changed\n"
    assert (w.project / "renamed.txt").stat().st_uid == os.getuid(), "files belong to the real user"


def test_protected_files_can_still_be_read(w):
    r = w.sh("cat .git/config; cat .m2c/state; cat .bashrc; ls .git")
    assert r.ok
    for rel in (".git/config", ".m2c/state", ".bashrc"):
        assert w.canaries[f"proj/{rel}"] in r.output


def test_the_system_toolchain_is_there_and_read_only(w):
    r = w.sh("ls /usr/bin/env /bin/sh >/dev/null && echo tools-ok; touch /usr/bin/x 2>&1; echo rc=$?")
    assert "tools-ok" in r.output and "rc=1" in r.output


# ==========================================================================
# writes outside the project
# ==========================================================================
def test_a_write_outside_the_project_fails_and_nothing_reaches_the_host(w):
    targets = [w.outside / "pwned", w.home / "pwned", Path("/usr/pwned"), Path("/etc/pwned"), Path("/pwned"), w.project.parent / "pwned"]
    script = "; ".join(f"echo x > {t} 2>/dev/null && echo WROTE:{i}" for i, t in enumerate(targets))
    r = w.sh_ran(script + "; true")
    assert "WROTE" not in r.output, r.output
    assert not any(t.exists() for t in targets)
    assert [p.name for p in w.outside.iterdir()] == ["keep"]


def test_cd_up_then_write_fails(w):
    r = w.sh_ran("cd .. && touch up-file 2>&1; echo rc1=$?; cd / && touch root-file 2>&1; echo rc2=$?; cd /usr && touch u 2>&1; echo rc3=$?")
    assert "rc1=0" not in r.output and "rc2=0" not in r.output and "rc3=0" not in r.output, r.output
    assert "Read-only file system" in r.output or "Permission denied" in r.output
    assert not (w.home / "up-file").exists() and not Path("/root-file").exists()


def test_the_home_directory_itself_is_read_only(w):
    r = w.sh_ran("touch ~/newfile 2>&1; echo rc=$?; mkdir ~/newdir 2>&1; echo rc=$?; echo $HOME")
    assert r.output.count("rc=") == 2 and r.output.count("rc=0") == 0
    assert not (w.home / "newfile").exists() and not (w.home / "newdir").exists()


def test_a_symlink_created_in_the_project_cannot_reach_outside(w):
    r = w.sh(f"ln -s {w.outside} out && echo x > out/pwned 2>&1; echo rc=$?; ln -s {w.home}/.ssh ssh-link; cat ssh-link/id_test 2>&1; echo done")
    assert "rc=0" not in r.output and w.canaries[".ssh/id_test"] not in r.output
    assert not (w.outside / "pwned").exists()


def test_a_symlink_that_was_already_in_the_project_cannot_reach_outside(w):
    (w.project / "outlink").symlink_to(w.outside)
    (w.project / "sshlink").symlink_to(w.home / ".ssh")
    r = w.sh("echo x > outlink/pwned 2>&1; echo rc=$?; cat sshlink/id_test 2>&1; ls outlink 2>&1; echo done")
    assert "rc=0" not in r.output and w.canaries[".ssh/id_test"] not in r.output
    assert not (w.outside / "pwned").exists() and [p.name for p in w.outside.iterdir()] == ["keep"]


def test_the_project_directory_itself_cannot_be_renamed_or_removed(w):
    r = w.sh("cd / ; mv " + str(w.project) + " " + str(w.project) + "-moved 2>&1; echo rc=$?; rmdir " + str(w.project) + " 2>&1; echo rc=$?")
    assert "rc=0" not in r.output and w.project.is_dir() and not Path(str(w.project) + "-moved").exists()


# ==========================================================================
# .git, .m2c and the shell rc files
# ==========================================================================
@pytest.mark.parametrize("name", [".git", ".m2c"])
def test_protected_directories_cannot_be_changed_replaced_moved_or_removed(w, name):
    target = ".git/config" if name == ".git" else ".m2c/state"
    r = w.sh(
        f"echo x > {name}/new 2>&1; echo a=$?; echo x >> {target} 2>&1; echo b=$?; "
        f"mv {name} {name}.bak 2>&1; echo c=$?; rm -rf {name} 2>&1; echo d=$?; "
        f"mkdir {name}/sub 2>&1; echo e=$?; ln -s /tmp {name}/link 2>&1; echo f=$?; rm {target} 2>&1; echo g=$?; true"
    )
    for letter in "abcdefg":
        assert f"{letter}=0" not in r.output, f"{letter}: {r.output}"
    assert (w.project / name).is_dir() and not (w.project / f"{name}.bak").exists()
    assert w.read(target) == w.canaries[f"proj/{target}"]
    assert sorted(p.name for p in (w.project / name).iterdir()) == sorted({".git": ["config"], ".m2c": ["state"]}[name])


@pytest.mark.parametrize("name", [".bashrc", ".profile", ".zshrc"])
def test_protected_rc_files_cannot_be_changed_replaced_or_removed(w, name):
    r = w.sh(
        f"echo 'evil' >> {name} 2>&1; echo a=$?; echo evil > {name} 2>&1; echo b=$?; rm -f {name} 2>&1; echo c=$?; "
        f"mv {name} {name}.bak 2>&1; echo d=$?; ln -sf /tmp/x {name} 2>&1; echo e=$?; chmod 777 {name} 2>&1; echo f=$?; true"
    )
    for letter in "abcdef":
        assert f"{letter}=0" not in r.output, f"{letter}: {r.output}"
    w.assert_untouched(name)
    assert not (w.project / f"{name}.bak").exists() and not (w.project / name).is_symlink()


def test_a_protected_rc_file_that_does_not_exist_is_not_protected_a_documented_limit(w):
    """Bubblewrap can only bind what exists.  ``.bash_login`` is absent, so the command may create it.  This test pins
    that limit (profile.py's docstring states it) so that closing it later is a visible, deliberate change."""
    r = w.sh("echo x > .bash_login && echo created")
    assert "created" in r.output and (w.project / ".bash_login").exists()


def test_a_git_directory_the_command_creates_itself_is_not_protected_a_documented_limit(w):
    shutil.rmtree(w.project / ".git")
    context = w.new_context()
    r = w.sh("mkdir .git && echo hooks > .git/hook && echo created", context=context)
    assert "created" in r.output
    # ... and the very next command is protected, because .git now exists
    r2 = w.sh("echo more >> .git/hook 2>&1; echo rc=$?", context=context)
    assert "rc=0" not in r2.output and (w.project / ".git/hook").read_text() == "hooks\n"


def test_the_model_cannot_turn_a_missing_git_into_a_symlink_to_disable_the_sandbox(w):
    shutil.rmtree(w.project / ".git")
    r = w.sh(f"ln -s {w.outside} .git && echo linked")
    assert "linked" in r.output and (w.project / ".git").is_symlink()
    with pytest.raises(ToolError) as info:
        w.sh("echo second command")
    assert info.value.code == "refused" and "symlink" in str(info.value)


# ==========================================================================
# home secrets
# ==========================================================================
def test_the_home_secrets_are_not_visible(w):
    r = w.sh(
        "cat ~/.ssh/id_test 2>&1; cat ~/.aws/credentials 2>&1; cat ~/.config/tool/secret 2>&1; cat ~/.m2c/state 2>&1; "
        "cat ~/.bashrc 2>&1; cat ~/.netrc 2>&1; ls -A ~ 2>&1; ls -d ~/.ssh ~/.aws ~/.config ~/.gnupg 2>&1; true"
    )
    for rel in (".ssh/id_test", ".aws/credentials", ".config/tool/secret", ".m2c/state", ".bashrc", ".netrc"):
        assert w.canaries[rel] not in r.output, rel
    assert r.output.count("No such file or directory") >= 6
    listing = [line for line in r.output.splitlines() if line.strip() == "proj"]
    assert listing, "the home holds the project and nothing else"
    for name in (".ssh", ".aws", ".config", ".m2c", ".gnupg"):
        assert f"\n{name}\n" not in "\n" + r.output + "\n"


def test_the_secrets_are_not_reachable_through_proc_or_the_host_path(w):
    r = w.sh(f"cat /proc/1/root{w.home}/.ssh/id_test 2>&1; cat /proc/self/root{w.home}/.aws/credentials 2>&1; ls /proc/1/root/var/tmp 2>&1; true")
    assert w.canaries[".ssh/id_test"] not in r.output and w.canaries[".aws/credentials"] not in r.output
    assert w.outside.name not in r.output


def test_only_the_listed_etc_entries_exist(w):
    r = w.sh("ls /etc; echo ---; cat /etc/shadow 2>&1; cat /etc/sudoers 2>&1; ls /etc/ssh 2>&1; ls /etc/ssl 2>&1; true")
    listing, _, rest = r.output.partition("---")
    allowed = {p.split("/")[0] for p in sandbox.profile.ETC_ENTRIES} | {"[stdout]"}
    names = {line.strip() for line in listing.splitlines() if line.strip() and not line.startswith("[")}
    assert names <= allowed, names - allowed
    assert "passwd" in names and "shadow" not in names and "ssh" not in names
    assert "No such file" in rest
    assert "private" not in rest.split("ls: cannot access '/etc/ssh'")[-1]


def test_toolchain_directories_are_read_only_and_credentials_in_them_are_masked(w):
    (w.home / ".nvm" / "bin").mkdir(parents=True)
    (w.home / ".nvm" / "bin" / "node-tool").write_text("tool-body")
    (w.home / ".cargo").mkdir()
    (w.home / ".cargo" / "credentials.toml").write_text(token())
    context = w.new_context()
    r = w.sh(
        "cat ~/.nvm/bin/node-tool; echo; echo x >> ~/.nvm/bin/node-tool 2>&1; echo a=$?; rm ~/.nvm/bin/node-tool 2>&1; echo b=$?; "
        "cat ~/.cargo/credentials.toml 2>&1; echo '<end>'; echo y >> ~/.cargo/credentials.toml 2>&1; echo c=$?; true",
        context=context,
    )
    assert "tool-body" in r.output and "a=0" not in r.output and "b=0" not in r.output and "c=0" not in r.output
    cargo_secret = (w.home / ".cargo" / "credentials.toml").read_text()
    assert cargo_secret not in r.output, "the registry credential file is masked with an empty file"
    assert (w.home / ".nvm" / "bin" / "node-tool").read_text() == "tool-body"


def test_extra_read_only_paths_are_readable_and_not_writable(w):
    extra = Path(tempfile.mkdtemp(prefix="m2c-extra-", dir=w.outside.parent))
    try:
        (extra / "lib.txt").write_text("library")
        context = w.new_context(sandbox_extra_ro=(str(extra),))
        r = w.sh(f"cat {extra}/lib.txt; echo; echo x >> {extra}/lib.txt 2>&1; echo a=$?; touch {extra}/new 2>&1; echo b=$?; true", context=context)
        assert "library" in r.output and "a=0" not in r.output and "b=0" not in r.output
        assert (extra / "lib.txt").read_text() == "library" and not (extra / "new").exists()
    finally:
        shutil.rmtree(extra, ignore_errors=True)


def test_an_extra_path_that_would_expose_a_secret_refuses_every_command(w):
    context = w.new_context(sandbox_extra_ro=("~/.ssh",))
    with pytest.raises((ToolError,)) as info:
        w.sh("echo hi", context=context)
    assert info.value.code == "refused"


# ==========================================================================
# the environment
# ==========================================================================
def test_secrets_in_the_environment_do_not_reach_the_command(w, monkeypatch):
    name = "FAKE_SERVICE_" + "TOK" + "EN"
    value = token("envcanary")
    monkeypatch.setenv(name, value)
    monkeypatch.setenv("AWS_FAKE_SETTING", value)
    monkeypatch.setenv("INNOCENT_SETTING", "visible")
    r = w.sh("env; echo ---; cat /proc/1/environ | tr '\\0' '\\n'; true")
    assert value not in r.output
    assert "INNOCENT_SETTING=visible" in r.output, "only credentials are stripped, as in C07"
    assert f"HOME={w.home}" in r.output and f"PWD={w.project}" in r.output and "TMPDIR=/tmp" in r.output


# ==========================================================================
# the network
# ==========================================================================
def test_the_network_is_unreachable(w):
    r = w.sh(
        "cat /proc/net/dev | tail -n +3 | cut -d: -f1 | tr -d ' ' | sed 's/^/iface:/'; "
        "bash -c 'exec 3<>/dev/tcp/192.0.2.1/53' 2>&1 && echo CONNECTED; "
        "bash -c 'exec 3<>/dev/tcp/1.1.1.1/443' 2>&1 && echo CONNECTED; true",
        timeout_s=20,
    )
    assert "CONNECTED" not in r.output
    assert [l for l in r.output.splitlines() if l.startswith("iface:")] == ["iface:lo"]
    assert "Network is unreachable" in r.output or "Connection refused" in r.output or "No route" in r.output


def test_the_hosts_loopback_services_are_not_reachable(w):
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(5)
    server.settimeout(1.0)
    port = server.getsockname()[1]
    try:
        r = w.sh_ran(f"bash -c 'exec 3<>/dev/tcp/127.0.0.1/{port}' 2>&1 && echo CONNECTED; true")
        assert "CONNECTED" not in r.output
        with pytest.raises(socket.timeout):
            server.accept()
    finally:
        server.close()


@pytest.mark.skipif(shutil.which("python3") is None, reason="needs python3 on the host (and in the sandbox)")
def test_abstract_unix_sockets_of_the_host_are_not_reachable(w):
    name = "\0m2c-escape-" + secrets.token_hex(6)
    server = socket.socket(socket.AF_UNIX)
    server.bind(name)
    server.listen(1)
    server.settimeout(1.0)
    try:
        code = "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); print('CONNECTED')"
        r = w.sh(f"/usr/bin/python3 -c \"{code}\" '\\0{name[1:]}' 2>&1 | tail -1; true")
        code2 = f"import socket; s=socket.socket(socket.AF_UNIX); s.connect('\\0{name[1:]}'); print('CONNECTED')"
        r2 = w.sh(f"/usr/bin/python3 -c \"{code2}\" 2>&1 | tail -1; true")
        assert "CONNECTED" not in r.output and "CONNECTED" not in r2.output
        with pytest.raises(socket.timeout):
            server.accept()
    finally:
        server.close()


# ==========================================================================
# processes
# ==========================================================================
def test_host_processes_are_not_visible_and_cannot_be_signalled(w):
    """Two throw-away host processes the test starts itself; the command tries to see and kill them.

    SAFETY OF THE TEST ITSELF: it must never be dangerous when the sandbox is broken.  An earlier version also ran
    ``kill -9 -1``, which in a sandbox WITHOUT a PID namespace signals every process the user owns (as root: the whole
    machine; as a person: their terminal and editor), so a regression would have killed the developer's session instead
    of failing a test.  Only the two victims are ever signalled here.
    """
    marker = token("hostproc")
    victims = [
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", marker + str(i)], stdin=subprocess.DEVNULL)
        for i in range(2)
    ]
    try:
        pids = " ".join(str(v.pid) for v in victims)
        # ``[h]ostproc...`` matches the victims' command lines but not this script's own text, which contains the pattern.
        r = w.sh(
            f"echo BEGIN; kill -9 {pids} 2>&1; echo kill_rc=$?; "
            f"for d in /proc/[0-9]*; do tr '\\0' ' ' < $d/cmdline 2>/dev/null; done | grep -c '[{marker[0]}]{marker[1:10]}'; "
            "ls -d /proc/[0-9]* | wc -l; echo END; true"
        )
        assert "BEGIN" in r.output and "END" in r.output, f"the script did not run to the end: {r.output}"
        assert all(v.poll() is None and _alive(v.pid) for v in victims), "the host processes must have survived"
        assert "kill_rc=0" not in r.output, "kill must have failed (no such process in this PID namespace)"
        lines = [x.strip() for x in r.output.splitlines()]
        found = int(lines[lines.index("END") - 2]) if lines.index("END") >= 2 else -1
        visible = int(lines[lines.index("END") - 1])
        assert found == 0, f"a host process is visible inside: {r.output}"
        assert 1 <= visible <= 12, f"only the sandbox's own few processes are visible: {r.output}"
    finally:
        for v in victims:
            v.kill()
            v.wait()


def test_the_command_runs_without_capabilities_even_when_the_suite_runs_as_root(w):
    r = w.sh("grep -E 'CapEff|CapBnd|NoNewPrivs' /proc/self/status; mount -o remount,rw / 2>&1; echo rc=$?; chroot / true 2>&1; echo rc=$?; true")
    assert "CapEff:\t0000000000000000" in r.output and "CapBnd:\t0000000000000000" in r.output
    assert "rc=0" not in r.output


def test_the_command_has_no_controlling_terminal(w):
    r = w.sh("cut -d' ' -f7 /proc/self/stat; ls /dev/tty 2>&1; cat /dev/tty 2>&1 | head -1; true")
    assert r.output.split("\n")[1].strip() == "0", "tty_nr must be 0: a new session, so no TIOCSTI keystroke injection"


def _run_argv_on_a_terminal(argv: list[str], env: dict[str, str], seconds: float = 30.0) -> str:
    """Run ``argv`` with a real pseudo-terminal as its controlling terminal (what a person's shell has) and return what it printed."""
    import pty
    import select

    pid, fd = pty.fork()
    if pid == 0:  # child: a session leader whose controlling terminal is the pty
        try:
            os.execve(argv[0], argv, env)
        finally:
            os._exit(127)
    chunks: list[bytes] = []
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    data = os.read(fd, 4096)
                except OSError:  # EIO: the child side closed
                    break
                if not data:
                    break
                chunks.append(data)
        else:
            os.kill(pid, 9)
    finally:
        os.close(fd)
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
    return b"".join(chunks).decode("utf-8", "replace")


def test_new_session_alone_removes_the_controlling_terminal_even_when_started_from_one(w):
    """The shell tool also starts bubblewrap in a new session, which hides a missing ``--new-session`` from every test that
    goes through it.  Here the profile is run by itself from a process that HAS a controlling terminal (a person's own shell,
    or the probe's runner): without ``--new-session`` the command would share it and could push keystrokes into it (TIOCSTI)."""
    argv = sandbox.wrap_command(
        "cut -d' ' -f7 /proc/self/stat; echo TTY_DONE", project=str(w.project), env={"PATH": "/usr/bin:/bin"},
        home=str(w.home), denied=sandbox.default_denied(),
    )
    out = _run_argv_on_a_terminal(argv, sandbox.bwrap.outer_environment())
    lines = [line.strip() for line in out.replace("\r", "").splitlines() if line.strip()]
    assert "TTY_DONE" in lines, f"the command did not run to its end: {out!r}"
    assert lines[lines.index("TTY_DONE") - 1] == "0", f"tty_nr must be 0 (no controlling terminal), got: {out!r}"


def test_the_profile_alone_clears_the_environment_even_when_bubblewrap_is_started_with_a_full_one(w):
    """The shell tool starts bubblewrap with a PATH-only environment, which hides a missing ``--clearenv`` from every test that
    goes through it.  Here bubblewrap gets the WHOLE test environment plus a secret, as it would after a careless change to
    ``outer_environment``; the profile on its own must still give the command only what it was passed."""
    value = token("outer-secret")
    argv = sandbox.wrap_command(
        "env; echo ENV_DONE", project=str(w.project), env={"PATH": "/usr/bin:/bin", "INNOCENT_SETTING": "visible"},
        home=str(w.home), denied=sandbox.default_denied(),
    )
    done = subprocess.run(
        argv, env={**os.environ, "M2C_OUTER_SECRET": value}, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    assert "ENV_DONE" in done.stdout and done.returncode == 0, done.stderr
    assert value not in done.stdout and "M2C_OUTER_SECRET" not in done.stdout
    assert "INNOCENT_SETTING=visible" in done.stdout and f"HOME={w.home}" in done.stdout


def test_a_process_that_leaves_the_group_does_not_survive_the_call(w):
    """C07 documented this as a limit (``setsid`` escapes the group kill).  Inside the sandbox it is closed: the signalled
    process is bubblewrap, and the kernel kills the whole PID namespace with it."""
    marker = f"3600.{secrets.randbelow(10**6):06d}"
    r = w.sh(f"setsid sleep {marker} </dev/null >/dev/null 2>&1 & echo started", timeout_s=20)
    assert r.ok and "started" in r.output
    assert wait_gone(marker), "a setsid'd process survived the call"


def test_a_process_that_ignores_sigterm_is_killed_after_the_timeout(w):
    marker = f"3700.{secrets.randbelow(10**6):06d}"
    started = time.monotonic()
    r = w.sh(f"trap '' TERM; setsid sleep {marker} & trap '' TERM; while true; do sleep 1; done", timeout_s=2)
    assert r.code == "timeout" and time.monotonic() - started < 15
    assert wait_gone(marker)


def test_a_forkbomb_style_burst_is_contained_to_the_call(w):
    marker = f"3800.{secrets.randbelow(10**6):06d}"
    r = w.sh(f"for i in 1 2 3 4 5 6 7 8; do (setsid sleep {marker} &) ; done; echo spawned", timeout_s=20)
    assert r.ok and wait_gone(marker)


# ==========================================================================
# /tmp and /dev
# ==========================================================================
def test_tmp_is_private_in_both_directions(w):
    host_file = Path(tempfile.mkstemp(prefix="m2c-hosttmp-", dir="/tmp")[1])
    try:
        host_file.write_text(token())
        inside_name = f"m2c-inside-{secrets.token_hex(5)}"
        r = w.sh(f"ls /tmp; echo ---; echo inside > /tmp/{inside_name} && echo wrote; ls /tmp")
        assert host_file.name not in r.output
        assert "wrote" in r.output and not (Path("/tmp") / inside_name).exists()
    finally:
        host_file.unlink(missing_ok=True)


def test_dev_is_minimal_and_nothing_written_there_reaches_the_host(w):
    name = f"m2c-dev-{secrets.token_hex(5)}"
    r = w.sh(f"ls /dev; touch /dev/{name} 2>&1; echo rc=$?; cat /dev/mem 2>&1 | head -c 1; ls /dev/sda* /dev/nvme* 2>&1 | head -2; true")
    listing = r.output.split("\n")
    for forbidden in ("sda", "nvme", "mem", "kmsg", "disk"):
        assert forbidden not in [l.strip() for l in listing], forbidden
    assert not Path("/dev", name).exists()


# ==========================================================================
# the failure is clean
# ==========================================================================
def test_a_command_that_fails_inside_the_sandbox_reports_like_any_other(w):
    r = w.sh("echo oops >&2; exit 7")
    assert (r.ok, r.exit_code, r.code) == (False, 7, "exit_nonzero") and "oops" in r.output and "[exit status: 7]" in r.output
