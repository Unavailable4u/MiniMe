"""
m2c/src/m2c/sandbox/probe.py -- Patch C11a: the sandbox self-test.

SECURITY-CRITICAL.  "bubblewrap is installed" proves nothing: a kernel that forbids user
namespaces, a container that cannot mount ``/proc``, an AppArmor rule or one wrong option all
leave a ``bwrap`` binary that either refuses to start or starts and confines nothing.  So the
rule of this package is: the sandbox is "active" only after it has been seen to HOLD.  This
module is the seeing.

What it does.  ``run_probe`` builds a throw-away world in a temporary directory: a FAKE HOME
with canaries in ``.ssh``, ``.aws`` and ``.bashrc``, and a FAKE PROJECT inside it with
``.git``, ``.m2c``, ``.bashrc`` and ``.profile``.  Then, with the SAME profile the shell tool
uses (``profile.build_profile``, the same toolchain and extra read-only settings), it starts
one bubblewrap per check, each running a small POSIX script that tries to do something the
sandbox must prevent, and judges the result from two independent sources: what the script
printed AND what is on the real disk afterwards (a write that "failed" quietly but happened is
caught; so is an escape that printed nothing).

The checks (``CHECK_NAMES``; each is one run):

``basic``                a normal command works: ``echo`` and writing a file inside the project.
``write_outside_project``  writes to the home, ``/usr``, ``/etc``, ``/`` and ``..`` all fail.
``write_git``            nothing can be added to, appended to, moved out of, or removed from ``.git``.
``write_m2c``            the same for ``.m2c``.
``home_secrets_hidden``  the ``~/.ssh`` and ``~/.aws`` canaries cannot be read and the home is empty.
``write_rc_file``        a protected shell start-up file cannot be appended to, removed or moved.
``network_unreachable``  only a loopback interface exists and a connect to a non-routable address fails.
``cd_up_write``          ``cd ..`` and ``cd /`` followed by a write fail.
``pid_isolation``        a process the probe started on the host is not visible inside.
``private_tmp``          a file in the host's ``/tmp`` is not visible, and a file written to ``/tmp``
                         inside does not appear on the host.
``env_cleared``          a variable in the outer environment does not reach the command, and ``HOME``
                         is the empty home.

A check passes only with POSITIVE evidence that the script ran (its last line, ``PROBE_RAN``, and
exit status 0), so a sandbox that failed to start can never pass an "it was prevented" check by
accident.  If ``basic`` cannot even start, the rest are not run: the reason is classified into a
fixed code (``userns_unavailable``, ``proc_unavailable``, ``netns_unavailable``, ``bwrap_failed``,
``timeout``) from bubblewrap's error text, and the text itself is discarded.

The result (``ProbeResult``): ``works`` yes or no, ``reason`` (fixed text; never a path, never a
command, never output), ``reason_code``, one ``CheckOutcome`` per check run and the bubblewrap
path found.  It is cached for the session per settings (``session_probe``; ``reset_session_cache``
for tests and for a doctor that wants a fresh look), and ``run_probe`` always runs fresh.

Testable without bubblewrap: every process is started through an injectable ``runner(argv, *,
env, timeout_s) -> RunResult``; tests pass a fake for each failure.  The default runner starts a
real process in its own session and kills the whole group on timeout.

Fails closed: any unexpected exception during the probe is "does not work" (``probe_error``).
Logging fails open and carries only words and codes: ``sandbox.probe status=ok|failed code=...
count=<checks run>``.  No command and no output is ever logged.

Standard library only.  Place this file at: m2c/src/m2c/sandbox/probe.py
"""
from __future__ import annotations

import logging
import os
import secrets
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ..logging_setup import log_event
from .bwrap import find_bwrap, find_shell, outer_environment
from .profile import (
    DEFAULT_HOME_TOOLCHAIN_DIRS,
    PROFILE_ERROR_MESSAGES,
    ProfileError,
    SystemLayout,
    build_profile,
    detect_system_layout,
)

