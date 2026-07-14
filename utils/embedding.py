"""
utils/embedding.py — Part 26 §4, split out of utils/llm_client.py.

embed_text() was living in llm_client.py, which means importing it also
imports llm_client's module-level SDK imports (groq, cerebras, openai) --
real dependency weight for something that's just an HTTP POST to
HuggingFace. eo/routing_memory.py avoided that weight by hand-copying
embed_text() instead of importing it, which solved the real problem but
left two copies of the same function to keep in sync.

This module has exactly the dependencies embed_text() actually needs
(os, requests) and nothing else, so both llm_client.py and
routing_memory.py can import the real thing instead of one of them
carrying a duplicate.
"""
import os

import requests

# Model choice is NOT arbitrary: your actual Upstash Vector index
# (checked via idx.info()) reports dimension=384, similarity_function=
# COSINE. sentence-transformers/all-MiniLM-L6-v2 is the standard model
# for that exact pairing -- if you ever recreate the Vector index with a
# different dimension, this model string must change to match, or every
# upsert/query call will fail with a dimension-mismatch error.
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
HF_FEATURE_EXTRACTION_URL = "https://router.huggingface.co/hf-inference/models"


def embed_text(text: str) -> list:
    """Embeds `text` via HuggingFace Inference API, returns a 384-dim
    vector (list[float]) ready for Upstash Vector's upsert()/query().

    Raises RuntimeError if HUGGINGFACE_API_KEY is missing, or the HF
    request fails outright (caller decides how to degrade -- e.g.
    agents/memory_search.py already wraps its embed_text() calls in
    try/except and treats a failure as "no prior context," not a hard
    error)."""
    api_key = os.getenv("HUGGINGFACE_API_KEY")
    if not api_key:
        raise RuntimeError("HUGGINGFACE_API_KEY not set — required for embed_text().")

    url = f"{HF_FEATURE_EXTRACTION_URL}/{EMBEDDING_MODEL}/pipeline/feature-extraction"
    response = requests.post(
        url,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"inputs": text, "options": {"wait_for_model": True}},
        timeout=(10, 90),  # (connect, read) — wait_for_model can mean a real cold-start wait
    )
    response.raise_for_status()
    embedding = response.json()

    # Feature-extraction can return either an already-pooled [dim] vector
    # or an unpooled [seq_len][dim] matrix depending on the model/endpoint
    # version -- mean-pool across tokens if it's the unpooled shape, so
    # callers always get back a flat list[float] regardless of which
    # shape HF happens to serve.
    if embedding and isinstance(embedding[0], list):
        seq_len = len(embedding)
        dim = len(embedding[0])
        embedding = [sum(tok[i] for tok in embedding) / seq_len for i in range(dim)]

    return embedding