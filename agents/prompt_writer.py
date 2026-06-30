import os
import sys
import json
from dotenv import load_dotenv
from groq import Groq

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

load_dotenv()

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

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

def run():
    plan = read(KEYS["current_plan"])
    cycle_goal = plan["cycle_goal"]

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"cycle_goal: {cycle_goal}"}
        ]
    )

    raw_text = response.choices[0].message.content.strip()
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