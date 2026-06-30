import os
import json
from dotenv import load_dotenv
from cerebras.cloud.sdk import Cerebras

from memory.bus import read, write, KEYS

load_dotenv()

api_key = os.getenv("CEREBRAS_API_KEY")
client = Cerebras(api_key=api_key)

SYSTEM_PROMPT = """You are a strict code reviewer. You will be given JSON containing
multiple code modules submitted by different writers. List every bug, security risk,
and interface mismatch between modules. Rate each issue: critical, moderate, minor.
Be specific about which module/file the issue is in.

Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly this shape:
{
  "issues": [
    {"module": "module_name", "severity": "critical|moderate|minor", "description": "..."}
  ],
  "summary": "one or two sentence overall verdict"
}
If there are no issues, return an empty issues list.
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def run_reviewer():
    submitted_code = read(KEYS["submitted_code"])
    if not submitted_code:
        raise ValueError("No submitted_code found in memory. Run the Code Writers first.")

    user_prompt = (
        "Here is the submitted code from all modules this cycle:\n\n"
        + json.dumps(submitted_code, indent=2)
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
        review_notes = json.loads(cleaned)
    except json.JSONDecodeError:
        review_notes = {
            "issues": [
                {
                    "module": "reviewer",
                    "severity": "critical",
                    "description": "Reviewer output was not valid JSON. Raw output saved.",
                }
            ],
            "summary": "Reviewer parsing failed.",
            "raw_output": raw,
        }

    write(KEYS["review_notes"], review_notes)
    return review_notes


if __name__ == "__main__":
    notes = run_reviewer()
    print(json.dumps(notes, indent=2))