__all__ = [
    "CHECK_NAMES", "CheckOutcome", "ProbeResult", "REASONS", "RunResult", "classify_start_failure", "default_runner",
    "reset_session_cache", "run_probe", "session_probe",
]

logger = logging.getLogger("m2c.sandbox.probe")

CHECK_NAMES: tuple[str, ...] = (
    "basic", "write_outside_project", "write_git", "write_m2c", "home_secrets_hidden", "write_rc_file",
    "network_unreachable", "cd_up_write", "pid_isolation", "private_tmp", "env_cleared",
)

SENTINEL = "PROBE_RAN"
PROBE_TIMEOUT_S = 15.0
_MAX_CAPTURE_BYTES = 65_536

# The words a person sees.  Fixed text: nothing from bubblewrap's own messages, a path or a command is ever added.
REASONS: dict[str, str] = {
    **PROFILE_ERROR_MESSAGES,
    "bwrap_missing": "bubblewrap (bwrap) is not installed",
    "userns_unavailable": "bubblewrap could not create a user namespace (the kernel, or a policy such as AppArmor or a sysctl, forbids unprivileged ones)",
    "proc_unavailable": "bubblewrap could not mount a private /proc (common inside containers that restrict mounts)",
    "netns_unavailable": "bubblewrap could not create an isolated network namespace",
    "bwrap_failed": "bubblewrap started but could not set the sandbox up",
    "timeout": "the sandbox self-test did not finish in time",
    "check_failed": "the sandbox started but did not hold",
    "probe_error": "the sandbox self-test could not be completed",
}


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class RunResult:
    """What one process run gave back."""

    returncode: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False


@dataclass(frozen=True)
class CheckOutcome:
    """One check: its name (from CHECK_NAMES), whether it passed, and a fixed code (``ok``, ``violated``,
    ``setup_failed`` or ``timeout``)."""

    name: str
    passed: bool
    code: str


@dataclass(frozen=True)
class ProbeResult:
    """The verdict.  ``works`` is True only when every check in CHECK_NAMES was run and passed."""

    works: bool
    reason_code: str
    reason: str
    checks: tuple[CheckOutcome, ...] = ()
    bwrap_path: str | None = None
    duration_ms: int = 0

    @property
    def failed(self) -> tuple[str, ...]:
        """The names of the checks that did not pass."""
        return tuple(c.name for c in self.checks if not c.passed)

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    def describe(self) -> str:
        """One line for a person: "N checks passed" or the reason (and which checks failed)."""
        if self.works:
            return f"all {self.passed_count} sandbox checks passed"
        if self.reason_code == "check_failed" and self.failed:
            return f"{self.reason}: {', '.join(self.failed)}"
        return self.reason


Runner = Callable[..., RunResult]


def _failure(code: str, *, bwrap_path: str | None = None, checks: Sequence[CheckOutcome] = (),
             started: float | None = None, detail: str | None = None) -> ProbeResult:
    code = code if code in REASONS else "probe_error"
    reason = REASONS[code]
    if detail:
        reason = f"{reason}: {detail}"
    ms = 0 if started is None else max(0, int((time.monotonic() - started) * 1000))
    return ProbeResult(False, code, reason, tuple(checks), bwrap_path, ms)


# --------------------------------------------------------------------------
# Running a process
# --------------------------------------------------------------------------
def default_runner(argv: Sequence[str], *, env: Mapping[str, str], timeout_s: float) -> RunResult:
    """Run ``argv`` in its own session with captured, bounded output; on timeout the whole group is killed."""
    try:
        proc = subprocess.Popen(
            list(argv), env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True, close_fds=True,
        )
    except (OSError, ValueError):
        return RunResult(None, "", "", False)
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:  # noqa: BLE001
            out, err = b"", b""
    except BaseException:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        raise
    return RunResult(
        proc.returncode,
        (out or b"")[:_MAX_CAPTURE_BYTES].decode("utf-8", "replace"),
        (err or b"")[:_MAX_CAPTURE_BYTES].decode("utf-8", "replace"),
        timed_out,
    )


