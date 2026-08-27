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
  3. Patch B7 — deterministic/generative split: not every cacheable
     call site wants the same thing from a "hit". A datasheet lookup
     or a spec calculation has exactly one correct answer, so replaying
     it verbatim is correct, not stale — that's CACHE_CLASS_DETERMINISTIC,
     and it keeps using check_cache()/write_cache() exactly as before.
     Advice, brainstorming, plans, and explanations are different: the
     "correct" answer isn't unique, and replaying the same paragraph
     verbatim on a repeat ask reads as frozen/unhelpful even when
     nothing is factually wrong with it. That's CACHE_CLASS_GENERATIVE —
     call sites never get the literal old answer back for replay; they
     call get_cached_reference() instead, which hands back the prior
     answer as *reference material* to feed into a fresh generation
     (see task_runner.py / loop_v4.py call sites for the prompt-side
     half of this). classify_cache_class() is a cheap keyword heuristic,
     not a model call — the whole point of SGA's fast path is to stay
     fast, so classification can't itself cost a network round trip.
"""
import hashlib
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import vector_index
from relay.emitter import emit_event  # NEW — CO4 patch 3
from utils.llm_client import embed_text, generate_text

SIMILARITY_THRESHOLD = 0.93
INVALIDATION_THRESHOLD = 0.90
CACHE_TTL_SECONDS = 60 * 60 * 48

# Patch B7 — see module docstring point 3.
CACHE_CLASS_DETERMINISTIC = "deterministic"
CACHE_CLASS_GENERATIVE = "generative"

# Keyword heuristic for classify_cache_class(). Deliberately conservative:
# anything not clearly a lookup/calculation/correctness-check defaults to
# GENERATIVE (see classify_cache_class()'s docstring for why the default
# leans that way).
_DETERMINISTIC_SIGNALS = (
    "calculate", "compute", "convert", "look up", "lookup", "datasheet",
    "spec sheet", "specification", "checksum", "hash of", "what is the value of",
    "what's the value of", "how many", "how much is", "resistor value",
    "voltage", "wattage", "tolerance rating", "is this code correct",
    "check this code", "does this compile", "syntax error", "unit test",
    "what is the boiling point", "what is the melting point", "molecular weight",
    "exchange rate", "square root of", "sum of", "average of",
)

# llama-3.3-70b-versatile decommissioned by Groq; migrated to the two
# models Groq's decommission notice suggested in its place.
_VERIFY_CHAIN = [
    {"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "SGA_GROQ_1"},
    {"provider": "groq", "model": "qwen/qwen3.6-27b", "key_env": "SGA_GROQ_1"},
]

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


def classify_cache_class(task_text: str) -> str:
    """Patch B7 — cheap, deterministic (pun intended) keyword split used
    at each cacheable call site to decide which cache behavior applies.
    Not a model call: SGA's whole premise is a fast pre-Inspector path,
    so classification has to be ~free.

    Errs toward CACHE_CLASS_GENERATIVE when unsure — worst case for a
    false "generative" is a lookup gets regenerated instead of replayed
    (slower, but still correct); worst case for a false "deterministic"
    is an opinion/plan/explanation gets woodenly replayed verbatim on a
    repeat ask, which is exactly the staleness this patch exists to fix.
    """
    lowered = (task_text or "").lower()
    if any(signal in lowered for signal in _DETERMINISTIC_SIGNALS):
        return CACHE_CLASS_DETERMINISTIC
    return CACHE_CLASS_GENERATIVE


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
                 context_text: str = "", session_id: str = None) -> dict | None:
    """Pass EXACTLY ONE of app_slug/workspace_id for a real scope, or
    neither for the legacy global bucket (existing callers not yet
    migrated keep working, just without scoping — same as before).

    session_id: NEW — CO4 patch 3, optional. Purely for the "cache_hit"
    event emitted below on an actual hit — emit_event() is already a
    documented no-op when session_id is None (see relay/emitter.py), so
    every existing caller that doesn't pass this (eo/loop_v4.py's CLI
    path never has a real session_id at all) keeps behaving exactly as
    before.

    Patch B7: this function is the DETERMINISTIC path only — call sites
    route here after classify_cache_class() (or their own judgment)
    says CACHE_CLASS_DETERMINISTIC. It only ever matches entries stored
    with cache_class == deterministic (or written before this patch,
    which default to deterministic — see write_cache()), so a
    generative entry can never accidentally get replayed verbatim
    through this path. GENERATIVE call sites use get_cached_reference()
    instead, further down.
    """
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

    if meta.get("cache_class", CACHE_CLASS_DETERMINISTIC) != CACHE_CLASS_DETERMINISTIC:
        return None  # generative entry — never replayed verbatim (Patch B7)

    answer = meta.get("answer")
    if not answer:
        return None

    stored_fingerprint = meta.get("context_fingerprint", "")
    current_fingerprint = _fingerprint(context_text)
    if stored_fingerprint and stored_fingerprint == current_fingerprint:
        emit_event("cache_hit", session_id=session_id, agent="semantic_cache",
                   payload={"verified": False, "similarity": top.score})
        return answer

    if _verify_still_accurate(task_text, answer, context_text):
        emit_event("cache_hit", session_id=session_id, agent="semantic_cache",
                   payload={"verified": True, "similarity": top.score})
        return answer
    return None


def write_cache(task_text: str, answer: str, app_slug: str = None, workspace_id: str = None,
                 context_text: str = "", cache_class: str = CACHE_CLASS_DETERMINISTIC) -> None:
    """cache_class: NEW — Patch B7. Tags the entry so a later check_cache()
    (deterministic replay) or get_cached_reference() (generative
    reference-only reuse) knows how it's allowed to be reused. Defaults
    to CACHE_CLASS_DETERMINISTIC so any caller not yet migrated keeps
    today's replay behavior unchanged."""
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
        "cache_class": cache_class,
    }
    metadata.update(_scope_metadata(scope_type, scope_id))
    index.upsert(vectors=[{
        "id": f"semcache_{hash(task_text)}",
        "vector": vector,
        "metadata": metadata,
    }])


def get_cached_reference(task_text: str, app_slug: str = None, workspace_id: str = None) -> str | None:
    """Patch B7 — GENERATIVE call sites only. Finds a semantically similar
    prior answer to hand back as *reference context* for a fresh
    generation. This is deliberately NOT check_cache(): there's no
    fingerprint/context-verify short-circuit and no cache_hit event,
    because reusing an old answer as an input to a new one isn't a
    "hit" in the replay sense — it's raw material. Matches entries of
    either cache_class (a deterministic answer is still fine background
    material for a generative follow-up); the asymmetry that matters is
    the other direction, enforced in check_cache().
    """
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

    return meta.get("answer") or None


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