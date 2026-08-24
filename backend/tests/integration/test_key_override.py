"""
tests/integration/test_key_override.py — Part 5 checklist item 3
Verifies code_writers.py, fixer_pool.py, reviewer.py, security_scanner.py
each honor key_override (None / single string / list) correctly, and that
security_scanner rejects an unknown token_env loudly.

Moved from tests/test_key_override.py (B1 reorg) and rewritten against
Migration Part 9 §2/§2.1, which replaced each module's static KEY_ENVS /
GROQ_KEY_ENVS / CLOUDFLARE_KEY_SLOTS pool constants with a registry-driven
pool (eo/worker_pool.py + eo/registry.py's AGENT_CAPABILITIES, filtered by
each module's ROLE_TAG) — those old constants no longer exist on the
modules, so the original assertions raised AttributeError under current
code. This version reads the same pool through each module's own
resolution path instead of a dead constant:

  - code_writers: no longer has its own eligible-pool helper (it delegates
    entirely to eo.worker_pool._select_workers), so we import that
    module's _eligible_pool("implementer") directly.
  - fixer_pool: still has its own real static list, unchanged in shape --
    OPENROUTER_KEY_ENVS (this module was never moved onto the
    registry-driven pool). OR-3a (reliability_overhaul_plan.md) renamed
    this from CEREBRAS_KEY_ENVS when fixer_pool.py's provider migrated;
    the assertion below is updated to match the current attribute name
    (it was raising AttributeError against the old one).
  - reviewer: still has its own private _eligible_pool() (ROLE_TAG =
    "verifier"), just called instead of reading the old GROQ_KEY_ENVS.
  - security_scanner: its pool is {account_id_env, token_env} slot pairs,
    not bare key strings, so the eligible set for comparison is built by
    mapping its own _eligible_pool() through its own _token_env_for().
    Its key_override also now takes an account_id_env string (e.g.
    "CLOUDFLARE_ACCOUNT_ID_4"), NOT the token_env -- _resolve_override_slots()
    used to match against token_env, but its own docstring documents that
    as a Part 9 §2.1 bug that's since been fixed to match on
    account_id_env, consistent with the other three modules matching
    key_override directly against AGENT_CAPABILITIES keys. The ORIGINAL
    version of this test used token_env-style override values
    ("CLOUDFLARE_API_KEY_4"), which would raise KeyError under current
    code -- fixed below.

Also converted from permanent module-level monkeypatching (which never
got undone and could leak a fake generate_text into any test file
collected afterward in the same pytest session) to conftest.py's `mock_llm`
fixture, which patches through pytest's monkeypatch and is undone
automatically after each test.

Run standalone:
    python -m pytest tests/integration/test_key_override.py -v
"""
from agents import code_writers, fixer_pool, reviewer, security_scanner
from eo.worker_pool import _eligible_pool as _wp_eligible_pool
from memory.bus import KEYS, write


def _fake_generate_text(system_prompt, user_content, chain, agent_name=None,
                         session_id=None, **kwargs):
    """Same response heuristic the original file used -- just enough
    per-agent JSON shape for each module's own parser to accept without
    error. We only care WHICH account got called, not response quality."""
    if "findings" in system_prompt.lower():
        return '{"findings": []}'
    if "issues" in system_prompt.lower():
        return '{"issues": [], "summary": "ok"}'
    if "module_name" in system_prompt:
        return '{"mod": {"language": "python", "code": "print(1)"}}'
    return "print('hello')"


def chain_primary(call):
    """Only chain[0] (the primary) is actually used when it succeeds --
    later chain entries are unreached fallback candidates. generate_text
    is always called positionally as (system_prompt, user_content, chain,
    ...) by all four modules under test, so chain is call.args[2]."""
    chain = call.args[2]
    step = chain[0]
    return step.get("key_env") or step.get("token_env")


def setup_common_memory():
    write(KEYS["module_specs"], {"modules": [{"name": "mod_a"}, {"name": "mod_b"}]})
    write(KEYS["submitted_code"], {
        "mod_a": {"language": "python", "code": "print('a')"},
        "mod_b": {"language": "python", "code": "print('b')"},
    })
    write(KEYS["review_notes"], {"issues": [], "summary": ""})


