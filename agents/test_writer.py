"""
agents/test_writer.py — Test Writer (Part 4, agent #5 of the v5 Master
Blueprint).

Runs after Code Writers, before the Reviewer Pool -- generates test code
for each freshly-written module. Output is plain assertion-style code
(not pytest) meant to be appended directly after a module's own code and
executed as one script: matches how sandbox_tester.py runs everything (a
single blob per module in one E2B sandbox, checking stderr), rather than
introducing a pytest runner the rest of the pipeline doesn't expect.

sandbox_tester.py appends this test code after a module's code when
present, so a module can "pass" the sandbox run only if it doesn't error
out AND its own generated tests don't raise AssertionError.

- Model: Groq `qwen/qwen3-32b`, fallback GitHub Models `o4-mini`.
- No dedicated key split -- this is a single sequential call per cycle,
  same low-volume tier as Idea Planner / Prompt Writer / Report Writer,
  so it shares the default GROQ_API_KEY / GITHUB_MODELS_PAT.
"""

import os
import sys
import json

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.retry import call_with_retry
from utils.llm_client import generate_text

load_dotenv()

# Groq qwen/qwen3-32b -> GitHub Models o4-mini, per Part 4, agent #5.
CHAIN = [
    {"provider": "groq", "model": "qwen/qwen3-32b", "key_env": "GROQ_API_KEY"},
    {"provider": "github", "model": "openai/o4-mini", "key_env": "GITHUB_MODELS_PAT"},
]

SYSTEM_PROMPT = """You are a test writer for an autonomous build loop. You will be
given JSON containing one or more freshly-written code modules. For each module,
write short test code that exercises its main functions with plain `assert`
statements -- NOT pytest, NOT unittest, no imports of testing frameworks.

Critical constraint: your test code will be appended directly AFTER the
module's own code in the same script and executed together. Do NOT redefine
any function, class, or variable from the module -- call the ones that will
already be in scope. Do NOT import the module; it's already loaded above
your code in the same file.

If a module has nothing meaningfully testable (e.g. pure UI markup, a config
file, boilerplate with no logic), output a single line: `# no testable logic`
for that module -- do not invent fake assertions just to have something.

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly this
shape:
{
  "module_name": "assert some_function(1, 2) == 3\\nassert other_function('') is None\\n"
}
Return an entry for every module you were given.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def run():
    submitted_code = read(KEYS["submitted_code"])
    if not submitted_code:
        raise ValueError("No submitted_code found in memory. Run the Code Writers first.")

    user_content = (
        "Modules to write tests for:\n" + json.dumps(submitted_code, indent=2)
    )

    raw_text = call_with_retry(
        lambda: generate_text(SYSTEM_PROMPT, user_content, CHAIN, agent_name="Test Writer"),
        agent_name="Test Writer",
    )
    cleaned = _strip_fences(raw_text)

    try:
        test_code = json.loads(cleaned)
        if not isinstance(test_code, dict):
            raise json.JSONDecodeError("expected a JSON object", cleaned, 0)
    except json.JSONDecodeError:
        print("  [Test Writer] output was not valid JSON -- no tests generated this cycle.")
        test_code = {}

    # Only keep string values -- anything else is a malformed entry we'd
    # rather drop than have crash sandbox_tester.py downstream.
    test_code = {
        name: code for name, code in test_code.items()
        if isinstance(code, str) and name in submitted_code
    }

    write(KEYS["test_code"], test_code)
    return test_code


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
