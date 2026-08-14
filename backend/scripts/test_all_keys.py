"""
scripts/test_all_keys.py — pings every LLM-provider key configured in the
environment with one small request each, and prints a pass/fail table.

Run from backend/, with your venv active and .env already loaded:

    python scripts/test_all_keys.py

Optional filters:

    python scripts/test_all_keys.py --provider gemini
    python scripts/test_all_keys.py --key GEMINI_API_KEY_12

What it does NOT do: touch the app's own quota tracking, cooldowns, or
memory bus. It talks to each provider directly with its raw SDK/REST call,
completely independent of utils/llm_client.py, so a broken key shows up
here even if the app's own fallback chain would currently skip past it.

Key discovery: scans os.environ for every var name matching the naming
patterns already used in backend/.env.example (GROQ_*, CEREBRAS_*,
CLOUDFLARE_ACCOUNT_ID_*/CLOUDFLARE_API_KEY_*, GEMINI_*, MISTRAL_*,
HUGGINGFACE_*, CF_SCANNER_RESERVE_*, EO_INSPECTOR_GROQ_KEY_*,
EO_PANEL_*). Add a pattern to KEY_PATTERNS below if you introduce a new
provider or naming scheme later — nothing here needs to know your actual
key values, just their env-var names.
"""

import os
import re
import sys
import time
import argparse

# Load .env the same way the rest of the backend does, if python-dotenv
# is available. Harmless no-op if you already export vars another way.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# 1. Discover every key-holding env var, grouped by provider.
# ---------------------------------------------------------------------------

# Cloudflare is (account_id, token) pairs, not a single key — handled
# separately below. Every other provider here is a single bearer key.
SIMPLE_KEY_PATTERNS = {
    "groq": re.compile(r"^(GROQ_API_KEY(_\d+)?|GROQ_RESERVE_\d+|SGA_GROQ_\d+|EO_INSPECTOR_GROQ_KEY_\d+)$"),
    "cerebras": re.compile(r"^(CEREBRAS_API_KEY_\d+|CEREBRAS_RESERVE_\d+|EO_PANEL_CEREBRAS_KEY)$"),
    "gemini": re.compile(r"^GEMINI_API_KEY_\d+$"),
    "mistral": re.compile(r"^MISTRAL_API_KEY(_\d+)?$"),
    "huggingface": re.compile(r"^HUGGINGFACE_API_KEY(_\d+)?$"),
}

CF_ACCOUNT_PATTERN = re.compile(r"^CLOUDFLARE_ACCOUNT_ID_(\d+)$")
CF_TOKEN_TEMPLATE = "CLOUDFLARE_API_KEY_{n}"
CF_SCANNER_ACCOUNT_PATTERN = re.compile(r"^CF_SCANNER_RESERVE_(\d+)_ACCOUNT_ID$")
CF_SCANNER_TOKEN_TEMPLATE = "CF_SCANNER_RESERVE_{n}_API_TOKEN"
EO_PANEL_CF_ACCOUNT = "EO_PANEL_CLOUDFLARE_ACCOUNT_ID"
EO_PANEL_CF_TOKEN = "EO_PANEL_CLOUDFLARE_API_TOKEN"

PROVIDER_DEFAULT_MODEL = {
    # llama-3.3-70b-versatile decommissioned by Groq; migrated to
    # openai/gpt-oss-120b. Same single-value pick and reasoning as
    # agents/generic_worker.py's own PROVIDER_DEFAULT_MODEL -- this dict
    # only has room for one model per provider (each test_*_key() call
    # below fires a single request), so gpt-oss-120b was picked as the
    # closer capability match of the two suggested replacements.
    "groq": "openai/gpt-oss-120b",
    "cerebras": "gpt-oss-120b",
    "gemini": "gemini-3.1-flash-lite",
    "mistral": "mistral-medium-latest",
    "huggingface": "openai/gpt-oss-120b:fastest",
    "cloudflare": "@cf/meta/llama-3.3-70b-instruct-fp8-fast",
}

MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
HF_ROUTER_BASE_URL = "https://router.huggingface.co/v1"

TEST_PROMPT = "Reply with exactly one word: pong"
REQUEST_TIMEOUT = 20  # seconds — small prompt, should be fast if the key is healthy


