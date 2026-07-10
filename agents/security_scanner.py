"""
agents/security_scanner.py — Security/Dependency Scanner Pool (Part 4,
agent #12 of the v5 Master Blueprint).

Provider: Cloudflare Workers AI, 5 parallel workers, keys #4-#8
(CLOUDFLARE_ACCOUNT_ID_4..8 / CLOUDFLARE_API_KEY_4..8) -- same account/key
pattern as reviewer.py and fixer_pool.py's Cloudflare fallbacks, but used
here as the PRIMARY provider for a full 5-worker pool, one worker per
module (round-robin if there are more modules than workers).

Runs after structure_architect.py, before file_manager.py: scan the final
code right before it's written to disk, not before Fixer Pool has had a
chance to patch anything (scanning pre-fix code would just re-flag issues
the Fixer already resolved).

Output, written to KEYS["security_scan_results"]:
{
  "module_name": {"findings": [{"severity": "...", "description": "..."}]}
}
"""
import os
import sys
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, KEYS
from relay.emitter import emit_event
from utils.llm_client import generate_text
from eo.registry import AGENT_CAPABILITIES
from eo.quota_sentinel import get_quota_snapshot

load_dotenv()

CLOUDFLARE_MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"

# Migration Part 9 §2.1: replaces the old static CLOUDFLARE_KEY_SLOTS /
# RESERVE_SLOT_NUMS / _slot_descriptors() with a registry-driven pool,
# ranked by live quota every run — same fairness-rotation pattern as
# code_writers.py/reviewer.py. AGENT_CAPABILITIES's dict keys ARE the
# account_id_env strings for cloudflare entries (Part 8 §2's key_id
# convention — that's what log_usage() actually keys cloudflare usage
# under), so the pool below is a list of account_id_env strings, not
# token_env. generate_text()'s cloudflare chain entries need BOTH halves
# though, so _token_env_for() derives the paired token_env from each
# account_id_env's naming pattern, and _slot_for() bundles both into the
# same {label, account_id_env, token_env} descriptor shape the rest of
# this module already expects.
ROLE_TAG = "security_reviewer"


def _eligible_pool() -> list:
    return [key for key, info in AGENT_CAPABILITIES.items() if ROLE_TAG in info.get("natural_roles", [])]


def _token_env_for(account_id_env: str) -> str:
    """Base slots: CLOUDFLARE_ACCOUNT_ID_N -> CLOUDFLARE_API_KEY_N.
    Reserve slots: CF_SCANNER_RESERVE_N_ACCOUNT_ID -> CF_SCANNER_RESERVE_N_API_TOKEN.
    The two families use genuinely different naming patterns, so this
    can't be a single string substitution rule — same reasoning
    _resolve_override_slots() below already relied on pre-Part-9."""
    if account_id_env.startswith("CLOUDFLARE_ACCOUNT_ID_"):
        n = account_id_env.rsplit("_", 1)[-1]
        return f"CLOUDFLARE_API_KEY_{n}"
    if account_id_env.startswith("CF_SCANNER_RESERVE_") and account_id_env.endswith("_ACCOUNT_ID"):
        n = account_id_env[len("CF_SCANNER_RESERVE_"):-len("_ACCOUNT_ID")]
        return f"CF_SCANNER_RESERVE_{n}_API_TOKEN"
    raise ValueError(f"Don't know how to derive a token_env for account_id_env {account_id_env!r} "
                     f"— add its naming pattern to _token_env_for().")


def _slot_for(account_id_env: str) -> dict:
    return {"label": account_id_env, "account_id_env": account_id_env,
            "token_env": _token_env_for(account_id_env)}


def _select_workers(worker_count: int, key_override=None) -> list:
    """Panel-driven hires (key_override, a token_env or list of them)
    always win outright, resolved via _resolve_override_slots() below.
    Otherwise ranks the full eligible pool (base + reserve) by today's
    live usage — keyed by account_id_env, matching get_quota_snapshot()'s
    own keys — and takes the `worker_count` least-used accounts."""
    if key_override:
        return _resolve_override_slots(key_override)
    pool = _eligible_pool()
    if not pool:
        raise RuntimeError("security_scanner: no accounts tagged 'security_reviewer' in AGENT_CAPABILITIES.")
    snapshot = get_quota_snapshot()
    ranked = sorted(pool, key=lambda k: (snapshot.get(k) or {}).get("pct") or 0.0)
    return [_slot_for(k) for k in ranked[:worker_count]]

SYSTEM_PROMPT = """You are a security auditor. You will be given one code
module. List any security issues: injection risks, hardcoded secrets,
unsafe deserialization, missing input validation, unsafe dependency usage,
path traversal, or similar. Be specific. If there are none, return an
empty findings list -- do not invent issues to have something to say.
Respond with ONLY valid JSON, no markdown fences, no preamble, in exactly
this shape:
{
  "findings": [{"severity": "critical|moderate|minor", "description": "..."}]
}
"""


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return text.strip()


