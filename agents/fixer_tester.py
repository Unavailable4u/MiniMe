import os
import json
from dotenv import load_dotenv
from cerebras.cloud.sdk import Cerebras
from e2b_code_interpreter import Sandbox

from memory.bus import read, write, KEYS

load_dotenv()

cerebras_api_key = os.getenv("CEREBRAS_API_KEY")
client = Cerebras(api_key=cerebras_api_key)

e2b_api_key = os.getenv("E2B_API_KEY")
os.environ["E2B_API_KEY"] = e2b_api_key

SYSTEM_PROMPT = """You are a bug-fixing engineer. You will be given JSON containing
multiple code modules and a list of review issues found in them. Resolve every
"critical" and "moderate" issue. You may leave "minor" issues unless they're trivial
to fix. Do not change module names. Do not add new modules.

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly this shape:
{
  "module_name": {"language": "python", "code": "...full corrected code..."},
  "another_module": {"language": "python", "code": "...full corrected code..."}
}
Return ALL modules from the input, fixed or not, with their full code each time.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _fix_code(submitted_code: dict, review_notes: dict) -> dict:
    user_prompt = (
        "Submitted code:\n" + json.dumps(submitted_code, indent=2)
        + "\n\nReview issues to fix:\n" + json.dumps(review_notes, indent=2)
    )

    response = client.chat.completions.create(
        model="gpt-oss-120b",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )

    raw = response.choices[0].message.content
    cleaned = _strip_fences(raw)

    try:
        fixed_code = json.loads(cleaned)
    except json.JSONDecodeError:
        fixed_code = submitted_code
        fixed_code["_fixer_error"] = {
            "language": "n/a",
            "code": "Fixer output was not valid JSON. Original code kept as-is.",
        }

    return fixed_code


def _run_in_sandbox(fixed_code: dict) -> dict:
    """Runs each module's code in a fresh E2B sandbox, returns pass/fail + output."""
    test_results = {}

    with Sandbox.create() as sbx:
        for module_name, module_data in fixed_code.items():
            if module_name == "_fixer_error":
                continue

            code = module_data.get("code", "")
            if not code:
                test_results[module_name] = {
                    "passed": False,
                    "stdout": "",
                    "stderr": "No code found for this module.",
                }
                continue

            execution = sbx.run_code(code)

            stderr = execution.logs.stderr
            error = execution.error

            test_results[module_name] = {
                "passed": not error and not stderr,
                "stdout": execution.logs.stdout,
                "stderr": stderr,
                "error": str(error) if error else None,
            }

    return test_results


def run_fixer_and_tester():
    submitted_code = read(KEYS["submitted_code"])
    review_notes = read(KEYS["review_notes"])

    if not submitted_code:
        raise ValueError("No submitted_code found in memory. Run the Code Writers first.")
    if not review_notes:
        raise ValueError("No review_notes found in memory. Run the Reviewer first.")

    fixed_code = _fix_code(submitted_code, review_notes)
    write(KEYS["fixed_code"], fixed_code)

    test_results = _run_in_sandbox(fixed_code)
    write(KEYS["test_results"], test_results)

    return fixed_code, test_results


if __name__ == "__main__":
    fixed, results = run_fixer_and_tester()
    print("\n--- fixed_code ---")
    print(json.dumps(fixed, indent=2))
    print("\n--- test_results ---")
    print(json.dumps(results, indent=2))