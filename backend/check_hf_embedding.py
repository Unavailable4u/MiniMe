"""
Hugging Face embedding cold-start check.

Mirrors utils/embedding.py's embed_text() EXACTLY (same URL, same payload,
same wait_for_model flag, same timeout) so this reproduces what your app
actually experiences -- not a simplified version of it.

Run from your backend folder (same venv as the app):
    python check_hf_embedding.py

Makes the SAME call twice, back-to-back:
  - Call #1: if the model is currently unloaded on HF's shared inference
    infra, this is the one that pays the cold-start cost. wait_for_model=True
    means HF holds the connection open and waits rather than erroring, so a
    slow-but-successful response here (many seconds, possibly close to a
    minute) is the cold-start tax landing.
  - Call #2: run immediately after #1. If the model is now warm, this
    should come back in well under a second. A big gap between call #1 and
    call #2 is the confirmation.
"""
import os
import time

import requests
from dotenv import load_dotenv

load_dotenv()

EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HF_FEATURE_EXTRACTION_URL = "https://router.huggingface.co/hf-inference/models"
API_KEY = os.getenv("HUGGINGFACE_API_KEY")


def timed_call(label, text):
    if not API_KEY:
        print(f"{label}: SKIPPED — HUGGINGFACE_API_KEY missing from .env")
        return
    url = f"{HF_FEATURE_EXTRACTION_URL}/{EMBEDDING_MODEL}/pipeline/feature-extraction"
    t0 = time.monotonic()
    try:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={"inputs": text, "options": {"wait_for_model": True}},
            timeout=(10, 90),
        )
        elapsed = time.monotonic() - t0
        if r.status_code == 200:
            vec = r.json()
            shape = f"{len(vec)}-dim" if vec and isinstance(vec[0], float) else "unpooled matrix"
            print(f"{label}: {elapsed:.2f}s -> HTTP 200, {shape} vector")
        else:
            print(f"{label}: {elapsed:.2f}s -> HTTP {r.status_code}: {r.text[:300]}")
    except requests.exceptions.ReadTimeout:
        elapsed = time.monotonic() - t0
        print(f"{label}: TIMED OUT after {elapsed:.2f}s waiting for model to load")
    except Exception as e:
        elapsed = time.monotonic() - t0
        print(f"{label}: FAILED after {elapsed:.2f}s -> {e!r}")


print(f"Model: {EMBEDDING_MODEL}")
print(f"Key present: {bool(API_KEY)}")
print()

timed_call("Call #1 (possible cold start)", "hi")
timed_call("Call #2 (should be warm now)", "hi")
