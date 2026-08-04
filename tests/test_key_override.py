"""
tests/test_key_override.py — Part 5 checklist item 3
Verifies code_writers.py, fixer_pool.py, reviewer.py, security_scanner.py
each honor key_override (None / single string / list) correctly, and that
security_scanner rejects an unknown token_env loudly.

This monkeypatches utils.llm_client.generate_text with a recorder instead
of hitting real providers -- we're testing "which account got called",
not model output quality, so no real API calls are needed or wanted here.

Run with: python -m tests.test_key_override   (from project root)
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import write, KEYS

# --- monkeypatch generate_text everywhere it's already been imported ---
import utils.llm_client as llm_client_module

CALLS = []

def _fake_generate_text(system_prompt, user_content, chain, agent_name=None,
                         session_id=None, tier=None):
    CALLS.append({"agent_name": agent_name, "chain": chain})
    if "findings" in system_prompt.lower():
        return '{"findings": []}'
    if "issues" in system_prompt.lower():
        return '{"issues": [], "summary": "ok"}'
    if "module_name" in system_prompt:
        return '{"mod": {"language": "python", "code": "print(1)"}}'
    return "print('hello')"

llm_client_module.generate_text = _fake_generate_text

# Import agents AFTER the monkeypatch so their `from utils.llm_client import
# generate_text` binding picks up the fake.
from agents import code_writers, fixer_pool, reviewer, security_scanner
code_writers.generate_text = _fake_generate_text
fixer_pool.generate_text = _fake_generate_text
reviewer.generate_text = _fake_generate_text
security_scanner.generate_text = _fake_generate_text


def reset_calls():
    CALLS.clear()


def chain_primary(call):
    """Only chain[0] (the primary) is actually used when it succeeds --
    later chain entries are unreached fallback candidates."""
    step = call["chain"][0]
    return step.get("key_env") or step.get("token_env")


def setup_common_memory():
    write(KEYS["module_specs"], {"modules": [{"name": "mod_a"}, {"name": "mod_b"}]})
    write(KEYS["submitted_code"], {
        "mod_a": {"language": "python", "code": "print('a')"},
        "mod_b": {"language": "python", "code": "print('b')"},
    })
    write(KEYS["review_notes"], {"issues": [], "summary": ""})


# --- code_writers ---
def test_code_writers_none_uses_default_pool():
    setup_common_memory(); reset_calls()
    code_writers.run(key_override=None)
    used = {chain_primary(c) for c in CALLS}
    assert used <= set(code_writers.KEY_ENVS), used
    assert used
    print("PASS code_writers: None -> default pool", used)

def test_code_writers_single_override():
    setup_common_memory(); reset_calls()
    code_writers.run(key_override="SOLO_CEREBRAS_KEY")
    used = {chain_primary(c) for c in CALLS}
    assert used == {"SOLO_CEREBRAS_KEY"}, used
    print("PASS code_writers: single override", used)

def test_code_writers_list_override():
    setup_common_memory(); reset_calls()
    code_writers.run(key_override=["K1", "K2"])
    used = {chain_primary(c) for c in CALLS}
    assert used <= {"K1", "K2"}, used
    print("PASS code_writers: list override", used)


# --- fixer_pool ---
def test_fixer_pool_none_uses_default_pool():
    setup_common_memory(); reset_calls()
    fixer_pool.run_fixer_pool(key_override=None)
    used = {chain_primary(c) for c in CALLS}
    assert used <= set(fixer_pool.CEREBRAS_KEY_ENVS), used
    print("PASS fixer_pool: None -> default pool", used)

def test_fixer_pool_single_override():
    setup_common_memory(); reset_calls()
    fixer_pool.run_fixer_pool(key_override="SOLO_FIXER_KEY")
    used = {chain_primary(c) for c in CALLS}
    assert used == {"SOLO_FIXER_KEY"}, used
    print("PASS fixer_pool: single override", used)

def test_fixer_pool_list_override():
    setup_common_memory(); reset_calls()
    fixer_pool.run_fixer_pool(key_override=["FX1", "FX2"])
    used = {chain_primary(c) for c in CALLS}
    assert used <= {"FX1", "FX2"}, used
    print("PASS fixer_pool: list override", used)


# --- reviewer ---
def test_reviewer_none_uses_default_pool():
    setup_common_memory(); reset_calls()
    reviewer.run_reviewer(key_override=None)
    used = {chain_primary(c) for c in CALLS}
    assert used <= set(reviewer.GROQ_KEY_ENVS), used
    print("PASS reviewer: None -> default pool", used)

def test_reviewer_single_override():
    setup_common_memory(); reset_calls()
    reviewer.run_reviewer(key_override="SOLO_REVIEW_KEY")
    used = {chain_primary(c) for c in CALLS}
    assert used == {"SOLO_REVIEW_KEY"}, used
    print("PASS reviewer: single override", used)

def test_reviewer_list_override():
    setup_common_memory(); reset_calls()
    reviewer.run_reviewer(key_override=["RV1", "RV2"])
    used = {chain_primary(c) for c in CALLS}
    assert used <= {"RV1", "RV2"}, used
    print("PASS reviewer: list override", used)


# --- security_scanner ---
def test_security_scanner_none_uses_default_slots():
    setup_common_memory(); reset_calls()
    security_scanner.run(key_override=None)
    used = {chain_primary(c) for c in CALLS}
    default_tokens = {f"CLOUDFLARE_API_KEY_{n}" for n in security_scanner.CLOUDFLARE_KEY_SLOTS}
    assert used <= default_tokens, used
    print("PASS security_scanner: None -> default slots", used)

def test_security_scanner_single_override():
    setup_common_memory(); reset_calls()
    security_scanner.run(key_override="CLOUDFLARE_API_KEY_4")
    used = {chain_primary(c) for c in CALLS}
    assert used == {"CLOUDFLARE_API_KEY_4"}, used
    print("PASS security_scanner: single override", used)

def test_security_scanner_list_override():
    setup_common_memory(); reset_calls()
    security_scanner.run(key_override=["CLOUDFLARE_API_KEY_5", "CLOUDFLARE_API_KEY_6"])
    used = {chain_primary(c) for c in CALLS}
    assert used <= {"CLOUDFLARE_API_KEY_5", "CLOUDFLARE_API_KEY_6"}, used
    print("PASS security_scanner: list override", used)

def test_security_scanner_unknown_override_raises():
    setup_common_memory(); reset_calls()
    try:
        security_scanner.run(key_override="NOT_A_REAL_TOKEN_ENV")
    except KeyError:
        print("PASS security_scanner: unknown token_env raises KeyError")
        return
    raise AssertionError("FAIL: expected KeyError for unknown token_env")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} key_override tests passed.")