def classify_start_failure(result: RunResult) -> str:
    """Map bubblewrap's error text to one of a few fixed codes.  The text itself is never kept."""
    if result.timed_out:
        return "timeout"
    text = (result.stderr or "").lower()
    if "can't mount proc" in text or "mounting proc" in text or "cannot mount proc" in text:
        return "proc_unavailable"
    if "loopback" in text or "rtnetlink" in text or "rtm_newaddr" in text or "network namespace" in text:
        return "netns_unavailable"
    if (
        "no permissions to create new namespace" in text or "creating new namespace failed" in text
        or "uid map" in text or "gid map" in text or "unprivileged" in text
        or ("namespace" in text and ("not permitted" in text or "permission denied" in text))
    ):
        return "userns_unavailable"
    return "bwrap_failed"


# --------------------------------------------------------------------------
# The throw-away world
# --------------------------------------------------------------------------
def _fixture_base() -> str:
    """A directory for the temporary world that is NOT under the sandbox's private /tmp, so that ``cd ..`` from the
    project lands somewhere the sandbox makes read-only, as it does for a real project under /home."""
    for candidate in ("/var/tmp", tempfile.gettempdir()):
        if os.path.isabs(candidate) and os.path.isdir(candidate) and os.access(candidate, os.W_OK | os.X_OK):
            return candidate
    return tempfile.gettempdir()


def _token(prefix: str = "m2c-probe") -> str:
    return f"{prefix}-{secrets.token_hex(8)}"


class _World:
    """The fake home and project, the canaries and the unique names the checks use.  Everything lives in one temporary
    directory that ``close`` removes."""

    def __init__(self) -> None:
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="m2c-probe-", dir=_fixture_base()))
        self.home = os.path.join(self.root, "home")
        self.project = os.path.join(self.home, "proj")
        self.ssh_canary, self.aws_canary, self.rc_canary = _token(), _token(), _token()
        self.git_canary, self.m2c_canary = _token(), _token()
        self.bashrc_canary, self.profile_canary = _token(), _token()
        self.basic_token = _token()
        self.marker = _token("m2c-probe-marker")
        self.outer_value = _token()
        self.unique = secrets.token_hex(6)
        self.host_tmp_file = ""
        for sub in (".ssh", ".aws", "proj/.git", "proj/.m2c"):
            os.makedirs(os.path.join(self.home, sub))
        self._write(os.path.join(self.home, ".ssh", "id_probe"), self.ssh_canary)
        self._write(os.path.join(self.home, ".aws", "credentials"), self.aws_canary)
        self._write(os.path.join(self.home, ".bashrc"), self.rc_canary)
        self._write(os.path.join(self.project, ".git", "config"), self.git_canary)
        self._write(os.path.join(self.project, ".m2c", "state"), self.m2c_canary)
        self._write(os.path.join(self.project, ".bashrc"), self.bashrc_canary)
        self._write(os.path.join(self.project, ".profile"), self.profile_canary)
        # paths outside the project that must stay untouched, with their real host locations
        self.outside_targets = [
            os.path.join(self.home, f"escape_home_{self.unique}"),
            f"/usr/escape_usr_{self.unique}",
            f"/etc/escape_etc_{self.unique}",
            f"/escape_root_{self.unique}",
            os.path.join(self.home, f"escape_up_{self.unique}"),
        ]
        self.cd_up_targets = [os.path.join(self.home, f"up_{self.unique}"), f"/root_{self.unique}"]
        self.escape_targets = self.outside_targets + self.cd_up_targets

    @staticmethod
    def _write(path: str, text: str) -> None:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def read(self, *parts: str) -> str | None:
        try:
            with open(os.path.join(self.project, *parts), encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    def exists(self, path: str) -> bool:
        return os.path.lexists(path)

    def close(self) -> None:
        for path in self.escape_targets + ([self.host_tmp_file] if self.host_tmp_file else []):
            try:
                if os.path.lexists(path):
                    os.unlink(path)
            except OSError:
                pass
        for leak in (f"/tmp/m2c-probe-inside-{self.unique}",):
            try:
                if os.path.lexists(leak):
                    os.unlink(leak)
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)


