"""
agents/final_qa.py — Final QA/Acceptance Reviewer (Part 4, agent #16 of
the v5 Master Blueprint).

Provider: Mistral La Plateforme, "mistral-large-latest" (blueprint pins
"Mistral Large 3" -- see documentation_agent.py's docstring for why this
uses the -latest alias instead). No fallback specified in the blueprint.

Runs after report_writer.py, before gatekeeper.py -- this is the last
judgment call before the mechanical CONTINUE/STOP/PAUSE decision, looking
across everything the cycle produced: test results, security findings,
duplication flags, and the report itself.

This agent is advisory, not a hard gate by itself: it writes a verdict to
KEYS["final_qa_verdict"] that gatekeeper.py can read and factor in. It does
NOT call sys.exit or otherwise stop the loop on its own -- per Part 12,
item 6 of the blueprint, tier-3 has no human review gate beyond the
Gatekeeper's own judgment, and this agent's job is to feed that judgment
better information, not to override it.
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

# No fallback specified in the blueprint for this agent -- single-step
# chain, same as documentation_agent.py. Routed through generate_text()
# (llm_client.py's "mistral" provider) instead of a hand-rolled client,
# so this call actually gets usage-logged -- previously it logged nothing.
CHAIN = [
    {"provider": "mistral", "model": "mistral-large-latest", "key_env": "MISTRAL_API_KEY"},
]

SYSTEM_PROMPT = """You are the final acceptance reviewer for an autonomous
build cycle. You will be given the cycle report, sandbox test results,
security scan findings, and any duplication flags. Decide whether this
cycle's output is acceptable to keep as-is.
Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly
this shape:
{
  "accept": true,
  "concerns": ["short concern 1", "..."],
  "summary": "one or two sentence verdict"
}
Set "accept" to false only for critical problems (failing tests, critical
security findings, or a cycle that clearly didn't do what it claimed) --
minor style nitpicks should be a "concern," not a rejection.
"""

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def run(session_id: str = None, tier: int = None) -> dict:
    report = read(KEYS["latest_report"], default={})
    test_results = read(KEYS["test_results"], default={})
    security_results = read(KEYS["security_scan_results"], default={})
    duplication_report = read(KEYS["duplication_report"], default={})

    user_prompt = json.dumps({
        "report": report,
        "test_results": test_results,
        "security_scan_results": security_results,
        "duplication_report": duplication_report,
    }, indent=2)

    try:
        raw_text = call_with_retry(
            lambda: generate_text(SYSTEM_PROMPT, user_prompt, CHAIN, agent_name="Final QA",
                                   session_id=session_id, tier=tier),
            agent_name="Final QA",
        )
        verdict = json.loads(_strip_fences(raw_text))
    except Exception as exc:
        # Advisory agent -- never block the loop if this itself fails.
        verdict = {"accept": True, "concerns": [f"Final QA agent failed: {exc}"], "summary": "Final QA unavailable this cycle."}

    write(KEYS["final_qa_verdict"], verdict)
    return verdict


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))