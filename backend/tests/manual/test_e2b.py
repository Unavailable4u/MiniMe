"""
tests/manual/test_e2b.py — moved from tests/test_e2b.py (B1 manual-tier
migration). Spins up a real E2B sandbox with E2B_API_KEY; not run in CI.
(Contrast with tests/integration/test_sandbox_tester.py, which fakes out
Sandbox entirely so the agent logic can be exercised in CI without
touching E2B at all.)
"""
import os

import pytest
from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox

load_dotenv()

pytestmark = pytest.mark.manual


@pytest.mark.skipif(not os.getenv("E2B_API_KEY"), reason="E2B_API_KEY not set")
def test_e2b_sandbox_runs_code():
    api_key = os.getenv("E2B_API_KEY")
    os.environ["E2B_API_KEY"] = api_key

    with Sandbox.create() as sbx:
        execution = sbx.run_code("print('hello from sandbox')")
        output = execution.logs.stdout
        assert output, "expected non-empty stdout from sandbox"
        print("Output:", output)
