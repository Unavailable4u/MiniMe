"""
m2c/tests/sandbox/test_sandbox_shell_regression.py -- Patch C11a: the shell tool behaves the same with the sandbox on and off.

C11a changes WHERE a command runs, not how a call is supervised: the timeout, the process-group kill, cancel, streaming and
the output cap are C07's, and the claim of this patch is that they are untouched.  This file is the evidence.  Every test here
runs the SAME assertions twice:

``unsandboxed``  ``ShellConfig(sandbox="off")``: C07's own path.
``sandboxed``    ``ShellConfig(sandbox="required")`` against the real bubblewrap (the ``real_sandbox`` marker; skipped LOUDLY
                 -- "SKIPPED: <reason>" on stderr -- where bubblewrap or user namespaces are not available, so a machine
                 without them never reports this as a pass).

Why these tests exist besides tests/tools/test_tools_shell.py.  That file's process tests write ``$!`` to a file and then look
at that PID on the HOST.  Inside the sandbox's PID namespace the number means something else, so under
``M2C_TEST_SANDBOX=real`` those six tests are skipped there (their ``skipif`` says so) and are replaced by the tests below,
which find the processes by a UNIQUE ARGUMENT (``sleep 3900.123456``) on the host's own ``/proc`` and ask the command itself
for positive evidence that its processes were running (``RUNNING:<n>``) before they were killed, so "nothing is left" can
never be true just because nothing ever started.  The three warn-once tests are about the unsandboxed warning, which a
sandboxed run correctly does not show; they are skipped under the same variable (the sandbox's side of the warning is pinned in
tests/tools/test_tools_shell_sandbox.py).

Nothing here may be dangerous when the sandbox is broken: no command signals anything but processes it started itself, and
none writes outside the temporary project.

Run (Ubuntu / WSL2 Ubuntu):   sudo apt install bubblewrap && cd m2c && pytest tests/sandbox/test_sandbox_shell_regression.py -v -rs
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

from m2c import sandbox
from m2c.tools import ToolContext
from m2c.tools import shell
from m2c.tools.shell import AUTH_USER_APPROVED, ShellConfig, configure_shell, run_command

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the sandbox is Linux only")

_PROBE: dict = {}


def _loud_skip(reason: str) -> None:
    sys.__stderr__.write(f"\nSKIPPED: sandboxed shell regression tests: {reason}\n")
    sys.__stderr__.flush()
    pytest.skip(reason)


@pytest.fixture(
    params=[
        pytest.param("off", id="unsandboxed"),
        pytest.param("required", id="sandboxed", marks=pytest.mark.real_sandbox),
    ]
)
def mode(request) -> str:
    if request.param == "required":
        if "result" not in _PROBE:
            _PROBE["result"] = sandbox.fresh_probe()
        if not _PROBE["result"].works:
            _loud_skip(_PROBE["result"].describe())
    return request.param


@pytest.fixture
def need_real_sandbox():
    if "result" not in _PROBE:
        _PROBE["result"] = sandbox.fresh_probe()
    if not _PROBE["result"].works:
        _loud_skip(_PROBE["result"].describe())


class Rig:
    """A project under the suite's temporary home and a shell context for one mode."""

    def __init__(self, mode: str, **cfg):
        self.home = Path(os.environ["HOME"]).resolve()
        self.project = self.home / "proj"
        self.project.mkdir(parents=True, exist_ok=True)
        self.context = ToolContext(self.project)
        configure_shell(self.context, ShellConfig(sandbox=mode, kill_grace_s=0.3, **cfg))

    def run(self, command: str, timeout_s: int = 20, **kw):
        return run_command(self.context, command, timeout_s, authorization=AUTH_USER_APPROVED, **kw)


@pytest.fixture
def rig(mode):
    return Rig(mode)


# --------------------------------------------------------------------------
# helpers: find processes on the HOST by a unique argument
# --------------------------------------------------------------------------
def sleeper(prefix: int) -> str:
    """A unique, valid ``sleep`` argument such as ``3900.123456``; it identifies the process on the host."""
    return f"{prefix}.{secrets.randbelow(10**6):06d}"


def running(marker: str, tag: str) -> str:
    """Shell text that prints ``RUNNING_<tag>:<n>``, n being the processes whose command line contains ``marker``.  The
    pattern's first character is bracketed so that this very script's own command line does not match it."""
    return f"echo RUNNING_{tag}:$(grep -la '[{marker[0]}]{marker[1:]}' /proc/[0-9]*/cmdline 2>/dev/null | wc -l)"


def count_running(output: str, tag: str) -> int:
    found = re.findall(rf"RUNNING_{tag}:(\d+)", output)
    assert found, f"the command never reported RUNNING_{tag}: {output!r}"
    return int(found[-1])


def host_pids_with(needle: str) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if needle.encode() in (entry / "cmdline").read_bytes():
                out.append(int(entry.name))
        except OSError:
            continue
    return out


