"""
eo/semantic_cache.py — Semantic Cache (blueprint §16.2), context-aware
and scope-aware.

Two independent fixes bundled together:
  1. Trust model (see prior revision): a hit is only replayed blindly
     if the context fingerprint is unchanged; otherwise it's verified
     against current context before replay, and invalidate_cache() lets
     a correction purge stale entries proactively instead of waiting on
     TTL.
  2. Scoping: `app_slug` alone conflated two unrelated concepts —
     build/research pipeline projects (app_slug) and notebook/workspace
     ids (workspace_id, from eo/chat_workspace.py). A cached answer
     scoped to one must never leak into, or be purged by, the other.
     Callers now pass an explicit (scope_type, scope_id) pair instead
     of a bare app_slug.
"""
import os
import sys
import time
import hashlib
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import vector_index
from utils.llm_client import embed_text, generate_text

SIMILARITY_THRESHOLD = 0.93
INVALIDATION_THRESHOLD = 0.90
CACHE_TTL_SECONDS = 60 * 60 * 48

_VERIFY_CHAIN = [{"provider": "groq", "model": "llama-3.3-70b-versatile", "key_env": "SGA_GROQ_1"}]

_VERIFY_SYSTEM_PROMPT = """You check whether a previously-given answer is still accurate. \
You will be shown the original question, a previously cached answer, and the CURRENT \
conversation context (which may include new information, corrections, or be unrelated to \
the cached answer). Respond with exactly one word: YES if the cached answer is still \
accurate and nothing in the current context contradicts or supersedes it. NO if the current \
context contradicts it, corrects it, makes it outdated, or you are not confident it still \
holds. When in doubt, say NO — a fresh answer is always safer than a stale one."""


def _scope_filter(scope_type: str, scope_id: str) -> str:
    """scope_type is 'app' or 'workspace' (or None for legacy/global
    callers not yet migrated). Each gets its own metadata field so an
    app-scoped entry and a workspace-scoped entry can never collide or
    be purged by the other, even if their ids happened to be equal by
    coincidence."""
    if scope_type and scope_id:
        return f"{scope_type} = '{scope_id}'"
    return "project = 'global'"  # legacy bucket — unmigrated callers only


def _scope_metadata(scope_type: str, scope_id: str) -> dict:
    if scope_type and scope_id:
        return {scope_type: scope_id}
    return {"project": "global"}


def _fingerprint(context_text: str) -> str:
    normalized = (context_text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _verify_still_accurate(task_text: str, cached_answer: str, context_text: str) -> bool:
    prompt = (
        f"Original question: {task_text}\n\n"
        f"Previously cached answer: {cached_answer}\n\n"
        f"Current conversation context:\n{context_text or '(none)'}\n\n"
        f"Is the cached answer still accurate? Reply YES or NO only."
    )
    try:
        result = generate_text(
            system_prompt=_VERIFY_SYSTEM_PROMPT,
            user_content=prompt,
            chain=_VERIFY_CHAIN,
            agent_name="semantic_cache_verify",
        ).strip().upper()
    except Exception:
        return False
    return result.startswith("YES")


def check_cache(task_text: str, app_slug: str = None, workspace_id: str = None,
                 context_text: str = "") -> dict | None:
    """Pass EXACTLY ONE of app_slug/workspace_id for a real scope, or
    neither for the legacy global bucket (existing callers not yet
    migrated keep working, just without scoping — same as before)."""
    scope_type = "app" if app_slug else ("workspace" if workspace_id else None)
    scope_id = app_slug or workspace_id

    try:
        vector = embed_text(task_text)
    except Exception:
        return None

    index = vector_index()
    results = index.query(vector=vector, top_k=1, include_metadata=True,
                          filter=_scope_filter(scope_type, scope_id))
    if not results:
        return None

    top = results[0]
    if top.score < SIMILARITY_THRESHOLD:
        return None

    meta = top.metadata or {}
    if time.time() - meta.get("_cached_at", 0) > CACHE_TTL_SECONDS:
        return None

    answer = meta.get("answer")
    if not answer:
        return None

    stored_fingerprint = meta.get("context_fingerprint", "")
    current_fingerprint = _fingerprint(context_text)
    if stored_fingerprint and stored_fingerprint == current_fingerprint:
        return answer

    if _verify_still_accurate(task_text, answer, context_text):
        return answer
    return None


def write_cache(task_text: str, answer: str, app_slug: str = None, workspace_id: str = None,
                 context_text: str = "") -> None:
    scope_type = "app" if app_slug else ("workspace" if workspace_id else None)
    scope_id = app_slug or workspace_id

    try:
        vector = embed_text(task_text)
    except Exception:
        return
    index = vector_index()
    metadata = {
        "answer": answer,
        "_cached_at": time.time(),
        "context_fingerprint": _fingerprint(context_text),
    }
    metadata.update(_scope_metadata(scope_type, scope_id))
    index.upsert(vectors=[{
        "id": f"semcache_{hash(task_text)}",
        "vector": vector,
        "metadata": metadata,
    }])


def invalidate_cache(text: str, app_slug: str = None, workspace_id: str = None) -> int:
    """Proactively purges near-matching cache entries within the given
    scope. Pass workspace_id when the correction/fact came from a
    notebook (the common case for note_candidates.py/workspace_facts.py
    callers), app_slug for a build/research pipeline correction. If
    neither is given, purges from the global bucket only — deliberately
    NOT a cross-scope wildcard, so a workspace correction can never
    reach into an unrelated app's cached answers or vice versa."""
    scope_type = "app" if app_slug else ("workspace" if workspace_id else None)
    scope_id = app_slug or workspace_id

    try:
        vector = embed_text(text)
    except Exception as exc:
        print(f"  [semantic_cache] invalidate_cache embedding failed, skipped: {exc}")
        return 0
    index = vector_index()
    try:
        results = index.query(vector=vector, top_k=10, include_metadata=True,
                              filter=_scope_filter(scope_type, scope_id))
        stale_ids = [r.id for r in results if r.score >= INVALIDATION_THRESHOLD]
        if stale_ids:
            index.delete(ids=stale_ids)
        return len(stale_ids)
    except Exception as exc:
        print(f"  [semantic_cache] invalidate_cache query/delete failed, skipped: {exc}")
        return 0