def _calls(mock_llm):
    return [c for c in mock_llm.mock.call_args_list]


# --- code_writers ---
def test_code_writers_none_uses_default_pool(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    code_writers.run(key_override=None)
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used <= set(_wp_eligible_pool("implementer")), used
    assert used

def test_code_writers_single_override(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    code_writers.run(key_override="SOLO_CEREBRAS_KEY")
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used == {"SOLO_CEREBRAS_KEY"}, used

def test_code_writers_list_override(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    code_writers.run(key_override=["K1", "K2"])
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used <= {"K1", "K2"}, used


# --- fixer_pool ---
def test_fixer_pool_none_uses_default_pool(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    fixer_pool.run_fixer_pool(key_override=None)
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used <= set(fixer_pool.OPENROUTER_KEY_ENVS), used

def test_fixer_pool_single_override(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    fixer_pool.run_fixer_pool(key_override="SOLO_FIXER_KEY")
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used == {"SOLO_FIXER_KEY"}, used

def test_fixer_pool_list_override(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    fixer_pool.run_fixer_pool(key_override=["FX1", "FX2"])
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used <= {"FX1", "FX2"}, used


# --- reviewer ---
def test_reviewer_none_uses_default_pool(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    reviewer.run_reviewer(key_override=None)
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used <= set(reviewer._eligible_pool()), used

def test_reviewer_single_override(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    reviewer.run_reviewer(key_override="SOLO_REVIEW_KEY")
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used == {"SOLO_REVIEW_KEY"}, used

def test_reviewer_list_override(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    reviewer.run_reviewer(key_override=["RV1", "RV2"])
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used <= {"RV1", "RV2"}, used


# --- security_scanner ---
# B2: security_scanner.run() now runs a real static scan (Gitleaks/Semgrep,
# agents/static_scan.py) before ever calling the LLM, and skips the LLM
# call entirely for a module the tools found clean (see security_scanner's
# _scan_one()). mock_static_scan stands in for that step here so each of
# these tests' modules has something to summarize -- without it, `used`
# would be empty and the *_single_override strict-equality assertion below
# would fail even though key-selection itself is working correctly.
_ONE_TOOL_FINDING = [{"severity": "critical", "description": "hardcoded secret", "source": "gitleaks"}]


def test_security_scanner_none_uses_default_slots(mock_static_scan, mock_llm):
    mock_static_scan.set_findings(_ONE_TOOL_FINDING)
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    security_scanner.run(key_override=None)
    used = {chain_primary(c) for c in _calls(mock_llm)}
    default_tokens = {security_scanner._token_env_for(k) for k in security_scanner._eligible_pool()}
    assert used <= default_tokens, used

def test_security_scanner_single_override(mock_static_scan, mock_llm):
    mock_static_scan.set_findings(_ONE_TOOL_FINDING)
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    # override is an account_id_env; the resulting chain call still uses
    # that slot's paired token_env (CLOUDFLARE_API_KEY_4) -- see module
    # docstring above.
    security_scanner.run(key_override="CLOUDFLARE_ACCOUNT_ID_4")
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used == {"CLOUDFLARE_API_KEY_4"}, used

def test_security_scanner_list_override(mock_static_scan, mock_llm):
    mock_static_scan.set_findings(_ONE_TOOL_FINDING)
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    security_scanner.run(key_override=["CLOUDFLARE_ACCOUNT_ID_5", "CLOUDFLARE_ACCOUNT_ID_6"])
    used = {chain_primary(c) for c in _calls(mock_llm)}
    assert used <= {"CLOUDFLARE_API_KEY_5", "CLOUDFLARE_API_KEY_6"}, used

def test_security_scanner_unknown_override_raises(mock_llm):
    mock_llm.mock.side_effect = _fake_generate_text
    setup_common_memory()
    try:
        security_scanner.run(key_override="NOT_A_REAL_ACCOUNT_ID")
    except KeyError:
        return
    raise AssertionError("expected KeyError for unknown account_id_env")
