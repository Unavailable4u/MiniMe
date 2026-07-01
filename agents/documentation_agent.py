"""
agents/documentation_agent.py — Documentation Agent (Part 4, agent #14 of
the v5 Master Blueprint).

Provider: Mistral La Plateforme, "mistral-medium-latest" (the blueprint
pins "Mistral Medium 3" -- using Mistral's rolling -latest alias so this
doesn't silently 404 the next time they bump the point version, same
reasoning idea_planner.py's comment gives for its Cerebras model choice).
No fallback specified in the blueprint for this agent.

Mistral's API is OpenAI-compatible, so this reuses the `openai` package
already in requirements.txt (same trick GitHub Models uses in
llm_client.py) rather than adding the `mistralai` SDK as a new dependency.

Runs after file_manager.py: docs describe what actually got written to
disk this cycle, not what was merely planned.

Unlike most agents, this one is allowed to touch the filesystem directly
(README.md only, never code) -- same trust level file_manager.py already
gives itself for README skeleton generation on cycle 1.
"""
import os
import sys
import json
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIStatusError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.retry import call_with_retry

load_dotenv()

MISTRAL_MODEL = "mistral-medium-latest"
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
APPS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")

SYSTEM_PROMPT = """You are a technical writer. You will be given a summary of
what was built/changed this cycle and the current file tree. Write updated
README content: what the app does, current features (done vs in progress),
how to run it, and a short "recent changes" note for this cycle.
Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly
this shape:
{"readme_markdown": "# Full README content in markdown..."}
"""

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        api_key = os.getenv("MISTRAL_API_KEY")
        if not api_key:
            raise RuntimeError("MISTRAL_API_KEY not set")
        _client = OpenAI(base_url=MISTRAL_BASE_URL, api_key=api_key)
    return _client


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _call_mistral(user_prompt: str) -> str:
    client = _get_client()
    response = client.chat.completions.create(
        model=MISTRAL_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    return (response.choices[0].message.content or "").strip()


def run() -> dict:
    idea = read(KEYS["original_idea"], default="")
    feature_status = read(KEYS["feature_status"], default={})
    report = read(KEYS["latest_report"], default={})
    file_map = read(KEYS["file_map"], default={})
    slug = read(KEYS["app_slug"], default=None)

    user_prompt = json.dumps({
        "idea": idea,
        "feature_status": feature_status,
        "this_cycle_summary": report.get("summary", ""),
        "file_map": file_map,
    }, indent=2)

    raw_text = call_with_retry(
        lambda: _call_mistral(user_prompt),
        agent_name="Documentation Agent",
    )
    doc = json.loads(_strip_fences(raw_text))
    write(KEYS["doc_output"], doc)

    if slug:
        readme_path = os.path.join(APPS_ROOT, slug, "README.md")
        if os.path.isdir(os.path.dirname(readme_path)):
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(doc.get("readme_markdown", ""))

    return doc


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
