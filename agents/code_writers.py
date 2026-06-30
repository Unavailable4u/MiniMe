import os
import sys
import json
from concurrent.futures import ThreadPoolExecutor
from dotenv import load_dotenv
from openai import OpenAI

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

load_dotenv()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

SYSTEM_PROMPT = """You are a focused implementer. Write complete, runnable Python code
for the module described below. Follow the spec exactly. Include basic input validation.
Do not invent features outside the spec. Output ONLY the code, no explanation, no markdown
code fences."""

def write_module(module_spec):
    """Generate code for a single module."""
    user_content = json.dumps(module_spec)
    response = client.chat.completions.create(
        model="openrouter/free",
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content}
        ]
    )
    code = response.choices[0].message.content.strip()
    # Strip markdown fences if present
    if code.startswith("```"):
        code = code.split("```")[1]
        if code.startswith("python"):
            code = code[6:]
        code = code.strip()
    return module_spec["name"], code

def run():
    specs = read(KEYS["module_specs"])
    modules = specs["modules"]

    results = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(write_module, m) for m in modules]
        for future in futures:
            name, code = future.result()
            results[name] = code

    write(KEYS["submitted_code"], results)
    return results

if __name__ == "__main__":
    results = run()
    for name, code in results.items():
        print(f"\n=== {name} ===")
        print(code[:300] + ("..." if len(code) > 300 else ""))