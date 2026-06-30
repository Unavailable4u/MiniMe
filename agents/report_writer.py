import os
import json
from dotenv import load_dotenv
from google import genai

from memory.bus import read, write, KEYS
import time
load_dotenv()

api_key = os.getenv("GEMINI_API_KEY_1")
client = genai.Client(api_key=api_key)

SYSTEM_PROMPT = """You are a report writer for an autonomous coding pipeline.
Summarize this cycle in under 200 words for the next planner. Cover: what got
built, what's still broken, what should be prioritized next cycle. Be honest
about failures. Write in plain language, not JSON -- this is read by a human
and by the next planning agent as plain text.
"""


def run_report_writer():
    fixed_code = read(KEYS["fixed_code"])
    test_results = read(KEYS["test_results"])
    review_notes = read(KEYS["review_notes"])

    if not fixed_code or not test_results:
        raise ValueError("Missing fixed_code or test_results in memory. Run the Fixer+Tester first.")

    user_prompt = (
        "Review notes from this cycle:\n" + json.dumps(review_notes, indent=2)
        + "\n\nFixed code modules (names only, not full code, to keep this short):\n"
        + json.dumps(list(fixed_code.keys()))
        + "\n\nSandbox test results:\n" + json.dumps(test_results, indent=2)
    )

    max_retries = 4
    response = None
    for attempt in range(max_retries):
        try:
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=SYSTEM_PROMPT + "\n\n" + user_prompt,
            )
            break
        except Exception as exc:
            if attempt == max_retries - 1:
                raise
            wait = 2 ** attempt  # 1s, 2s, 4s, 8s
            print(f"  [Report Writer] API error ({exc.__class__.__name__}), "
                  f"retrying in {wait}s... (attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)

    report_text = response.text.strip()

    failed_modules = [
        name for name, result in test_results.items()
        if not result.get("passed", False)
    ]

    report_record = {
        "text": report_text,
        "all_tests_passed": len(failed_modules) == 0,
        "failed_modules": failed_modules,
    }

    write(KEYS["latest_report"], report_record)
    return report_record


if __name__ == "__main__":
    report = run_report_writer()
    print(json.dumps(report, indent=2))