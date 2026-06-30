import os
import sys
import json
from dotenv import load_dotenv
from google import genai

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

load_dotenv()

client = genai.Client(api_key=os.getenv("GEMINI_API_KEY_1"))

SYSTEM_PROMPT = """You are the product planner for an autonomous build loop.
Given the original idea and any prior report, output ONLY a JSON object with:
- "features": a list of 3-6 feature names for the app
- "priorities": the same features ordered by priority (most important first)
- "cycle_goal": ONE specific, small, buildable-in-one-pass goal for this cycle only

Keep cycle_goal narrow. Do not invent unrelated features. Respond with ONLY valid JSON, no markdown, no explanation."""

def run():
    idea = read(KEYS["original_idea"])
    prior_report = read(KEYS["latest_report"], default=None)

    user_content = f"Original idea: {idea}"
    if prior_report:
        user_content += f"\n\nPrior cycle report: {json.dumps(prior_report)}"
    else:
        user_content += "\n\nThis is cycle 1. No prior report exists yet."

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=f"{SYSTEM_PROMPT}\n\n{user_content}"
    )

    raw_text = response.text.strip()
    # Strip markdown code fences if Gemini adds them anyway
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