"""
agents/documentation_agent.py — Documentation Agent (Part 4, agent #14 of
the v5 Master Blueprint).

Provider: Mistral La Plateforme, "mistral-medium-latest" (the blueprint
pins "Mistral Medium 3" -- using Mistral's rolling -latest alias so this
doesn't silently 404 the next time they bump the point version, same
reasoning idea_planner.py's comment gives for its Cerebras model choice).
No fallback specified in the blueprint for this agent.

Mistral's API is OpenAI-compatible, so this reuses the `openai` package
already in requirements.txt (same trick GitHub Models uses in
llm_client.py) rather than adding the `mistralai` SDK as a new dependency.

Runs after file_manager.py: docs describe what actually got written to
disk this cycle, not what was merely planned.

Unlike most agents, this one is allowed to touch the filesystem directly
(README.md only, never code) -- same trust level file_manager.py already
gives itself for README skeleton generation on cycle 1.
"""
import os
import sys
import json
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, read_many, KEYS
from utils.llm_client import generate_text

load_dotenv()

# No fallback specified in the blueprint for this agent -- single-step
# chain. Using the -latest alias, see module docstring. Now routed through
# generate_text() (llm_client.py's new "mistral" provider) instead of a
# hand-rolled client, so this call actually gets usage-logged like every
# other agent -- previously it logged nothing at all.
CHAIN = [
    {"provider": "mistral", "model": "mistral-medium-latest", "key_env": "MISTRAL_API_KEY"},
    # Gemini/Mistral/HF rollout, Patch 5 (§4b): this agent had a single
    # account and zero fallback -- like structure_architect.py, it's
    # called directly with its own CHAIN and never goes through
    # AGENT_CAPABILITIES (see the rollout guide's §1 table), so the tag
    # in eo/registry.py's "documentation_writer" role is decorative and
    # the only real fix is appending steps here. Gemini first (distinct
    # provider lineage, so a Mistral-wide outage doesn't take out both
    # the primary and first fallback at once), then a second Mistral
    # account as the last resort. mistral-medium-latest kept for
    # MISTRAL_API_KEY_5 to match this agent's existing model pin (see
    # module docstring) rather than switching to the mistral-large
    # default other roles use -- no reason to change model choice for a
    # fallback account on the same provider doing the identical job.
    {"provider": "gemini", "model": "gemini-3.6-flash", "key_env": "GEMINI_API_KEY_8"},
    {"provider": "mistral", "model": "mistral-medium-latest", "key_env": "MISTRAL_API_KEY_5"},
    # Quota-reality fix, §11c (2026-07-30): was two fallback accounts
    # (Gemini + one Mistral); MISTRAL_API_KEY_8 (one of the 3
    # newly-provisioned Mistral keys) is the real third account §7 asked
    # for -- Mistral publishes no daily cap, only RPS, so this is
    # genuinely the lowest-leverage of the 16-key wiring work, but it's
    # still real redundancy this agent didn't have before.
    {"provider": "mistral", "model": "mistral-medium-latest", "key_env": "MISTRAL_API_KEY_8"},
]
APPS_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "apps")

SYSTEM_PROMPT = """You are a technical writer. You will be given a summary of
what was built/changed this cycle and the current file tree. Write updated
README content: what the app does, current features (done vs in progress),
how to run it, and a short "recent changes" note for this cycle.
Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly
this shape:
{"readme_markdown": "# Full README content in markdown..."}
"""

def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def run(session_id: str = None, tier: int = None, domain: str = None) -> dict:
    # Batched into a single MGET instead of 4 sequential round trips --
    # these four keys are unrelated and none is used until after all of
    # them are read anyway.
    _vals = read_many(
        [KEYS["original_idea"], KEYS["feature_status"],
         KEYS["latest_report"], KEYS["file_map"]],
        default=None,
    )
    idea = _vals[KEYS["original_idea"]] or ""
    feature_status = _vals[KEYS["feature_status"]] or {}
    report = _vals[KEYS["latest_report"]] or {}
    file_map = _vals[KEYS["file_map"]] or {}
    # Migration Part B (session isolation fix): was read(KEYS["app_slug"], ...),
    # which -- "app_slug" being exempt from memory.bus's namespacing --
    # always reads the raw, UNSCOPED global Redis record, not this run's
    # own session-scoped slug (see api/task_runner.py's _run_tier3_hires()
    # and memory/bus.py's set_app_slug()). That let one session's docs
    # agent read (or silently fall back to None instead of) an entirely
    # unrelated session's app name.
    from memory.bus import get_current_app_slug
    slug = get_current_app_slug()

    user_prompt = json.dumps({
        "idea": idea,
        "feature_status": feature_status,
        "this_cycle_summary": report.get("summary", ""),
        "file_map": file_map,
    }, indent=2)

    # perf audit §4.4 / priority #7: was double-wrapped in call_with_retry
    # on top of generate_text()'s own chain-walk fallback — a real
    # multi-provider outage retried the whole CHAIN up to 4 times with
    # real sleeps (1/2/4/8s) in between, on top of generate_text() already
    # having walked every step in CHAIN once per attempt. generate_text()
    # is the single source of retry/fallback behavior now.
    raw_text = generate_text(SYSTEM_PROMPT, user_prompt, CHAIN, agent_name="Documentation Agent",
                              session_id=session_id, tier=tier, domain=domain)
    doc = json.loads(_strip_fences(raw_text))
    write(KEYS["doc_output"], doc)

    if slug:
        readme_path = os.path.join(APPS_ROOT, slug, "README.md")
        if os.path.isdir(os.path.dirname(readme_path)):
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(doc.get("readme_markdown", ""))

    # Part 0 §0.1: this README is a node like anything else a domain
    # produces. workspace_id reuses the app slug for now (the coding
    # domain doesn't yet have a separate workspace concept the way
    # Notes/Research/Plan will) -- section="coding" distinguishes it from
    # nodes future domains write under their own section name.
    if slug:
        from eo.knowledge_graph import write_node
        write_node(
            workspace_id=slug,
            section="coding",
            node_type="note",
            title="README",
            content=doc.get("readme_markdown", ""),
            created_by="documentation_agent",
            session_id=session_id,
            tier=tier,
        )

    return doc


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))