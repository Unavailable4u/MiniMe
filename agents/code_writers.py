import os
import sys
import json
import time
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIStatusError

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS

load_dotenv()

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)

# Real free model IDs tried in order. If one is rate-limited, next is tried.
# Verify current free models at: openrouter.ai/models?max_price=0
FREE_MODELS = [
    "openai/gpt-oss-120b:free",
    "meta-llama/llama-3.3-70b-instruct:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
]

# Seconds to wait between writing each module (avoids per-minute rate limits)
INTER_MODULE_DELAY = 10

SYSTEM_PROMPT = """You are a focused implementer. Write complete, runnable Python code
for the module described below. Follow the spec exactly. Include basic input validation.
Do not invent features outside the spec. Output ONLY the code, no explanation, no markdown
code fences."""


def _call_with_model_fallback(user_content: str) -> str:
    """
    Tries each model in FREE_MODELS in order.
    On 429 or provider error, waits briefly then moves to the next model.
    Returns the generated code string.
    Raises RuntimeError if all models are exhausted.
    """
    for model_index, model in enumerate(FREE_MODELS):
        print(f"    [Code Writer] trying model: {model}")
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_content},
                ],
            )
            content = response.choices[0].message.content or ""
            return content.strip()

        except (RateLimitError, APIStatusError) as exc:
            is_last = model_index == len(FREE_MODELS) - 1
            if is_last:
                raise RuntimeError(
                    f"All free models exhausted. Last error: {exc}"
                ) from exc
            wait = 12
            print(f"    [Code Writer] {model} rate-limited ({exc.__class__.__name__}), "
                  f"waiting {wait}s then trying next model...")
            time.sleep(wait)

        except Exception as exc:
            # Non-rate-limit errors (bad JSON, network blip) — don't skip to
            # next model, just re-raise so the loop can surface the real problem.
            raise


def write_module(module_spec: dict) -> tuple[str, str]:
    """Generate code for a single module. Returns (module_name, code)."""
    user_content = json.dumps(module_spec)
    raw = _call_with_model_fallback(user_content)

    # Strip markdown fences if the model adds them anyway
    code = raw
    if code.startswith("```"):
        code = code.split("```")[1]
        if code.startswith("python"):
            code = code[6:]
        code = code.strip()

    if not code:
        code = (
            f"# CODE WRITER FAILED: model returned empty content. "
            f"No code generated for module '{module_spec.get('name', '?')}'."
        )

    return module_spec["name"], code


def run():
    specs = read(KEYS["module_specs"])
    modules = specs["modules"]
    results = {}

    # Sequential — NOT parallel. Three simultaneous calls on a free tier
    # (20 req/min limit) instantly triggers rate limiting. The loop is
    # network-bound anyway; sequential with a small gap is just as fast
    # in wall-clock terms once rate limits are factored in.
    for i, module in enumerate(modules):
        if i > 0:
            print(f"    [Code Writer] waiting {INTER_MODULE_DELAY}s before next module...")
            time.sleep(INTER_MODULE_DELAY)
        name, code = write_module(module)
        results[name] = code
        print(f"    [Code Writer] wrote module: {name} ({len(code)} chars)")

    write(KEYS["submitted_code"], results)
    return results


if __name__ == "__main__":
    results = run()
    for name, code in results.items():
        print(f"\n=== {name} ===")
        print(code[:300] + ("..." if len(code) > 300 else ""))