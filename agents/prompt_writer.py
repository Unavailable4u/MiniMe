import os
import re
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from utils.llm_client import generate_text

load_dotenv()

# Part 7 §7.5 — Sentry monitoring hook. Confirms the blueprint's own claim
# that monitoring needs "zero new code": integration_flagger (§7.3) tags
# "monitoring" once, early, and is cached at stage_output:{session_id}:
# integration_flagger (see api/server.py's get_tasks()) — this module just
# has to notice that tag and add one ordinary module_specs entry.
# agents/code_writers.py's pool handles the rest exactly like any other
# module, since _write_one_module() has no awareness of what a spec
# "is" beyond name/description/inputs/outputs/edge_cases.
MONITORING_MODULE_NAME = "monitoring_setup"

MONITORING_MODULE_SPEC = {
    "name": MONITORING_MODULE_NAME,
    "description": (
        "Initialize Sentry error tracking for this project via "
        "sentry_sdk.init(dsn=...). Read the DSN from a SENTRY_DSN "
        "environment variable; if it is unset or empty, skip "
        "initialization gracefully rather than raising, so a project "
        "without Sentry configured yet still runs normally."
    ),
    "inputs": "SENTRY_DSN environment variable (optional)",
    "outputs": "Sentry SDK initialized and capturing unhandled exceptions when SENTRY_DSN is set",
    "edge_cases": [
        "SENTRY_DSN not set -> skip init, do not raise",
        "sentry-sdk not installed -> fail gracefully, do not crash the app",
    ],
}


def _parse_fenced_json(text):
    """Intentional twin of api/server.py's own _parse_fenced_json() --
    duplicated rather than imported, same reasoning
    agents/deploy_config_writer.py's docstring already gives for
    duplicating structure_architect.py's _get_project_tree(): this
    codebase has no shared-util module for this, and an agents/ module
    importing from the api/ layer would be a backwards dependency (the
    API layer imports agent modules, not the other way around). Keep
    this in sync with api/server.py's copy by hand if that parsing logic
    ever changes.

    Returns [] (not None) on anything unparseable, matching
    api/server.py's own fail-quiet convention for a role that hasn't
    run yet."""
    if not text:
        return []
    match = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    raw = match.group(1) if match else text
    try:
        parsed = json.loads(raw.strip())
        return parsed.get("integrations", []) if isinstance(parsed, dict) else []
    except (json.JSONDecodeError, AttributeError):
        return []


def _maybe_add_monitoring_module(specs: dict, session_id: str) -> dict:
    """Part 7 §7.5. Appends MONITORING_MODULE_SPEC to specs["modules"] if
    (a) integration_flagger's cached output for this session flagged
    "monitoring", and (b) a module of that name isn't already present --
    idempotent within one cycle's module list, even though this whole
    function re-runs every cycle same as the rest of module_specs (that's
    fine; every other module in the list gets regenerated every cycle
    too, this is no different).

    No-op (returns specs unchanged) if session_id is falsy, matching the
    fail-quiet convention every other session_id-keyed lookup in this
    codebase already uses (see eo/conversation_memory.py's
    _workspace_facts_text(), agents/deploy_agent.py's own docstring)."""
    if not session_id:
        return specs
    flagged_text = read(f"stage_output:{session_id}:integration_flagger", default=None)
    integrations = _parse_fenced_json(flagged_text)
    is_monitoring_flagged = any(item.get("type") == "monitoring" for item in integrations)
    if not is_monitoring_flagged:
        return specs

    modules = specs.setdefault("modules", [])
    already_present = any(
        (m.get("name") or "").strip().lower() == MONITORING_MODULE_NAME
        for m in modules
    )
    if not already_present:
        modules.append(dict(MONITORING_MODULE_SPEC))
    return specs

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

def run(session_id: str = None, tier: int = None, domain: str = None):
    plan = read(KEYS["current_plan"])
    cycle_goal = plan["cycle_goal"]

    raw_text = generate_text(
        SYSTEM_PROMPT,
        f"cycle_goal: {cycle_goal}",
        CHAIN,
        agent_name="Prompt Writer",
        session_id=session_id,
        tier=tier,
        domain=domain,  # Migration Part 2 §2.6: cost-tracking gap
    ).strip()
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    specs = json.loads(raw_text)
    specs = _maybe_add_monitoring_module(specs, session_id)  # Part 7 §7.5
    write(KEYS["module_specs"], specs)
    return specs

if __name__ == "__main__":
    specs = run()
    print(json.dumps(specs, indent=2))