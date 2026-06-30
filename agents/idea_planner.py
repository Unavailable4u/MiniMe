import os
import sys
import json
from dotenv import load_dotenv
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.retry import call_with_retry
from utils.gemini_client import generate_text
load_dotenv()
SYSTEM_PROMPT = """You are the product planner for an autonomous build loop.
Given the original idea and any prior report, output ONLY a JSON object with:
- "features": a list of 3-6 feature names for the app
- "priorities": the same features ordered by priority (most important first)
- "cycle_goal": ONE specific, small, buildable-in-one-pass goal for this cycle only
IMPORTANT — avoid getting stuck refining one feature indefinitely:
- If the prior report shows a feature is reasonably working (tests passing, no
  unresolved critical issues), move on to the NEXT unbuilt feature in priorities,
  even if the current one could still be polished further.
- Do not generate more than 2 consecutive cycle_goals that refine or harden the
  same feature. Polish is welcome, but never at the cost of leaving a listed
  feature completely unstarted.
- Treat "unstarted, listed feature" as higher priority than "deeper validation
  or robustness on an already-working feature."
Keep cycle_goal narrow. Do not invent unrelated features. Respond with ONLY valid JSON, no markdown, no explanation."""
def run():
    idea = read(KEYS["original_idea"])
    prior_report = read(KEYS["latest_report"], default=None)
    user_content = f"Original idea: {idea}"
    if prior_report:
        user_content += f"\n\nPrior cycle report: {json.dumps(prior_report)}"
    else:
        user_content += "\n\nThis is cycle 1. No prior report exists yet."
    raw_text = call_with_retry(
        lambda: generate_text(SYSTEM_PROMPT, user_content, agent_name="Idea Planner"),
        agent_name="Idea Planner",
    )
    # Strip markdown code fences if the model adds them anyway
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()
    plan = json.loads(raw_text)
    write(KEYS["current_plan"], plan)
    return plan
if __name__ == "__main__":
    plan = run()
    print(json.dumps(plan, indent=2))