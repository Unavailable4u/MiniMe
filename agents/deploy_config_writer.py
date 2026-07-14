"""
agents/deploy_config_writer.py — Part 7 §7.4, step 1 of the propose/apply
split. Decides WHICH free host fits this project and WHAT the deploy
config file should contain. Does not touch the filesystem itself; it only
outputs a plan that agents/deploy_agent.py then writes to disk
deterministically -- the exact same split structure_architect.py/
file_manager.py already proved out (structure_architect.py proposes a
JSON plan, file_manager.py executes it; this pair mirrors that shape one
stage later in the pipeline, for deployment instead of file layout).

Deliberately its own REAL_ACTION_ROLES module rather than a generic_worker
role, even though it's pure reasoning with no real action of its own: it
needs the project's actual on-disk file tree and language/stack signal
(module_specs), and agents/generic_worker.py's run() has no filesystem
access and no bus-key reads beyond input_keys/task_text -- the identical
reason structure_architect.py itself is a dedicated module and not
generic_worker, despite also never writing to disk.

Runs after file_manager.py, once code actually exists on disk to inspect
-- see eo/structure.py's STRUCTURE_TEMPLATES["coding"] placement.

Place this file at: agents/deploy_config_writer.py
"""

import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS, get_current_app_slug
from utils.llm_client import generate_text
from relay.emitter import emit_event

load_dotenv()

# Same reasoning-role chain shape as agents/prompt_writer.py -- this is a
# planning/reasoning call (propose a config), not the tight, latency-
# sensitive structure_architect.py call that runs mid-cycle right before
# a file write; a slightly longer fallback chain is fine here.
CHAIN = [
    {"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "GROQ_API_KEY"},
    {"provider": "cerebras", "model": "gpt-oss-120b", "key_env": "CEREBRAS_API_KEY_1"},
    {"provider": "github", "model": "openai/gpt-4.1-mini", "key_env": "GITHUB_MODELS_PAT"},
]

APPS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")

DEPLOY_CONFIG_PLAN_KEY = "deploy_config_plan"

SYSTEM_PROMPT = """You are the deployment config writer for an autonomous coding
pipeline. You decide WHICH free hosting platform fits a finished project and WHAT
its deploy config file should contain -- you never deploy anything yourself, only
propose a plan the deterministic writer executes.

You will be given the project's current file tree and the module specs for this
cycle (language/stack signal).

Pick exactly ONE platform that fits what you see:
- "render" (render.yaml) -- general-purpose backend/full-stack services
- "fly" (fly.toml) -- general-purpose, Dockerfile-friendly services
- "github_pages" (a GitHub Actions workflow YAML) -- static output only
  (plain HTML/CSS/JS, or a static site generator's build output)
- "vercel" (vercel.json) -- frontend-only projects (React/Next/Vite/etc.)

Respond with ONLY valid JSON, no markdown fences, no explanation, in exactly this
shape:
{
  "platform": "render",
  "config_filename": "render.yaml",
  "config_content": "...",
  "reason": "..."
}
config_content must be the COMPLETE, real content of that config file for this
specific project (not a placeholder) -- reference the actual entry point / build
command you can infer from the file tree and module specs. Never propose whether
to deploy, only how; the decision to actually go live belongs to the user.
"""


def _get_project_tree(app_dir: str) -> list:
    # Deliberately mirrors structure_architect.py's own _get_project_tree()
    # exactly (same logic, same shape) rather than importing a private,
    # underscore-prefixed helper across a module boundary -- this codebase
    # has no shared-util module for this, and neither module treats the
    # other's internals as a public dependency.
    if not os.path.isdir(app_dir):
        return []
    tree = []
    for root, _, files in os.walk(app_dir):
        for fname in files:
            full = os.path.join(root, fname)
            rel = os.path.relpath(full, app_dir)
            tree.append(rel)
    return sorted(tree)


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def run_deploy_config_writer(session_id: str = None, tier: int = None,
                              task_text: str = None, domain: str = None) -> dict:
    app_slug = get_current_app_slug()
    app_dir = os.path.join(APPS_ROOT, app_slug) if app_slug else None
    project_tree = _get_project_tree(app_dir) if app_dir else []
    module_specs = read(KEYS["module_specs"], default={})

    user_prompt = (
        "Current project file tree:\n" + json.dumps(project_tree, indent=2)
        + "\n\nThis cycle's module_specs (language/stack signal):\n"
        + json.dumps(module_specs, indent=2)
    )
    if task_text:
        user_prompt += f"\n\nOriginal task: {task_text}"

    raw = generate_text(SYSTEM_PROMPT, user_prompt, CHAIN, agent_name="Deploy Config Writer",
                         session_id=session_id, tier=tier, domain=domain)
    cleaned = _strip_fences(raw)

    try:
        plan = json.loads(cleaned)
    except json.JSONDecodeError:
        # Fail safe, same spirit as structure_architect.py's own fallback:
        # deploy_agent.py still needs something valid to write, even if
        # the model's output didn't parse. Render.yaml is the most
        # general-purpose of the four options, so it's the safest guess.
        plan = {
            "platform": "render",
            "config_filename": "render.yaml",
            "config_content": "# fallback: deploy config writer output was not valid JSON\n",
            "reason": "fallback: could not parse a real proposal",
        }

    write(DEPLOY_CONFIG_PLAN_KEY, plan)
    emit_event("deploy_config_proposed", session_id, agent="deploy_config_writer",
               payload={"platform": plan.get("platform"), "config_filename": plan.get("config_filename")})
    return plan


if __name__ == "__main__":
    result = run_deploy_config_writer()
    print(json.dumps(result, indent=2))