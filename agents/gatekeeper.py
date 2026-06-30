import os
import json
from dotenv import load_dotenv
from groq import Groq

from memory.bus import read, write, KEYS

load_dotenv()

api_key = os.getenv("GROQ_API_KEY")
client = Groq(api_key=api_key)

# --- Safety configuration (Section 8 of the blueprint) ---
MAX_CYCLES = 10            # Hard cycle cap
HUMAN_CHECKPOINT_EVERY = 5  # Force a pause every N cycles regardless of verdict

SYSTEM_PROMPT = """You are the gatekeeper for an autonomous coding pipeline.
Given a cycle report and the cycle count, decide one of exactly three words:
CONTINUE, PAUSE_FOR_HUMAN, or STOP.

CONTINUE if real progress was made and no critical unresolved issue exists.
PAUSE_FOR_HUMAN if something looks seriously wrong and a human should look first.
STOP if the report indicates the project is effectively complete or unrecoverable.

Respond with ONLY one of these three words, nothing else.
"""


def _get_critical_issue_keys(review_notes: dict) -> set:
    if not review_notes:
        return set()
    issues = review_notes.get("issues", [])
    return {
        (issue.get("module", ""), issue.get("description", ""))
        for issue in issues
        if issue.get("severity") == "critical"
    }


def _ask_llm_for_decision(report: dict, cycle_count: int) -> str:
    user_prompt = (
        f"Cycle count: {cycle_count}\n\n"
        f"Report:\n{json.dumps(report, indent=2)}"
    )
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    )
    decision = response.choices[0].message.content.strip().upper()
    if decision not in ("CONTINUE", "PAUSE_FOR_HUMAN", "STOP"):
        decision = "PAUSE_FOR_HUMAN"
    return decision


def run_gatekeeper(cycle_count: int = None) -> str:
    if cycle_count is None:
        cycle_count = read(KEYS["cycle_count"], default=1)

    report = read(KEYS["latest_report"])
    review_notes = read(KEYS["review_notes"])

    if not report:
        raise ValueError("No latest_report found in memory. Run the Report Writer first.")

    # --- Hard rule 1: cycle cap ---
    if cycle_count >= MAX_CYCLES:
        decision = "STOP"
        write(KEYS["loop_decision"], decision)
        return decision

    # --- Hard rule 2: repeat-failure breaker ---
    current_critical = _get_critical_issue_keys(review_notes)
    previous_critical = set(
        tuple(pair) for pair in read("previous_critical_issues", default=[])
    )
    repeated_critical = current_critical & previous_critical

    if repeated_critical:
        decision = "PAUSE_FOR_HUMAN"
        write(KEYS["loop_decision"], decision)
        write("previous_critical_issues", [list(pair) for pair in current_critical])
        return decision

    write("previous_critical_issues", [list(pair) for pair in current_critical])

    # --- Hard rule 3: human checkpoint every N cycles ---
    if cycle_count % HUMAN_CHECKPOINT_EVERY == 0:
        decision = "PAUSE_FOR_HUMAN"
        write(KEYS["loop_decision"], decision)
        return decision

    # --- No hard rule triggered: ask the LLM for judgment ---
    decision = _ask_llm_for_decision(report, cycle_count)
    write(KEYS["loop_decision"], decision)
    return decision


if __name__ == "__main__":
    result = run_gatekeeper()
    print("Gatekeeper decision:", result)