def _scan_one(module_name: str, code: str, slot: dict, session_id: str = None, path: str = None,
               domain: str = None) -> tuple:
    """
    Stage 6 step 5: fires agent_start/agent_done labeled scanner_{slot}.
    `slot` is a descriptor dict (see _slot_descriptors) rather than a bare
    int, since Migration Part 3 step 4.3 added reserve slots that resolve
    to a different pair of env var names than the base 4-8 slots. The
    label still identifies which account did the work, and slots get
    reused round-robin when there are more modules than slots, same
    reasoning as code_writers.py's key-slot labeling.

    NOTE: the old hand-rolled call used Cloudflare's json_object response
    mode as a light steering hint (not a strict schema, unlike
    fixer_pool.py's old json_schema mode -- there's nothing lost here that
    wasn't already just "asking nicely" via the prompt). generate_text()
    has no equivalent parameter, so this now relies purely on
    SYSTEM_PROMPT's instructions plus the broad except below, same
    tolerance as before.
    """
    label = slot["label"]
    agent_name = f"scanner_{label}"
    emit_event("agent_start", session_id=session_id, agent=agent_name, path=path,
               payload={"label": f"Scanner {label} — {module_name}"})
    started = time.monotonic()

    chain = [
        {"provider": "cloudflare", "model": CLOUDFLARE_MODEL,
         "account_id_env": slot["account_id_env"], "token_env": slot["token_env"]},
    ]

    try:
        raw_text = generate_text(
            SYSTEM_PROMPT,
            json.dumps({"module": module_name, "code": code[:6000]}),
            chain,
            agent_name=agent_name,
            session_id=session_id,
            path=path,  # Migration Part 27 §1: generate_text() now accepts `path` for real
            domain=domain,  # Migration Part 2 §2.6: cost-tracking gap
        )
        result = json.loads(_strip_fences(raw_text))
    except Exception as exc:
        result = {"findings": [], "error": str(exc)}

    duration_ms = int((time.monotonic() - started) * 1000)
    findings = result.get("findings", [])
    if result.get("error"):
        summary = f"scan failed: {result['error']}"
    elif findings:
        summary = f"{len(findings)} finding(s)"
    else:
        summary = "no findings"
    emit_event("agent_done", session_id=session_id, agent=agent_name, path=path,
               payload={"summary": summary, "duration_ms": duration_ms})
    return module_name, result


def _resolve_override_slots(key_override) -> list:
    """
    Migration Part 5 §2.3 — security_scanner.py's slots are
    {account_id_env, token_env} PAIRS, not bare key_env strings like
    code_writers.py/reviewer.py/fixer_pool.py's worker keys. A hiring
    decision's agent_key (e.g. "CLOUDFLARE_API_KEY_4") only names the
    token half, so it can't be turned directly into a slot the way the
    other three modules do.

    Instead of guessing the paired account_id_env by string-munging the
    token name (the reserve slots use a different naming scheme --
    CF_SCANNER_RESERVE_N_ACCOUNT_ID/_API_TOKEN -- than the base slots'
    CLOUDFLARE_ACCOUNT_ID_N/_API_KEY_N, so a single substitution rule
    would silently break on one of the two families), this looks the
    given token_env(s) up against the actual known slot descriptors
    (base + reserve, searched regardless of `expanded`, since a Panel
    hire that explicitly names a reserve key should be honored even if
    the mode-based ceiling wouldn't otherwise unlock it) and returns the
    matching full descriptors.

    Raises KeyError, loudly, if a given account_id_env doesn't match any
    known slot -- silently dropping to the default pool would mean the
    Panel's specific account choice was ignored without anyone noticing.

    Bug fix (Part 9 §2.1 follow-up): this used to match key_override
    against each slot's token_env. That was correct under the OLD static
    CLOUDFLARE_KEY_SLOTS design, but Part 9 §2.1 switched AGENT_CAPABILITIES
    (and therefore every hiring decision's agent_key, i.e. what actually
    arrives here as key_override) to be keyed by account_id_env for
    cloudflare entries -- see this module's top-of-file docstring and
    _eligible_pool(). This function was never updated to match, so a
    perfectly valid override like 'CLOUDFLARE_ACCOUNT_ID_4' was checked
    against token_env values it could never match, raising KeyError on
    every single Panel-directed hire for this role. Matching on
    account_id_env instead makes this consistent with how code_writers.py/
    reviewer.py/fixer_pool.py already resolve their own key_overrides
    (directly against AGENT_CAPABILITIES keys).
    """
    account_id_envs = key_override if isinstance(key_override, list) else [key_override]
    all_slots = [_slot_for(k) for k in _eligible_pool()]  # base + reserve, always
    by_account = {slot["account_id_env"]: slot for slot in all_slots}
    resolved = []
    for account_id_env in account_id_envs:
        slot = by_account.get(account_id_env)
        if slot is None:
            raise KeyError(
                f"key_override references '{account_id_env}', which doesn't match "
                f"any known Cloudflare slot's account_id_env in "
                f"security_scanner._eligible_pool(). Known account_id_envs: "
                f"{sorted(by_account.keys())}"
            )
        resolved.append(slot)
    return resolved


def run(session_id: str = None, path: str = None, expanded: bool = False,
        key_override=None, domain: str = None) -> dict:
    """
    key_override: None (default) -> today's exact behavior, picks slots
        via _slot_descriptors(expanded).
    key_override: a single key_env (token_env) string -> use ONLY that
        Cloudflare account/token pair for every module in this call.
    key_override: a list of key_env (token_env) strings -> use exactly
        those account/token pairs as the parallel scanner pool.
    """
    submitted_code = read(KEYS["fixed_code"], default=None) or read(KEYS["submitted_code"], default={})
    if not submitted_code:
        write(KEYS["security_scan_results"], {})
        return {}

    modules = list(submitted_code.items())
    worker_count = 8 if expanded else 5
    slots = _select_workers(worker_count, key_override)
    results = {}
    with ThreadPoolExecutor(max_workers=len(slots)) as executor:
        futures = {}
        for i, (name, data) in enumerate(modules):
            code = data.get("code", "") if isinstance(data, dict) else str(data)
            slot = slots[i % len(slots)]
            futures[executor.submit(_scan_one, name, code, slot, session_id=session_id, path=path,
                                     domain=domain)] = name
        for future in as_completed(futures):
            name, result = future.result()
            results[name] = result

    write(KEYS["security_scan_results"], results)
    return results


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))