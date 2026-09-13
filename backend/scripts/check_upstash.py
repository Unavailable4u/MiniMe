"""
Standalone Upstash REST latency check.

Run from your backend folder (same venv as the app):
    python check_upstash.py

Times a single GET against Redis and Vector's REST endpoints, with an
explicit 15s timeout on THIS script (unlike the app's own client, which
has none) -- so if either one is paused/hanging, you'll get a clean
timeout message instead of waiting forever.
"""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("UPSTASH_REDIS_REST_URL")
REDIS_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN")
VECTOR_URL = os.getenv("UPSTASH_VECTOR_REST_URL")
VECTOR_TOKEN = os.getenv("UPSTASH_VECTOR_REST_TOKEN")

print("Loaded from .env:")
print("  UPSTASH_REDIS_REST_URL  =", REDIS_URL)
print("  UPSTASH_VECTOR_REST_URL =", VECTOR_URL)
print()


def timed_get(label, url, token, path):
    if not url or not token:
        print(f"{label}: SKIPPED — URL or token missing from .env")
        return
    t0 = time.monotonic()
    try:
        r = requests.get(
            f"{url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        elapsed = time.monotonic() - t0
        print(f"{label}: {elapsed:.2f}s -> HTTP {r.status_code}: {r.text[:300]}")
    except requests.exceptions.Timeout:
        elapsed = time.monotonic() - t0
        print(f"{label}: TIMED OUT after {elapsed:.2f}s — likely paused or unreachable")
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"{label}: FAILED after {elapsed:.2f}s -> {e!r}")


timed_get("Redis  (GET /get/foo)", REDIS_URL, REDIS_TOKEN, "/get/foo")
timed_get("Vector (GET /info)", VECTOR_URL, VECTOR_TOKEN, "/info")
