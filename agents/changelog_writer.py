"""
agents/changelog_writer.py — Changelog/Commit-Message Writer (Part 4, agent
#15 of the v5 Master Blueprint).

Runs after File Manager, before Report Writer. Turns the cycle's actual
changes (what File Manager wrote/moved/deleted, the cycle goal, whether
tests passed) into a short commit message and a one-line changelog entry.

Per the blueprint table this agent is single-provider with no fallback
listed (GitHub Models gpt-4.1-mini only) -- it's low-stakes, low-volume
text generation, not something worth spending a second key on. If it
starts failing in practice, adding a Groq step to CHAIN is a one-line
change, same pattern as every other agent's CHAIN in this codebase.

Note: this agent only writes the message to memory -- it does not run
`git commit` itself. Blueprint Part 5.3 flags this as "load-bearing for
the loop trigger" once GitHub Actions scheduling exists; until then this
is inert but ready for that wiring.
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

# GitHub Models gpt-4.1-mini, per Part 4, agent #15 -- no fallback per spec.
CHAIN = [
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
]

SYSTEM_PROMPT = """You are a commit-message and changelog writer for an autonomous
build loop. Given this cycle's goal, which files were written/moved/deleted, and
whether sandbox tests passed, output ONLY a JSON object with:
- "commit_message": a single conventional-commit-style line (e.g.
  "feat: add task input validation" or "fix: resolve NameError in delete_all"),
  under 72 characters, no trailing period.
- "changelog_entry": one plain-English sentence a non-technical reader could
  understand, describing what changed this cycle.
Base both strictly on the information given -- do not invent features or
changes that aren't reflected in the file list or cycle goal.
Respond with ONLY valid JSON, no markdown fences, no preamble."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def run():
    plan = read(KEYS["current_plan"], default={})
    cycle_goal = plan.get("cycle_goal", "unspecified")
    fm_summary = read("last_file_manager_summary", default={})
    test_results = read(KEYS["test_results"], default={})
    cycle_num = read(KEYS["cycle_count"], default=1)

    passed = sum(1 for r in test_results.values() if r.get("passed"))
    total = len(test_results)

    user_content = json.dumps({
        "cycle_num": cycle_num,
        "cycle_goal": cycle_goal,
        "files_written": fm_summary.get("written", []),
        "files_moved": fm_summary.get("moved", []),
        "files_deleted": fm_summary.get("deleted", []),
        "sandbox_tests": f"{passed}/{total} passed",
    }, indent=2)

    try:
        raw_text = call_with_retry(
            lambda: generate_text(SYSTEM_PROMPT, user_content, CHAIN, agent_name="Changelog Writer"),
            agent_name="Changelog Writer",
        )
    except Exception as exc:
        print(f"  [Changelog Writer] failed ({exc}), falling back to a plain message.")
        result = {
            "commit_message": f"cycle {cycle_num}: {cycle_goal}"[:72],
            "changelog_entry": f"Cycle {cycle_num}: {cycle_goal}. {passed}/{total} sandbox tests passed.",
        }
        write(KEYS["commit_message"], result["commit_message"])
        write(KEYS["changelog_entry"], result["changelog_entry"])
        return result

    cleaned = _strip_fences(raw_text)
    try:
        result = json.loads(cleaned)
        if not isinstance(result, dict) or "commit_message" not in result:
            raise json.JSONDecodeError("missing commit_message", cleaned, 0)
    except json.JSONDecodeError:
        print("  [Changelog Writer] output was not valid JSON, using a plain fallback message.")
        result = {
            "commit_message": f"cycle {cycle_num}: {cycle_goal}"[:72],
            "changelog_entry": f"Cycle {cycle_num}: {cycle_goal}. {passed}/{total} sandbox tests passed.",
        }

    write(KEYS["commit_message"], result.get("commit_message", ""))
    write(KEYS["changelog_entry"], result.get("changelog_entry", ""))
    return result


if __name__ == "__main__":
    result = run()
    print(json.dumps(result, indent=2))