def _spawn_host_marker(token: str) -> Callable[[], None] | None:
    """Start a harmless host process whose command line contains ``token`` and return the function that stops it;
    None when it could not be started."""
    try:
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", token],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except (OSError, ValueError):
        return None

    def stop() -> None:
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass

    return stop


# --------------------------------------------------------------------------
# The checks.  Each is (script, verifier).  Scripts are POSIX sh, start with a no-op that names the check (tests read
# it), and end with the sentinel.  A verifier gets the result and the world and returns True when the sandbox held.
# --------------------------------------------------------------------------
def _q(value: str) -> str:
    return shlex.quote(value)


def _script(name: str, *lines: str) -> str:
    return "\n".join([f": probe_check={name}", *lines, f"echo {SENTINEL}", ""])


def _ran(result: RunResult) -> bool:
    return result.returncode == 0 and not result.timed_out and SENTINEL in result.stdout.split()


def _section(text: str, begin: str, end: str) -> list[str]:
    lines = text.splitlines()
    try:
        a, b = lines.index(begin), lines.index(end)
    except ValueError:
        return []
    return [line.strip() for line in lines[a + 1:b] if line.strip()]


def _no_wrote_markers(result: RunResult) -> bool:
    return not any(word.startswith(("WROTE", "APPENDED", "MOVED", "REMOVED", "MKDIR", "CONNECTED", "VISIBLE"))
                   for word in result.stdout.split())


