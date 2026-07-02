import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.llm_client import generate_text

load_dotenv()

# Part 4, agent #2 — Groq primary, GitHub Models fallback. Same chain
# shape as the rest of the roster now that this goes through
# generate_text() instead of a hand-rolled Groq client (this is what
# makes usage logging work for this agent, per Stage 6 cleanup).
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
]

SYSTEM_PROMPT = """You are a technical spec writer for an autonomous build loop.
Given a cycle_goal, break it into 2-3 independent modules that can be built in parallel
without depending on each other's internal code (only on their defined interface).

Output ONLY a JSON object with a "modules" key containing a list. Each module must have:
- "name": short module name
- "description": what it does
- "inputs": expected inputs
- "outputs": expected outputs
- "edge_cases": list of edge cases to handle

Respond with ONLY valid JSON, no markdown, no explanation."""

def run(session_id: str = None, tier: int = None):
    plan = read(KEYS["current_plan"])
    cycle_goal = plan["cycle_goal"]

    raw_text = generate_text(
        SYSTEM_PROMPT,
        f"cycle_goal: {cycle_goal}",
        CHAIN,
        agent_name="Prompt Writer",
        session_id=session_id,
        tier=tier,
    ).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    specs = json.loads(raw_text)
    write(KEYS["module_specs"], specs)
    return specs

if __name__ == "__main__":
    specs = run()
    print(json.dumps(specs, indent=2))