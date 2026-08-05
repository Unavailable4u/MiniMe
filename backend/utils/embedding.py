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

# Gemini/Mistral/HF rollout, Patch 7 (§4b/§5 of the rollout guide): the
# reserve accounts held back specifically for this -- embed_text() itself
# stays a single-account, single-attempt call (see key_env param below),
# so its 6 OTHER callers (agents/overlapping_checker.py,
# agents/source_quality_flagger.py, eo/knowledge_graph.py,
# eo/semantic_cache.py, eo/routing_memory.py's own hand-rolled _embed(),
# diagnose_mode_a.py) are completely unaffected by this patch and keep
# today's single-account behavior. Only embed_text_with_fallback() below
# walks this list -- and only agents/memory_search.py and
# agents/duplication_checker.py were repointed at it (Patch 7's actual
# scope per the guide's §4b table). Order matters: HUGGINGFACE_API_KEY
# first since it's the existing account with prior quota history: _6/_7
# only get hit once that one's exhausted or down.
# Quota-reality fix, §11d (2026-07-30): added the 3 newly-provisioned
# HuggingFace keys (_8/_9/_10) here, alongside _6/_7 -- this is where
# $0.10/month/account actually stretches (embeddings are far cheaper per
# call than chat completions). Deliberately NOT added to
# eo/registry.py's AGENT_CAPABILITIES -- that's the chat/extraction pool
# (_2/3/4/5), a different job with a much worse cost-per-call ratio (§5).
HF_EMBEDDING_KEY_ENVS = [
    "HUGGINGFACE_API_KEY", "HUGGINGFACE_API_KEY_6", "HUGGINGFACE_API_KEY_7",
    "HUGGINGFACE_API_KEY_8", "HUGGINGFACE_API_KEY_9", "HUGGINGFACE_API_KEY_10",
]


def embed_text(text: str, key_env: str = "HUGGINGFACE_API_KEY") -> list:
    """Embeds `text` via HuggingFace Inference API, returns a 384-dim
    vector (list[float]) ready for Upstash Vector's upsert()/query().

    Raises RuntimeError if `key_env` is missing from the environment, or
    the HF request fails outright (caller decides how to degrade -- e.g.
    agents/memory_search.py already wraps its embed_text() calls in
    try/except and treats a failure as "no prior context," not a hard
    error).

    `key_env` (Patch 7) defaults to "HUGGINGFACE_API_KEY" so every
    existing caller that doesn't pass it keeps today's exact behavior --
    this parameter is additive, not a breaking change to embed_text()'s
    contract. It exists so embed_text_with_fallback() below can point a
    single attempt at a specific account without embed_text() needing to
    know anything about fallback chains itself."""
    api_key = os.getenv(key_env)
    if not api_key:
        raise RuntimeError(f"{key_env} not set — required for embed_text().")

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


def embed_text_with_fallback(text: str, key_envs: list = None) -> tuple:
    """Patch 7: the real fallback chain the rollout guide calls for.
    Walks `key_envs` (default HF_EMBEDDING_KEY_ENVS) in order via
    embed_text(), moving to the next account on ANY failure -- missing
    key or a failed HF request alike, same "just try the next one"
    contract generate_text() already uses for its provider chains in
    utils/llm_client.py. Returns (vector, key_env_that_answered) so the
    caller can log usage against the account that actually got billed,
    not always the first one in the list.

    Raises RuntimeError only once every step in the chain is exhausted --
    same as embed_text() raising today, just delayed until there's
    genuinely nowhere left to fall back to. Callers that already wrap
    embed_text() in a broad try/except (memory_search.py,
    duplication_checker.py) don't need to change that handling at all,
    only what they call and what they log."""
    keys = key_envs or HF_EMBEDDING_KEY_ENVS
    last_exc = None
    for key_env in keys:
        try:
            vector = embed_text(text, key_env=key_env)
            return vector, key_env
        except Exception as exc:
            last_exc = exc
            continue
    raise RuntimeError(
        f"embed_text_with_fallback() exhausted all {len(keys)} HF account(s) "
        f"({', '.join(keys)}). Last error: {last_exc}"
    )