def _checks(world: _World) -> dict[str, tuple[str, Callable[[RunResult], bool]]]:
    w = world
    u = w.unique
    checks: dict[str, tuple[str, Callable[[RunResult], bool]]] = {}

    # -- basic ---------------------------------------------------------
    def basic(r: RunResult) -> bool:
        return _ran(r) and w.basic_token in r.stdout and w.read("probe_out.txt") == w.basic_token + "\n"

    checks["basic"] = (_script(
        "basic", f"printf '%s\\n' {_q(w.basic_token)} > probe_out.txt && cat probe_out.txt",
    ), basic)

    # -- write outside the project --------------------------------------
    def outside(r: RunResult) -> bool:
        return _ran(r) and _no_wrote_markers(r) and not any(w.exists(p) for p in w.outside_targets)

    checks["write_outside_project"] = (_script(
        "write_outside_project",
        f"touch {_q(os.path.join(w.home, f'escape_home_{u}'))} 2>/dev/null && echo WROTE_HOME",
        f"touch /usr/escape_usr_{u} 2>/dev/null && echo WROTE_USR",
        f"touch /etc/escape_etc_{u} 2>/dev/null && echo WROTE_ETC",
        f"touch /escape_root_{u} 2>/dev/null && echo WROTE_ROOT",
        f"touch ../escape_up_{u} 2>/dev/null && echo WROTE_UP",
    ), outside)

    # -- .git and .m2c: add, append, move away, remove -------------------
    def protected_dir(name: str, canary: str, member: str) -> Callable[[RunResult], bool]:
        def verify(r: RunResult) -> bool:
            if not (_ran(r) and _no_wrote_markers(r)):
                return False
            if w.read(name, member) != canary or not os.path.isdir(os.path.join(w.project, name)):
                return False
            return not w.exists(os.path.join(w.project, name, f"probe_new_{u}")) and not w.exists(
                os.path.join(w.project, f"{name}_moved")) and not w.exists(os.path.join(w.project, name, f"sub_{u}"))
        return verify

    def protected_dir_script(name: str, member: str) -> str:
        return _script(
            f"write_{name.lstrip('.')}",
            f"echo x > {name}/probe_new_{u} 2>/dev/null && echo WROTE_NEW",
            f"echo x >> {name}/{member} 2>/dev/null && echo APPENDED",
            f"mv {name} {name}_moved 2>/dev/null && echo MOVED",
            f"rm -rf {name} 2>/dev/null; [ -e {name}/{member} ] || echo REMOVED",
            f"mkdir {name}/sub_{u} 2>/dev/null && echo MKDIR",
        )

    checks["write_git"] = (protected_dir_script(".git", "config"), protected_dir(".git", w.git_canary, "config"))
    checks["write_m2c"] = (protected_dir_script(".m2c", "state"), protected_dir(".m2c", w.m2c_canary, "state"))

    # -- the home secrets are absent -------------------------------------
    def secrets_hidden(r: RunResult) -> bool:
        if not _ran(r):
            return False
        if any(c in r.stdout or c in r.stderr for c in (w.ssh_canary, w.aws_canary, w.rc_canary)):
            return False
        return set(_section(r.stdout, "LISTING_BEGIN", "LISTING_END")) <= {"proj"}

    checks["home_secrets_hidden"] = (_script(
        "home_secrets_hidden",
        f"cat {_q(os.path.join(w.home, '.ssh', 'id_probe'))} 2>&1",
        f"cat {_q(os.path.join(w.home, '.aws', 'credentials'))} 2>&1",
        f"cat {_q(os.path.join(w.home, '.bashrc'))} 2>&1",
        "echo LISTING_BEGIN",
        f"ls -A {_q(w.home)} 2>&1",
        "echo LISTING_END",
    ), secrets_hidden)

    # -- a protected shell start-up file ---------------------------------
    def rc_protected(r: RunResult) -> bool:
        return (_ran(r) and _no_wrote_markers(r) and w.read(".bashrc") == w.bashrc_canary
                and w.read(".profile") == w.profile_canary and not w.exists(os.path.join(w.project, f".profile_moved")))

    checks["write_rc_file"] = (_script(
        "write_rc_file",
        "echo x >> .bashrc 2>/dev/null && echo APPENDED",
        "echo x >> .profile 2>/dev/null && echo APPENDED_PROFILE",
        "rm -f .bashrc 2>/dev/null; [ -e .bashrc ] || echo REMOVED",
        "mv .profile .profile_moved 2>/dev/null && echo MOVED",
    ), rc_protected)

    # -- no network --------------------------------------------------------
    def no_network(r: RunResult) -> bool:
        if not _ran(r) or not _no_wrote_markers(r):
            return False
        interfaces = _section(r.stdout, "IFACES_BEGIN", "IFACES_END")
        return bool(interfaces) and set(interfaces) <= {"lo"}

    checks["network_unreachable"] = (_script(
        "network_unreachable",
        "echo IFACES_BEGIN",
        "sed -n '3,$p' /proc/net/dev 2>/dev/null | sed 's/:.*//; s/^ *//'",
        "echo IFACES_END",
        "if command -v bash >/dev/null 2>&1; then bash -c 'exec 3<>/dev/tcp/192.0.2.1/53' 2>/dev/null && echo CONNECTED; fi",
    ), no_network)

    # -- cd .. then write -----------------------------------------------------
    def cd_up(r: RunResult) -> bool:
        return _ran(r) and _no_wrote_markers(r) and not any(w.exists(p) for p in w.cd_up_targets)

    checks["cd_up_write"] = (_script(
        "cd_up_write",
        f"cd .. && touch up_{u} 2>/dev/null && echo WROTE_UP",
        f"cd / && touch root_{u} 2>/dev/null && echo WROTE_ROOT",
    ), cd_up)

    # -- host processes are not visible ---------------------------------------
    head, tail = w.marker[:9], w.marker[9:]  # the literal in the script is split, so the shell does not match itself
    checks["pid_isolation"] = (_script(
        "pid_isolation",
        "for d in /proc/[0-9]*; do",
        "  c=$(tr '\\0' ' ' < \"$d/cmdline\" 2>/dev/null)",
        f"  case \"$c\" in *{head}\"{tail[:1]}\"{tail[1:]}*) echo VISIBLE;; esac",
        "done",
    ), lambda r: _ran(r) and _no_wrote_markers(r))

    # -- /tmp is private ----------------------------------------------------------
    def tmp_private(r: RunResult) -> bool:
        if not _ran(r) or "HOST_TMP_VISIBLE" in r.stdout or "TMP_NOT_WRITABLE" in r.stdout:
            return False
        return not w.exists(f"/tmp/m2c-probe-inside-{u}")

    checks["private_tmp"] = (_script(
        "private_tmp",
        f"[ -e /tmp/m2c-probe-hosttmp-{u} ] && echo HOST_TMP_VISIBLE",
        f"echo inside > /tmp/m2c-probe-inside-{u} 2>/dev/null || echo TMP_NOT_WRITABLE",
    ), tmp_private)

    # -- the environment is rebuilt, not inherited --------------------------------------
    def env_cleared(r: RunResult) -> bool:
        if not _ran(r) or w.outer_value in r.stdout or "PROBE_OUTER_VALUE" in r.stdout:
            return False
        return f"HOME={w.home}" in r.stdout.splitlines() and f"PWD={w.project}" in r.stdout.splitlines()

    checks["env_cleared"] = (_script("env_cleared", "env"), env_cleared)

    assert tuple(checks) == CHECK_NAMES, "every check in CHECK_NAMES must be defined, in order"
    return checks