def _alive(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] not in ("Z", "X")


def gone(needle: str, seconds: float = 6.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not [p for p in host_pids_with(needle) if _alive(p)]:
            return True
        time.sleep(0.05)
    return not [p for p in host_pids_with(needle) if _alive(p)]


@pytest.fixture(autouse=True)
def _reap_strays():
    """Whatever a failing test leaves behind (only processes this file started, found by their unique argument)."""
    markers: list[str] = []
    yield markers
    for needle in markers:
        for pid in host_pids_with(needle):
            try:
                os.kill(pid, 9)
            except OSError:
                pass


# ==========================================================================
# timeout and kill
# ==========================================================================
def test_a_timeout_ends_the_call_and_kills_every_process_it_started(rig, _reap_strays):
    child, grandchild = sleeper(3900), sleeper(3901)
    _reap_strays += [child, grandchild]
    script = f"sh -c 'sleep {grandchild} & wait' & sleep {child} & sleep 0.4; {running(child, 'c')}; {running(grandchild, 'g')}; wait"
    started = time.monotonic()
    r = rig.run(script, 2)
    assert r.ok is False and r.code == "timeout", r.output
    assert time.monotonic() - started < 12
    assert count_running(r.output, "c") >= 1 and count_running(r.output, "g") >= 1, "they were running before the timeout"
    assert gone(child), "the child survived the timeout"
    assert gone(grandchild), "the grandchild survived the timeout"


def test_a_command_that_ignores_sigterm_is_killed_after_the_grace_period(rig, _reap_strays):
    marker = sleeper(3902)
    _reap_strays.append(marker)
    started = time.monotonic()
    r = rig.run(f"trap '' TERM; sleep {marker} & sleep 0.4; {running(marker, 's')}; wait", 2)
    assert r.code == "timeout" and time.monotonic() - started < 12
    assert count_running(r.output, "s") >= 1
    assert gone(marker), "SIGKILL must follow SIGTERM"


def test_a_command_that_closed_its_own_output_is_still_timed_out(rig, _reap_strays):
    marker = sleeper(3903)
    _reap_strays.append(marker)
    started = time.monotonic()
    r = rig.run(f"exec >&- 2>&-; sleep {marker}", 1)
    assert r.code == "timeout" and time.monotonic() - started < 12
    assert gone(marker)


def test_background_jobs_are_stopped_when_the_command_ends_and_never_block_the_result(rig, _reap_strays):
    marker = sleeper(3904)
    _reap_strays.append(marker)
    started = time.monotonic()
    r = rig.run(f"sleep {marker} & sleep 0.4; {running(marker, 'b')}; echo done", 30)
    assert r.ok and "done" in r.output, r.output
    assert time.monotonic() - started < 10, "a background job holding the pipes must not block the result"
    assert count_running(r.output, "b") >= 1
    assert gone(marker)


def test_a_background_job_that_redirected_its_output_is_stopped_too(rig, _reap_strays):
    marker = sleeper(3905)
    _reap_strays.append(marker)
    r = rig.run(f"sleep {marker} >/dev/null 2>&1 & sleep 0.4; {running(marker, 'r')}; echo done", 30)
    assert r.ok and count_running(r.output, "r") >= 1
    assert gone(marker)


# ==========================================================================
# cancel
# ==========================================================================
def test_cancel_stops_the_command_promptly_and_kills_what_it_started(rig, _reap_strays):
    marker = sleeper(3906)
    _reap_strays.append(marker)
    cancel = threading.Event()
    threading.Timer(1.2, cancel.set).start()
    started = time.monotonic()
    r = rig.run(f"sleep {marker} & sleep 0.4; echo started; {running(marker, 'k')}; wait", 60, cancel=cancel)
    assert r.ok is False and r.code == "cancelled" and "started" in r.output and "cancelled" in r.output
    assert time.monotonic() - started < 8
    assert count_running(r.output, "k") >= 1
    assert gone(marker)


def test_a_cancel_that_is_already_set_runs_nothing(rig):
    cancel = threading.Event()
    cancel.set()
    r = rig.run("touch ran.flag", 20, cancel=cancel)
    assert r.ok is False and r.code == "cancelled"
    assert not (rig.project / "ran.flag").exists()


# ==========================================================================
# streaming
# ==========================================================================
def test_chunks_arrive_in_the_order_they_were_written_and_in_time(mode):
    rig = Rig(mode)
    seen: list[tuple[str, str, float]] = []
    t0 = time.monotonic()
    r = rig.run("echo o1; sleep 1.0; echo e1 >&2; sleep 0.4; echo o2", 20, on_chunk=lambda s, t: seen.append((s, t, time.monotonic() - t0)))
    assert r.ok, r.output
    assert [(s, t) for s, t, _ in seen] == [("stdout", "o1\n"), ("stderr", "e1\n"), ("stdout", "o2\n")]
    assert seen[0][2] < 0.9, "the first line is delivered during the pause, not at the end"
    assert r.output == "[stdout]\no1\n[stderr]\ne1\n[stdout]\no2\n[exit status: 0]"


def test_small_chunks_are_coalesced_and_nothing_is_lost(mode):
    rig = Rig(mode)
    chunks: list[tuple[str, str]] = []
    r = rig.run("for i in $(seq 1 20000); do echo line$i; done", 60, on_chunk=lambda s, t: chunks.append((s, t)))
    assert r.ok
    expected = "".join(f"line{i}\n" for i in range(1, 20001))
    assert "".join(t for s, t in chunks if s == "stdout") == expected
    assert len(chunks) <= 120, f"{len(chunks)} callbacks for {len(expected)} bytes: not coalesced"


# ==========================================================================
# the output cap
# ==========================================================================
def test_big_output_keeps_head_and_tail_with_a_marker(mode):
    rig = Rig(mode)
    r = rig.run("seq 1 100000")
    assert r.ok and r.truncated is True
    assert len(r.output) <= shell.DEFAULT_MAX_OUTPUT_BYTES + 300
    assert r.output.startswith("[stdout]\n1\n2\n3\n")
    assert "100000\n[exit status: 0]" in r.output.replace("\r", "")
    m = re.search(r"\[\.\.\. (\d+) bytes omitted \.\.\.\]", r.output)
    assert m and int(m.group(1)) > 400_000


def test_a_command_that_never_stops_writing_is_cut_off_by_the_timeout(mode):
    rig = Rig(mode)
    started = time.monotonic()
    r = rig.run("yes", 1)
    assert r.code == "timeout" and r.truncated
    assert time.monotonic() - started < 12


# ==========================================================================
# the sandbox changes where a command runs, not what a call returns
# ==========================================================================
PARITY_COMMANDS = [
    "echo hello",
    "echo out; echo err >&2; exit 3",
    "printf 'no newline'",
    "printf '\\033[31mred\\033[0m plain\\n'",
    "printf 'é中\\n'",
    "false",
    "exit 0",
    "echo $((6*7))",
    "cat /nonexistent-m2c-file",
    "cat; echo after-stdin",  # standard input is closed, so cat ends at once
    "echo a; sleep 0.3; echo b >&2; sleep 0.3; echo c",  # pauses, as in C07's tests: without them two streams written at once may merge
    "kill -9 $$",  # ended by SIGKILL: the same annotated status in both modes
    "kill -TERM $$",
    "head -c 50000 /dev/zero | tr '\\0' x",  # over the cap below: the same truncation
]


def outcome_key(r) -> tuple:
    return (r.ok, r.exit_code, r.code, r.truncated, r.trust, r.output)


@pytest.mark.real_sandbox
def test_the_same_commands_give_the_same_outcomes_with_and_without_the_sandbox(need_real_sandbox):
    plain, boxed = Rig("off", max_output_bytes=1000), Rig("required", max_output_bytes=1000)
    for command in PARITY_COMMANDS:
        a, b = plain.run(command), boxed.run(command)
        assert outcome_key(a) == outcome_key(b), f"outcomes differ for {command!r}:\n  off:      {a}\n  required: {b}"


def test_a_signal_is_annotated_the_same_way_in_both_modes(mode):
    rig = Rig(mode)
    r = rig.run("kill -9 $$")
    assert r.ok is False and r.exit_code == 137 and r.output.endswith("[exit status: 137 (terminated by signal 9)]"), r.output


def test_the_working_directory_is_the_project_and_files_reach_the_disk(mode):
    rig = Rig(mode)
    r = rig.run("pwd; echo content > made.txt; mkdir -p sub/dir; echo deep > sub/dir/f")
    assert r.ok and str(rig.project) in r.output
    assert (rig.project / "made.txt").read_text() == "content\n" and (rig.project / "sub/dir/f").read_text() == "deep\n"


def test_environment_secrets_never_reach_the_command_in_either_mode(mode, monkeypatch):
    value = "canary" + secrets.token_hex(12)
    monkeypatch.setenv("AWS_FAKE_SETTING", value)
    monkeypatch.setenv("INNOCENT_SETTING", "visible")
    rig = Rig(mode)
    r = rig.run("env; echo ---; cat /proc/1/environ | tr '\\0' '\\n'; true")
    assert value not in r.output and "INNOCENT_SETTING=visible" in r.output


def test_the_temporary_project_is_removed_with_the_test(mode):
    """Hygiene: nothing a test does here may outlive the suite's temporary home."""
    rig = Rig(mode)
    rig.run("echo x > leftover")
    assert (rig.project / "leftover").exists()
    shutil.rmtree(rig.project)
    assert not rig.project.exists()