def discover_keys():
    """Returns a list of (provider, key_env_name, extra) tuples to test.
    extra is None for simple single-key providers, or (account_id_env,
    token_env) for cloudflare-shaped ones."""
    found = []

    for provider, pattern in SIMPLE_KEY_PATTERNS.items():
        for var in os.environ:
            if pattern.match(var) and os.environ.get(var):
                found.append((provider, var, None))

    # Cloudflare production pool
    for var in os.environ:
        m = CF_ACCOUNT_PATTERN.match(var)
        if m and os.environ.get(var):
            token_env = CF_TOKEN_TEMPLATE.format(n=m.group(1))
            if os.environ.get(token_env):
                found.append(("cloudflare", var, token_env))

    # Cloudflare security-scanner reserve pool
    for var in os.environ:
        m = CF_SCANNER_ACCOUNT_PATTERN.match(var)
        if m and os.environ.get(var):
            token_env = CF_SCANNER_TOKEN_TEMPLATE.format(n=m.group(1))
            if os.environ.get(token_env):
                found.append(("cloudflare", var, token_env))

    # EO Panel's dedicated Cloudflare account
    if os.environ.get(EO_PANEL_CF_ACCOUNT) and os.environ.get(EO_PANEL_CF_TOKEN):
        found.append(("cloudflare", EO_PANEL_CF_ACCOUNT, EO_PANEL_CF_TOKEN))

    return sorted(set(found), key=lambda t: (t[0], t[1]))


# ---------------------------------------------------------------------------
# 2. One test call per provider shape.
# ---------------------------------------------------------------------------

def test_openai_shaped(key_env, base_url=None, model=None, sdk="openai"):
    """Covers groq, cerebras, mistral, gemini, huggingface — all either
    the openai SDK pointed at a base_url, or (for groq/cerebras) their
    own SDK with the same chat.completions.create shape."""
    key = os.environ[key_env]
    try:
        if sdk == "groq":
            from groq import Groq
            client = Groq(api_key=key, timeout=REQUEST_TIMEOUT)
        elif sdk == "cerebras":
            from cerebras.cloud.sdk import Cerebras
            client = Cerebras(api_key=key, timeout=REQUEST_TIMEOUT)
        else:
            from openai import OpenAI
            kwargs = {"api_key": key, "timeout": REQUEST_TIMEOUT}
            if base_url:
                kwargs["base_url"] = base_url
            client = OpenAI(**kwargs)

        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a health-check probe."},
                {"role": "user", "content": TEST_PROMPT},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
        return True, text[:60]
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def test_cloudflare(account_id_env, token_env):
    import requests
    account_id = os.environ[account_id_env]
    token = os.environ[token_env]
    model = PROVIDER_DEFAULT_MODEL["cloudflare"]
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{model}"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"messages": [
                {"role": "system", "content": "You are a health-check probe."},
                {"role": "user", "content": TEST_PROMPT},
            ]},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", True) and data.get("errors"):
            return False, str(data["errors"])[:120]
        text = str(data.get("result", {}).get("response", "")).strip()
        return True, text[:60]
    except Exception as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def run_test(provider, key_env, extra):
    if provider == "groq":
        return test_openai_shaped(key_env, model=PROVIDER_DEFAULT_MODEL["groq"], sdk="groq")
    if provider == "cerebras":
        return test_openai_shaped(key_env, model=PROVIDER_DEFAULT_MODEL["cerebras"], sdk="cerebras")
    if provider == "gemini":
        return test_openai_shaped(key_env, base_url=GEMINI_BASE_URL,
                                   model=PROVIDER_DEFAULT_MODEL["gemini"], sdk="openai")
    if provider == "mistral":
        return test_openai_shaped(key_env, base_url=MISTRAL_BASE_URL,
                                   model=PROVIDER_DEFAULT_MODEL["mistral"], sdk="openai")
    if provider == "huggingface":
        return test_openai_shaped(key_env, base_url=HF_ROUTER_BASE_URL,
                                   model=PROVIDER_DEFAULT_MODEL["huggingface"], sdk="openai")
    if provider == "cloudflare":
        return test_cloudflare(key_env, extra)
    return False, "unknown provider"


# ---------------------------------------------------------------------------
# 3. CLI + report.
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ping every configured LLM key.")
    parser.add_argument("--provider", help="Only test this provider (groq/cerebras/gemini/mistral/huggingface/cloudflare)")
    parser.add_argument("--key", help="Only test this one key_env / account_id_env name")
    args = parser.parse_args()

    targets = discover_keys()
    if args.provider:
        targets = [t for t in targets if t[0] == args.provider.lower()]
    if args.key:
        targets = [t for t in targets if t[1] == args.key]

    if not targets:
        print("No matching keys found in the environment. Is .env loaded?")
        sys.exit(1)

    print(f"Testing {len(targets)} key(s)...\n")
    results = []
    for provider, key_env, extra in targets:
        label = key_env if extra is None else f"{key_env}/{extra}"
        sys.stdout.write(f"  {provider:12} {label:35} ... ")
        sys.stdout.flush()
        start = time.monotonic()
        ok, detail = run_test(provider, key_env, extra)
        elapsed_ms = int((time.monotonic() - start) * 1000)
        status = "OK" if ok else "FAIL"
        print(f"{status} ({elapsed_ms}ms) — {detail}")
        results.append((provider, label, ok, detail))

    failed = [r for r in results if not r[2]]
    print(f"\n{len(results) - len(failed)}/{len(results)} keys OK.")
    if failed:
        print("\nFailed keys:")
        for provider, label, _, detail in failed:
            print(f"  [{provider}] {label} — {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main()