# --------------------------------------------------------------------------
# The probe
# --------------------------------------------------------------------------
def run_probe(
    *,
    runner: Runner | None = None,
    bwrap_path: str | None = None,
    layout: SystemLayout | None = None,
    toolchain_dirs: Sequence[str] = DEFAULT_HOME_TOOLCHAIN_DIRS,
    extra_ro: Sequence[str] = (),
    denied: Sequence[str] = (),
    timeout_s: float = PROBE_TIMEOUT_S,
    marker_factory: Callable[[str], Callable[[], None] | None] | None = None,
) -> ProbeResult:
    """Really test the sandbox, fresh, and say whether it works.  Never raises: any trouble is "does not work".

    ``runner``          how a process is started (tests pass a fake); the default runs a real one.
    ``bwrap_path``      the binary to test; None looks for it.
    ``layout``/``toolchain_dirs``/``extra_ro``/``denied``   the profile settings the shell tool will use.
    ``marker_factory``  starts the host process the PID check must not see (tests pass a no-op).
    """
    started = time.monotonic()
    result = _run_probe(runner, bwrap_path, layout, toolchain_dirs, extra_ro, denied, timeout_s, marker_factory, started)
    log_event(logger, logging.INFO if result.works else logging.WARNING, "sandbox.probe",
              status="ok" if result.works else "failed", code=result.reason_code, count=len(result.checks),
              duration_ms=result.duration_ms)
    return result


