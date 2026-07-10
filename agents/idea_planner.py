import os
import sys
import json
from dotenv import load_dotenv
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.retry import call_with_retry
from utils.llm_client import generate_text
load_dotenv()

# Fallback chain per Part 4, agent #1 of the v5 blueprint:
# Groq llama-3.3-70b-versatile -> Cerebras gpt-oss-120b -> GitHub Models
# (Cerebras's llama-3.3-70b was deprecated Feb 2026 and now 404s; gpt-oss-120b
# is the one model guaranteed on Cerebras's public production tier.)
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_1"},
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
]

SYSTEM_PROMPT = """You are the product planner for an autonomous build loop.
Given the original idea, any prior report, and the current feature_status,
output ONLY a JSON object with:
- "features": a list of 3-6 feature names for the app (keep these names IDENTICAL
  across cycles once chosen -- do not rename or rephrase a feature once listed)
- "priorities": the same features ordered by priority (most important first)
- "target_feature": the EXACT name of ONE feature from "features" that this
  cycle's work will focus on. This must match a string in "features" exactly,
  character for character.
- "cycle_goal": ONE specific, small, buildable-in-one-pass goal for this cycle,
  describing the work on target_feature only.
RULES for choosing target_feature:
- feature_status will show you which features are "done", "in_progress", or
  missing (meaning not started). ALWAYS prefer a feature that is missing or
  "in_progress" over a feature already marked "done".
- Only pick a "done" feature again if EVERY other feature is also "done".
- Do not invent features outside the original idea's scope.
Respond with ONLY valid JSON, no markdown, no explanation."""
def run(session_id: str = None, domain: str = None):
    idea = read(KEYS["original_idea"])
    prior_report = read(KEYS["latest_report"], default=None)
    feature_status = read(KEYS["feature_status"], default={})
    user_content = f"Original idea: {idea}"
    user_content += f"\n\nCurrent feature_status: {json.dumps(feature_status)}"
    if prior_report:
        user_content += f"\n\nPrior cycle report: {json.dumps(prior_report)}"
    else:
        user_content += "\n\nThis is cycle 1. No prior report exists yet."
    raw_text = call_with_retry(
        lambda: generate_text(SYSTEM_PROMPT, user_content, CHAIN, agent_name="Idea Planner",
                               session_id=session_id, domain=domain),
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