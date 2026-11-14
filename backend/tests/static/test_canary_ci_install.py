"""Patch M2C-Q06b: the `canary` job installs the backend and the m2c client in two steps, and checks the result.

The problem this guards against. backend/requirements.txt pins ``rich==14.3.4`` (guardrails-ai 0.10.2 needs rich below 15,
e2b needs 14 or newer) while m2c/pyproject.toml pins ``rich==15.0.0`` and ``prompt_toolkit==3.0.53`` for the terminal UI.
``pip install -r requirements.txt -e ../m2c`` therefore ends in ``ResolutionImpossible``, and the `canary` job failed at
its first install step. The canary test never imports the terminal UI (only ``m2c/tui/app.py`` imports those two
libraries, and the harness imports ``m2c.tui.session``, which does not), and ``websockets`` is pinned to the same
version in both packages, so the job installs the backend first and then ``pip install --no-deps -e ../m2c``.

What this file checks, with no network and no pip run:

1. ci.yml parses, and the canary job installs in two steps: the backend requirements in one command that does not
   mention m2c, and ``--no-deps -e ../m2c`` (no ``[dev]`` extra) in a later one. No job anywhere puts the backend
   requirements and m2c on one pip command line.
2. A ``pip check`` step comes after both installs and before the first pytest step. ``--no-deps`` leaves m2c's own
   ``rich==15.0.0`` and ``prompt_toolkit`` requirements unmet on purpose, so a plain ``pip check`` would always fail:
   the step tolerates exactly those two lines and fails on anything else (a drifted ``websockets``, a new m2c runtime
   dependency, a broken backend pin). The step's real shell text is run here against a fake ``python`` that prints
   pip's real messages.
3. backend/tests/canary/README.md and README.md say ``pip install --no-deps -e ../m2c`` and say why.

The tests that need Linux and bash (the step is run with ``bash -e``, as GitHub's runner does) skip elsewhere.
"""
import os
import pathlib
import re
import shutil
import stat
import subprocess

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[3]
CI = REPO / ".github" / "workflows" / "ci.yml"
CANARY_README = REPO / "backend" / "tests" / "canary" / "README.md"
ROOT_README = REPO / "README.md"

# What `pip check` prints (pip 24 to 26) for the two things --no-deps leaves unmet, and for a real problem.
EXPECTED_LINES = (
    "m2c 0.1.0.dev0 requires prompt-toolkit, which is not installed.\n"
    "m2c 0.1.0.dev0 has requirement rich==15.0.0, but you have rich 14.3.4.\n"
)
WEBSOCKETS_DRIFT = (
    "m2c 0.1.0.dev0 requires websockets==16.0, but you have websockets 15.0.1 which is incompatible.\n"
    "m2c 0.1.0.dev0 has requirement websockets==16.0, but you have websockets 15.0.1.\n"
)
BACKEND_BREAK = "guardrails-ai 0.10.2 has requirement rich<15.0.0,>=13.6.0, but you have rich 15.0.0.\n"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def canary_steps(workflow):
    return workflow["jobs"]["canary"]["steps"]


def _run_text(step):
    return step.get("run", "") or ""


def _index(steps, predicate, what):
    for i, step in enumerate(steps):
        if predicate(_run_text(step)):
            return i
    raise AssertionError(f"the canary job has no step that {what}")


