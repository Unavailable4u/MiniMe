import json
import os
import sys

from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import KEYS, read_many, write
from utils.llm_client import DROPPABLE_CONTEXT_MARKER, generate_text

load_dotenv()

# FALLBACK_CHAIN: last-resort static chain, used ONLY if
# eo/dynamic_chain.py's build_fallback_chain() below returns nothing at
# all (every registered account excluded/cooling down at once -- should
# be very rare). This used to be the ONLY chain this module ever tried
# (module-level CHAIN, both Groq steps AND the Cerebras step all pinned
# to one hardcoded account each -- CEREBRAS_API_KEY_1 in particular was
# quietly shared with deploy_config_writer.py, dataset_analyst.py,
# output_organizer.py, responder.py, prompt_writer_lean.py, and
# reviewer_fixer_lean.py, so one cooldown on that single account took
# out this agent's Cerebras fallback step along with all of theirs at
# once -- see backend/eo/agent_dependencies.py-adjacent Patch 8 audit).
# "idea_planner" is already tagged in eo/registry.py's
# AGENT_CAPABILITIES (GROQ_API_KEY/_10/_11/_12's natural_roles), so
# build_fallback_chain() below gets a real, live, quota-ranked,
# cooldown-aware chain to build from -- no registry changes needed for
# this agent specifically. Kept here as the literal fallback of last
# resort, same model/key choices as before (see run()'s real call site
# for the fix).
FALLBACK_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "GROQ_API_KEY"},
    # OR-3c (reliability_overhaul_plan.md): Cerebras retired to a paid
    # tier (see OR-2's .env.example note) -- was CEREBRAS_API_KEY_1,
    # "openrouter/free" now (not a pinned model slug -- see
    # utils/llm_client.py's OPENROUTER_BASE_URL comment). "idea_planner"
    # IS tagged in AGENT_CAPABILITIES (GROQ keys), so build_fallback_chain()
    # normally succeeds and this really is the rare last-resort step this
    # docstring describes -- low-traffic, but still real Cerebras spend
    # whenever every registered account is cooling down at once.
    {"provider": "openrouter", "model": "openrouter/free", "key_env": "OPENROUTER_API_KEY_1"},
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
    # Batched into a single MGET instead of 3 sequential round trips --
    # these three keys are unrelated and none is used until after all of
    # them are read anyway.
    _vals = read_many(
        [KEYS["original_idea"], KEYS["latest_report"], KEYS["feature_status"]],
        default=None,
    )
    idea = _vals[KEYS["original_idea"]]
    prior_report = _vals[KEYS["latest_report"]]
    feature_status = _vals[KEYS["feature_status"]] or {}
    user_content = f"Original idea: {idea}"
    user_content += f"\n\nCurrent feature_status: {json.dumps(feature_status)}"
    if prior_report:
        # Phase 7: prior_report is genuinely optional trailing context --
        # SYSTEM_PROMPT above already tells the model "any prior report",
        # explicitly allowing for none. It's also the last thing appended
        # to user_content, same shape as hardware_speccer.py's
        # hw_reference_context -- so on a genuine CONTEXT_LENGTH_EXCEEDED,
        # utils.llm_client's _shrink_prompt_for_retry() can drop exactly
        # this block first (full report JSON can be sizeable across many
        # build cycles) instead of guessing which raw characters are safe
        # to cut from the end of user_content.
        user_content += DROPPABLE_CONTEXT_MARKER + f"Prior cycle report: {json.dumps(prior_report)}"
    else:
        user_content += "\n\nThis is cycle 1. No prior report exists yet."
    # perf audit §4.4 / priority #7: was double-wrapped in call_with_retry
    # on top of generate_text()'s own chain-walk fallback — a real
    # multi-provider outage retried the whole CHAIN up to 4 times with
    # real sleeps (1/2/4/8s) in between, on top of generate_text() already
    # having walked every step in CHAIN once per attempt. generate_text()
    # is the single source of retry/fallback behavior now.
    #
    # Patch 8.2 (key-rotation fix, generalized): deferred import -- see
    # eo/dynamic_chain.py's module docstring for why this can't be a
    # module-level import (eo.registry imports this module at load time;
    # eo.dynamic_chain imports eo.registry at ITS module level, so a
    # top-level import here would close a circular loop). Quota-ranked,
    # cooldown-aware, spread across every account "idea_planner" is
    # tagged for in AGENT_CAPABILITIES -- replaces the old fixed CHAIN
    # that pinned every step (including the one Cerebras step) to one
    # hardcoded account with nothing to fall back to.
    from eo.dynamic_chain import build_fallback_chain
    chain = build_fallback_chain("idea_planner") or FALLBACK_CHAIN

    raw_text = generate_text(SYSTEM_PROMPT, user_content, chain, agent_name="Idea Planner",
                              session_id=session_id, domain=domain)
    # Strip markdown code fences if the model adds them anyway
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        raw_text = raw_text.removeprefix("json")
        raw_text = raw_text.strip()
    plan = json.loads(raw_text)
    write(KEYS["current_plan"], plan)
    return plan
if __name__ == "__main__":
    plan = run()
    print(json.dumps(plan, indent=2))