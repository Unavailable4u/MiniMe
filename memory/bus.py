
import os
import json
import re
from dotenv import load_dotenv
from upstash_redis import Redis
from upstash_vector import Index
load_dotenv()
redis = Redis(
    url=os.getenv("UPSTASH_REDIS_REST_URL"),
    token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
)
def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower()).strip("_")
    return slug[:max_len] or "untitled_app"
_app_slug_cache = None
def _current_app_slug():
    global _app_slug_cache
    if _app_slug_cache is None:
        raw = redis.get("app_slug")
        if raw is not None:
            _app_slug_cache = json.loads(raw)
    return _app_slug_cache
def _namespaced(key: str) -> str:
    """Prefixes every key with the active app_slug, except app_slug itself
    (bootstrap key, can't prefix itself) and usage:* keys (Part 7.1 --
    quota is a property of your accounts, not any one project)."""
    if key == "app_slug" or key.startswith("usage:"):
        return key
    slug = _current_app_slug()
    return f"{slug}:{key}" if slug else key
# DB4 — Vector (Part 5, memory schema). Used by Cross-Cycle Memory Search
# (#0) and the Duplication/Similarity Checker (#7). Both share this one
# index but use different id-prefix namespaces (see each agent's docstring)
# so their embeddings never collide or get queried against each other.
_vector_index = None
def vector_index() -> Index:
    """Lazy singleton so importing bus.py doesn't require Vector env vars
    to be set for code paths that never touch it (most agents don't)."""
    global _vector_index
    if _vector_index is None:
        url = os.getenv("UPSTASH_VECTOR_REST_URL")
        token = os.getenv("UPSTASH_VECTOR_REST_TOKEN")
        if not url or not token:
            raise RuntimeError(
                "UPSTASH_VECTOR_REST_URL / UPSTASH_VECTOR_REST_TOKEN not set. "
                "Create the Vector index in your Upstash console (Part 5, DB4) "
                "and add both to .env."
            )
        _vector_index = Index(url=url, token=token)
    return _vector_index
def write(key: str, value):
    """Write any JSON-serializable value to memory."""
    redis.set(_namespaced(key), json.dumps(value))
    if key == "app_slug":
        global _app_slug_cache
        _app_slug_cache = value
def read(key: str, default=None):
    """Read a value back from memory. Returns default if not found."""
    raw = redis.get(_namespaced(key))
    if raw is None:
        return default
    value = json.loads(raw)
    if key == "app_slug":
        global _app_slug_cache
        _app_slug_cache = value
    return value
def append_cycle_history(cycle_num: int, report: dict):
    """Store each cycle's report under its own key, for long-term memory."""
    write(f"cycle:{cycle_num}:report", report)
# Standard memory keys used across the loop
KEYS = {
    "original_idea": "original_idea",
    "current_plan": "current_plan",
    "module_specs": "module_specs",
    "submitted_code": "submitted_code",
    "test_code": "test_code",
    "review_notes": "review_notes",
    "fixed_code": "fixed_code",
    "test_results": "test_results",
    "commit_message": "commit_message",
    "changelog_entry": "changelog_entry",
    "latest_report": "latest_report",
    "cycle_count": "cycle_count",
    "loop_decision": "loop_decision",
    "feature_status": "feature_status",
    "file_map": "file_map",
    "app_slug": "app_slug",
    # --- new: the 6 backfilled tier-3 agents ---
    "retrieved_context": "retrieved_context",        # #0 Cross-Cycle Memory Search output
    "dependency_map": "dependency_map",               # #4 Module Dependency Mapper
    "duplication_report": "duplication_report",       # #7 Duplication/Similarity Checker
    "security_scan_results": "security_scan_results", # #12 Security/Dependency Scanner Pool
    "doc_output": "doc_output",                        # #14 Documentation Agent
    "final_qa_verdict": "final_qa_verdict",             # #16 Final QA/Acceptance Reviewer
    # --- Tier-1 lean pipeline (Part 2.4) --- deliberately separate key
    # names from the tier-3 keys above, even though the shapes are
    # similar in places, so a tier-1 run and a tier-3 cycle can never
    # collide if they're ever run back to back on the same app_slug.
    "tier1_task_text": "tier1_task_text",
    "tier1_module_spec": "tier1_module_spec",
    "tier1_code": "tier1_code",
    "tier1_review_notes": "tier1_review_notes",
    "tier1_fixed_code": "tier1_fixed_code",
    "tier1_test_results": "tier1_test_results",
}