def _commands(run_text):
    """The shell lines of a ``run:`` block, comments and blank lines dropped, continuation lines joined."""
    joined = run_text.replace("\\\n", " ")
    return [ln.strip() for ln in joined.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def test_the_workflow_parses_and_has_the_canary_job(workflow):
    assert "canary" in workflow["jobs"]


def test_canary_installs_the_backend_and_m2c_in_two_steps(canary_steps):
    backend = _index(
        canary_steps,
        lambda r: any(c.startswith("pip install") and "-r requirements.txt" in c and "m2c" not in c for c in _commands(r)),
        "installs the backend requirements without mentioning m2c",
    )
    client = _index(
        canary_steps,
        lambda r: any(c.startswith("pip install") and "--no-deps" in c and "-e ../m2c" in c for c in _commands(r)),
        "runs `pip install --no-deps -e ../m2c`",
    )
    assert backend < client, "the backend must be installed before m2c"
    for command in _commands(_run_text(canary_steps[client])):
        if "../m2c" in command:
            assert "[dev]" not in command, "m2c[dev] pins a different pytest than the backend"


def test_no_pip_command_in_any_job_combines_the_backend_requirements_with_m2c(workflow):
    for name, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            for command in _commands(_run_text(step)):
                if "pip install" in command and "m2c" in command:
                    assert "requirements" not in command, f"job {name}: backend requirements and m2c in one pip command: {command}"


def test_pip_check_runs_after_both_installs_and_before_the_first_test(canary_steps):
    client = _index(canary_steps, lambda r: "--no-deps -e ../m2c" in r, "installs m2c")
    check = _index(canary_steps, lambda r: "pip check" in r, "runs pip check")
    first_test = _index(canary_steps, lambda r: "pytest" in r, "runs pytest")
    assert client < check < first_test


# --- the pip check step, run for real against a fake `python` ---------------------------------------------------

needs_bash = pytest.mark.skipif(shutil.which("bash") is None or os.name != "posix", reason="needs bash on a POSIX system")


def _run_pip_check_step(canary_steps, tmp_path, pip_output, pip_rc):
    step = canary_steps[_index(canary_steps, lambda r: "pip check" in r, "runs pip check")]
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake_python = fake_bin / "python"
    fake_python.write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "-m pip check" ]; then printf "%s" "$FAKE_PIP_CHECK_OUTPUT"; exit "$FAKE_PIP_CHECK_RC"; fi\n'
        'echo "unexpected call of the fake python: $*" >&2\n'
        "exit 99\n",
        encoding="utf-8",
    )
    fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)
    env = dict(os.environ, PATH=f"{fake_bin}{os.pathsep}{os.environ['PATH']}", FAKE_PIP_CHECK_OUTPUT=pip_output, FAKE_PIP_CHECK_RC=str(pip_rc))
    return subprocess.run(["bash", "-e", "-c", _run_text(step)], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=30)


@needs_bash
def test_a_clean_pip_check_passes(canary_steps, tmp_path):
    assert _run_pip_check_step(canary_steps, tmp_path, "No broken requirements found.\n", 0).returncode == 0


@needs_bash
def test_only_the_two_expected_m2c_lines_pass(canary_steps, tmp_path):
    result = _run_pip_check_step(canary_steps, tmp_path, EXPECTED_LINES, 1)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompt-toolkit" in result.stdout, "the findings must stay visible in the log"


@needs_bash
def test_websockets_drift_fails_loudly(canary_steps, tmp_path):
    result = _run_pip_check_step(canary_steps, tmp_path, EXPECTED_LINES + WEBSOCKETS_DRIFT, 1)
    assert result.returncode != 0
    assert "websockets" in result.stdout, "the offending line must be in the log"
    assert "::error" in result.stdout, "the failure must be annotated, not just a bare exit code"


@needs_bash
def test_a_broken_backend_pin_fails(canary_steps, tmp_path):
    result = _run_pip_check_step(canary_steps, tmp_path, BACKEND_BREAK, 1)
    assert result.returncode != 0
    assert "guardrails-ai" in result.stdout


@needs_bash
def test_a_failure_with_no_recognisable_output_fails_closed(canary_steps, tmp_path):
    """pip check exits 1 and prints something nobody expected (or nothing): that is a failure, never a pass."""
    assert _run_pip_check_step(canary_steps, tmp_path, "", 1).returncode != 0
    assert _run_pip_check_step(canary_steps, tmp_path, "Traceback (most recent call last):\n", 1).returncode != 0


# --- the instructions -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("path", [CANARY_README, ROOT_README], ids=["canary README", "root README"])
def test_the_readmes_use_no_deps_and_say_why(path):
    text = path.read_text(encoding="utf-8")
    assert "pip install --no-deps -e ../m2c" in text
    assert not re.search(r"pip install -e \.\./m2c", text), f"{path.name} still has the form that upgrades rich to 15"
    assert "rich" in text and "14.3.4" in text and "15.0.0" in text, f"{path.name} must say why (the rich pins differ)"
