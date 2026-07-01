"""
agents/sandbox_tester.py — Sandbox Tester (Part 4, agent #10 of the v5
Master Blueprint).

Replaces the testing half of the old agents/fixer_tester.py. Fixing is now
a separate agent: agents/fixer_pool.py (#9).

No LLM call -- this agent only executes code. Runs each fixed module in its
own E2B sandbox, genuinely in parallel (ThreadPoolExecutor), up to
MAX_WORKERS at once. Your E2B Hobby account supports 20 concurrent
sandboxes, so this stays well within that even at tier-3 scale (5 code
modules per cycle by default).
"""

import os
import sys
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from e2b_code_interpreter import Sandbox

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

load_dotenv()

e2b_api_key = os.getenv("E2B_API_KEY")
if e2b_api_key:
    os.environ["E2B_API_KEY"] = e2b_api_key

MAX_WORKERS = 5


def _run_one_module(module_name: str, module_data, test_code: str = "") -> tuple:
    if isinstance(module_data, str):
        module_data = {"language": "python", "code": module_data}
    elif not isinstance(module_data, dict):
        return module_name, {
            "passed": False,
            "stdout": "",
            "stderr": "",
            "error": f"Unexpected module data shape: {type(module_data).__name__}",
        }

    code = module_data.get("code", "")
    if not code:
        return module_name, {
            "passed": False,
            "stdout": "",
            "stderr": "No code found for this module.",
            "error": None,
        }

    try:
        full_code = code
        if test_code:
            full_code = code.rstrip() + "\n\n# --- Generated tests (Test Writer) ---\n" + test_code

        with Sandbox.create() as sbx:
            execution = sbx.run_code(full_code)
            stderr = execution.logs.stderr
            error = execution.error
            return module_name, {
                "passed": not error and not stderr,
                "stdout": execution.logs.stdout,
                "stderr": stderr,
                "error": str(error) if error else None,
            }
    except Exception as exc:
        return module_name, {
            "passed": False,
            "stdout": "",
            "stderr": "",
            "error": f"Sandbox failed to run: {exc}",
        }


def run_sandbox_tester():
    fixed_code = read(KEYS["fixed_code"])
    if not fixed_code:
        raise ValueError("No fixed_code found in memory. Run the Fixer Pool first.")

    test_code_map = read(KEYS["test_code"], default={})
    modules = {name: data for name, data in fixed_code.items() if name != "_fixer_error"}

    test_results = {}
    num_workers = min(MAX_WORKERS, max(len(modules), 1))
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = {
            executor.submit(_run_one_module, name, data, test_code_map.get(name, "")): name
            for name, data in modules.items()
        }
        for future in as_completed(futures):
            name, result = future.result()
            test_results[name] = result

    write(KEYS["test_results"], test_results)
    return test_results


if __name__ == "__main__":
    results = run_sandbox_tester()
    print(json.dumps(results, indent=2))