def _run_probe(runner, bwrap_path, layout, toolchain_dirs, extra_ro, denied, timeout_s, marker_factory,
               started) -> ProbeResult:
    world: _World | None = None
    stop_marker: Callable[[], None] | None = None
    bwrap: str | None = None
    try:
        bwrap = bwrap_path if bwrap_path is not None else find_bwrap()
        if not bwrap:
            return _failure("bwrap_missing", started=started)
        try:
            resolved = layout if layout is not None else detect_system_layout("/")
        except ProfileError as exc:
            return _failure(exc.code, bwrap_path=bwrap, started=started)
        if find_shell(resolved.root) is None:
            return _failure("no_shell", bwrap_path=bwrap, started=started)
        run = runner if runner is not None else default_runner
        world = _World()
        checks = _checks(world)
        marker = marker_factory if marker_factory is not None else _spawn_host_marker
        stop_marker = marker(world.marker)
        host_tmp = f"/tmp/m2c-probe-hosttmp-{world.unique}"
        try:
            with open(host_tmp, "x", encoding="utf-8") as handle:
                handle.write("host")
            world.host_tmp_file = host_tmp
        except OSError:
            world.host_tmp_file = ""

        probe_env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "TERM": "dumb"}
        try:
            profile = build_profile(
                project=world.project, home=world.home, env=probe_env, layout=resolved,
                toolchain_dirs=toolchain_dirs, extra_ro=extra_ro, denied=denied,
            )
        except ProfileError as exc:
            return _failure(exc.code, bwrap_path=bwrap, started=started)
        shell = find_shell(resolved.root) or "/bin/sh"
        outer = outer_environment()
        outer["PROBE_OUTER_VALUE"] = world.outer_value

        outcomes: list[CheckOutcome] = []
        for name in CHECK_NAMES:
            script, verify = checks[name]
            if name == "pid_isolation" and stop_marker is None:
                outcomes.append(CheckOutcome(name, False, "setup_failed"))
                continue
            result = run([bwrap, *profile, "--", shell, "-c", script], env=outer, timeout_s=timeout_s)
            if name == "basic" and not _ran(result):
                return _failure(classify_start_failure(result), bwrap_path=bwrap, checks=[
                    CheckOutcome(name, False, "timeout" if result.timed_out else "setup_failed")], started=started)
            if result.timed_out:
                outcomes.append(CheckOutcome(name, False, "timeout"))
            elif not _ran(result):
                outcomes.append(CheckOutcome(name, False, "setup_failed"))
            elif verify(result):
                outcomes.append(CheckOutcome(name, True, "ok"))
            else:
                outcomes.append(CheckOutcome(name, False, "violated"))
        ms = max(0, int((time.monotonic() - started) * 1000))
        if all(o.passed for o in outcomes):
            return ProbeResult(True, "ok", "the sandbox works", tuple(outcomes), bwrap, ms)
        failed = ", ".join(o.name for o in outcomes if not o.passed)
        return ProbeResult(False, "check_failed", f"{REASONS['check_failed']}: {failed}", tuple(outcomes), bwrap, ms)
    except Exception:  # noqa: BLE001 -- a security check that cannot be completed is a failed one
        return _failure("probe_error", bwrap_path=bwrap, started=started)
    finally:
        if stop_marker is not None:
            try:
                stop_marker()
            except Exception:  # noqa: BLE001
                pass
        if world is not None:
            world.close()


# --------------------------------------------------------------------------
# The session cache
# --------------------------------------------------------------------------
_CACHE: dict[tuple, ProbeResult] = {}
_CACHE_LOCK = threading.Lock()


def _key(toolchain_dirs: Sequence[str], extra_ro: Sequence[str], denied: Sequence[str], runner: Runner | None) -> tuple:
    return (tuple(toolchain_dirs), tuple(extra_ro), tuple(denied), id(runner) if runner is not None else None)


def session_probe(
    *,
    runner: Runner | None = None,
    toolchain_dirs: Sequence[str] = DEFAULT_HOME_TOOLCHAIN_DIRS,
    extra_ro: Sequence[str] = (),
    denied: Sequence[str] = (),
    **kwargs,
) -> ProbeResult:
    """The probe result for this process, run once per distinct settings and then remembered.  The lock is held for the
    whole probe, so two threads starting a command at the same time run it once."""
    key = _key(toolchain_dirs, extra_ro, denied, runner)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is None:
            cached = run_probe(runner=runner, toolchain_dirs=toolchain_dirs, extra_ro=extra_ro, denied=denied, **kwargs)
            _CACHE[key] = cached
        return cached


def reset_session_cache() -> None:
    """Forget every remembered result (tests; a doctor that wants a fresh look uses ``run_probe`` directly)."""
    with _CACHE_LOCK:
        _CACHE.clear()
