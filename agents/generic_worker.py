"""
agents/generic_worker.py — v6 migration Part 10. Runs ANY role that
doesn't perform a real action (no file writes, no external API calls),
using that role's stored brief (eo/registry.py's get_role_prompt) and the
memory-bus hand-off contract each execution-order step implies: it reads
whichever earlier stages' outputs it's told to (input_keys), and writes
its own output back under its own role name so a later stage can read it
in turn.

Migration Part 12 §3.4: idea_planner/prompt_writer/test_writer are not in
REAL_ACTION_ROLES (Part 10 §2.1), so they run through this module, which
writes output only to stage_output:{session_id}:{role}. But
code_writers.py (a real-action module, untouched since v5) still reads
its input from the ORIGINAL v5 bus keys (module_specs, current_plan,
etc.) via memory.bus.read(KEYS[...]). Unifying the execution path doesn't
unify the bus convention -- nothing wrote those legacy keys anymore once
prompt_writer moved to generic_worker. LEGACY_BUS_KEY_MAP below bridges
that: for the handful of roles a real-action module still expects a key
from, run() also reads/writes that original key, so code_writers.py etc.
keep working completely unmodified.

Honest caveat (not fully solved by this bridge): stage_output:* keys are
namespaced by session_id; the legacy keys (module_specs, current_plan)
are namespaced by app_slug (memory/bus.py's original design). For a
single task run these usually align in practice, but they're genuinely
two different namespacing dimensions -- a true unification is a bigger
change than this bridge attempts. This map covers coding's specific
early-stage hand-off, which is what's actually needed for coding tasks to
work through the unified pipeline.
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import get_role_prompt, AGENT_CAPABILITIES
from eo.quota_sentinel import get_quota_snapshot
from utils.llm_client import generate_text
from memory.bus import read as bus_read, write as bus_write
# NOTE: `from eo.panel import _best_match` is deliberately NOT imported at
# module level here. eo.registry.py now imports this module (generic_worker)
# at load time so resolve("generic_worker") works, and eo.panel.py imports
# FROM eo.registry (AGENT_CAPABILITIES, get_role_prompt) -- importing
# eo.panel here too would close a circular loop:
#   eo.registry -> agents.generic_worker -> eo.panel -> eo.registry
# Deferring this one import to inside run() (below) breaks the cycle: by
# the time run() is actually CALLED, both modules have finished loading.

PROVIDER_DEFAULT_MODEL = {
    "groq": "llama-3.3-70b-versatile",
    "cerebras": "llama-3.3-70b",
    "mistral": "mistral-large-latest",
    "github": "openai/gpt-4.1-mini",
}

MARKDOWN_INSTRUCTION = (
    "\n\nFormat your answer in Markdown: use fenced code blocks with a "
    "language tag for any code, use tables for tabular data, use headers/"
    "bullet lists to structure longer answers, and use bold/italic "
    "sparingly for emphasis."
)

NEXT_TAG_INSTRUCTION = (
    "\n\nAfter your answer, on its own final line, write exactly one of:\n"
    "NEXT: DONE                 (your part is genuinely complete)\n"
    "NEXT: <role_name>          (this needs another pass from a specific "
    "earlier or later role, name it exactly)\n"
    "Default to NEXT: DONE unless something is genuinely unresolved.\n"
    "IMPORTANT: this NEXT: line must be plain text, NOT inside a markdown "
    "code block or any other formatting, so it can still be parsed "
    "correctly."
)

# Migration Part 12 §3.4 — see module docstring. A role not in this map
# (most non-coding roles) only gets the normal stage_output:* treatment.
LEGACY_BUS_KEY_MAP = {
    "idea_planner": "current_plan",
    "prompt_writer": "module_specs",
    "test_writer": "test_code",
}


def _chain_step_for(agent_key: str) -> dict:
    info = AGENT_CAPABILITIES[agent_key]
    provider = info["provider"]
    step = {"provider": provider, "model": PROVIDER_DEFAULT_MODEL.get(provider, ""), "key_env": agent_key}
    if provider == "cloudflare":
        step = {"provider": provider, "account_id_env": info.get("key_id", agent_key),
                 "token_env": agent_key.replace("ACCOUNT_ID", "API_TOKEN")}
    return step


def parse_next_tag(raw_text: str) -> tuple:
    """
    Migration Part 12 §5: renamed from _parse_next -- made public since
    Part 11 §2 imports it across a module boundary (agents/reviewer.py,
    agents/fixer_pool.py). No logic change from the original _parse_next,
    name only.
    """
    lines = raw_text.strip().splitlines()
    if lines and lines[-1].strip().upper().startswith("NEXT:"):
        tag = lines[-1].split(":", 1)[1].strip()
        body = "\n".join(lines[:-1]).strip()
        return body, (None if tag.upper() == "DONE" else tag)
    return raw_text.strip(), None   # no tag found — treat as done, don't crash on it


def run(role: str, task_text: str, input_keys: list = None, session_id: str = None,
        key_override=None) -> dict:
    """
    role: the exact role name the Panel/registry assigned (e.g.
        "brainstormer", "fact_checker") — also used as this call's own
        output key on the memory bus, so a later stage can read it.
    input_keys: the specific earlier stages' output this role should
        read, per this task's execution_order (eo/router.py's
        role_names[:idx] slice) — NOT the whole history, just what
        precedes this role in the resolved order.
    """
    brief = get_role_prompt(role)
    input_keys = input_keys or []
    context_parts = [f"TASK: {task_text}"]
    for k in input_keys:
        prior = bus_read(f"stage_output:{session_id}:{k}", default=None)
        if prior is None and k in LEGACY_BUS_KEY_MAP:
            # Migration Part 12 §3.4: fall back to the original v5 bus key
            # if this earlier role never wrote a stage_output entry (i.e.
            # it's a real-action-adjacent role like idea_planner/
            # prompt_writer whose actual consumer is a real-action module,
            # not another generic_worker step). app_slug-namespaced, not
            # session-namespaced -- see module docstring's caveat.
            prior = bus_read(LEGACY_BUS_KEY_MAP[k], default=None)
        if prior:
            context_parts.append(f"--- Output from '{k}' ---\n{prior}")
    context = "\n\n".join(context_parts)

    if key_override:
        agent_key = key_override if isinstance(key_override, str) else key_override[0]
    else:
        from eo.panel import _best_match   # deferred — see module-level note above
        agent_key = _best_match(role, get_quota_snapshot())

    chain = [_chain_step_for(agent_key)] if agent_key else []
    raw = generate_text(
        system_prompt=(brief or "") + MARKDOWN_INSTRUCTION + NEXT_TAG_INSTRUCTION,
        user_content=context,
        chain=chain,
        agent_name=f"generic:{role}",
    )
    body, next_destination = parse_next_tag(raw)
    if session_id:
        bus_write(f"stage_output:{session_id}:{role}", body)
    if role in LEGACY_BUS_KEY_MAP:
        # Migration Part 12 §3.4: also feed the original v5 bus key so
        # code_writers.py etc. keep reading real input, unmodified.
        bus_write(LEGACY_BUS_KEY_MAP[role], body)
    return {"role": role, "text": body, "next_destination": next_destination}