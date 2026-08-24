"""
tests/integration/test_sandbox_tester.py — mocked rebuild of the old
tests/test_sandbox_tester.py.

This agent makes no LLM call at all (see its own module docstring) --
it spins up real E2B sandboxes and executes code in them. There is
nothing here for mock_llm to patch; instead this fakes out
agents.sandbox_tester.Sandbox itself with a stand-in that actually
executes the given code via a local subprocess, so passed/failed
results stay genuinely meaningful (a broken module really does fail)
without any network call to E2B. A pure "always returns passed=True"
stub would let a real regression (e.g. sandbox_tester stops appending
generated test code) slip through unnoticed.
"""
import subprocess
import sys

import pytest

from agents import sandbox_tester
from memory.bus import KEYS, read, write


class _FakeExecutionLogs:
    def __init__(self, stdout, stderr):
        self.stdout = stdout
        self.stderr = stderr


class _FakeExecution:
    def __init__(self, stdout, stderr, error):
        self.logs = _FakeExecutionLogs(stdout, stderr)
        self.error = error


class _FakeSandbox:
    """Runs code with the real local Python interpreter in a subprocess
    -- close enough to E2B's actual "run this code, tell me stdout/
    stderr/error" contract to exercise sandbox_tester.py's own pass/fail
    logic honestly, with no network dependency."""

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def run_code(self, code):
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=15,
        )
        stdout = [proc.stdout] if proc.stdout else []
        stderr = [proc.stderr] if proc.stderr else []
        error = proc.stderr.strip().splitlines()[-1] if proc.returncode != 0 and proc.stderr else None
        return _FakeExecution(stdout, stderr, error)


class _FakeSandboxCls:
    @staticmethod
    def create(*args, **kwargs):
        return _FakeSandbox()


@pytest.fixture
def fake_sandbox(monkeypatch):
    monkeypatch.setattr(sandbox_tester, "Sandbox", _FakeSandboxCls)


FIXED_CODE = {
    "todo_storage": {
        "language": "python",
        "code": (
            "def add_todo(todos, item):\n"
            "    todos.append(item)\n"
            "    return todos\n\n"
            "todos = []\n"
            "add_todo(todos, 'buy milk')\n"
            "print(todos)\n"
        ),
    },
    "broken_module": {
        "language": "python",
        "code": "print(undefined_variable)\n",
    },
}


def test_clean_module_passes_and_broken_module_fails(fake_sandbox):
    write(KEYS["fixed_code"], FIXED_CODE)

    results = sandbox_tester.run_sandbox_tester()

    assert results["todo_storage"]["passed"] is True
    assert results["broken_module"]["passed"] is False
    assert results["broken_module"]["error"] or results["broken_module"]["stderr"]
    assert read(KEYS["test_results"]) == results


def test_appends_generated_test_code_and_a_failing_test_fails_the_module(fake_sandbox):
    """sandbox_tester.py appends Test Writer's generated test code after
    the module's own code and runs both as one script -- a module can
    "pass" the sandbox run only if the module itself AND its generated
    tests both succeed."""
    write(KEYS["fixed_code"], {
        "math_utils": {
            "language": "python",
            "code": "def add(a, b):\n    return a + b\n",
        },
    })
    write(KEYS["test_code"], {
        "math_utils": "assert add(2, 2) == 5\n",  # deliberately wrong
    })

    results = sandbox_tester.run_sandbox_tester()

    assert results["math_utils"]["passed"] is False


def test_falls_back_to_submitted_code_when_fixed_code_is_absent(fake_sandbox):
    write(KEYS["submitted_code"], {
        "math_utils": {"language": "python", "code": "print('ok')\n"},
    })

    results = sandbox_tester.run_sandbox_tester()

    assert results["math_utils"]["passed"] is True


def test_raises_missing_dependency_with_no_code_at_all(fake_sandbox):
    from eo.errors import MissingDependencyError

    with pytest.raises(MissingDependencyError):
        sandbox_tester.run_sandbox_tester()
