"""
OR-1 manual check (Phase 3g, reliability_overhaul_plan.md).

Run this once, locally, with a real OPENROUTER_API_KEY set, to answer
the one open question OR-1 needs before any ledger/QUOTA_CONFIG code
gets written: does OpenRouter's chat-completions response carry the
same x-ratelimit-* headers Groq/Cerebras/Mistral/Gemini do (in which
case rate_ledger's existing token-based can_proceed()/record_headroom()
path just works unchanged), or does it send nothing useful (in which
case the ledger needs a request-count-based gating mode for this
provider specifically -- flagged in the plan as a small Phase 2
extension, not something to bolt on inside 3g).

Usage:
    export OPENROUTER_API_KEY_1=sk-or-v1-...
    python3 test_openrouter.py

Mirrors _call_step()'s own header-extraction technique in
utils/llm_client.py exactly (.with_raw_response.create(...) then
.headers), so whatever this prints is what generate_text() will
actually see once OpenRouter is wired into a real chain.
"""
import os
import sys

from openai import OpenAI

KEY_ENV = sys.argv[1] if len(sys.argv) > 1 else "OPENROUTER_API_KEY_1"
MODEL = sys.argv[2] if len(sys.argv) > 2 else "openai/gpt-oss-120b:free"

api_key = os.getenv(KEY_ENV)
if not api_key:
    print(f"Set {KEY_ENV} first (export {KEY_ENV}=sk-or-v1-...).")
    sys.exit(1)

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)

print(f"Calling {MODEL} via OpenRouter ({KEY_ENV})...\n")

raw_response = client.chat.completions.with_raw_response.create(
    model=MODEL,
    messages=[
        {"role": "system", "content": "You are a terse test assistant."},
        {"role": "user", "content": "Reply with exactly one word: pong"},
    ],
    max_tokens=16,
)
headers = getattr(raw_response, "headers", None) or {}
response = raw_response.parse()

print("=== ALL RESPONSE HEADERS ===")
for k, v in sorted(headers.items()):
    print(f"  {k}: {v}")

print("\n=== RATE-LIMIT-SHAPED HEADERS SPECIFICALLY ===")
found_any = False
for k in headers.keys():
    lk = k.lower()
    if "ratelimit" in lk or "rate-limit" in lk or lk.startswith("x-ratelimit"):
        print(f"  {k}: {headers[k]}")
        found_any = True
if not found_any:
    print("  (none found -- OpenRouter did not send any x-ratelimit-* style header on this call)")

print("\n=== RESPONSE BODY (sanity check) ===")
choice = response.choices[0]
print(f"  text: {(choice.message.content or '').strip()!r}")
print(f"  finish_reason: {choice.finish_reason}")
usage = getattr(response, "usage", None)
print(f"  usage: {usage}")

print("\n=== VERDICT ===")
if found_any:
    print("  Headers present -> rate_ledger's existing token-based headroom path")
    print("  should work unchanged for openrouter. Proceed with OR-1's QUOTA_CONFIG")
    print("  entry using the tpm/tpd shape, same as cerebras/groq.")
else:
    print("  No rate-limit headers on this call -> the ledger will need a")
    print("  request-count-based gating mode for openrouter (20 rpm / ~50-1000")
    print("  rpd, per the plan), not the token-based tpm/tpd shape. This is a")
    print("  small rate_ledger.py extension, not something to improvise inside")
    print("  the chain-swap patches -- flag it and we'll scope that separately")
    print("  before OR-3 lands.")
