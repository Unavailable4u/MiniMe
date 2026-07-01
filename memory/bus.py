
import os
import json
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

redis = Redis(
    url=os.getenv("UPSTASH_REDIS_REST_URL"),
    token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
)

def write(key: str, value):
    """Write any JSON-serializable value to memory."""
    redis.set(key, json.dumps(value))

def read(key: str, default=None):
    """Read a value back from memory. Returns default if not found."""
    raw = redis.get(key)
    if raw is None:
        return default
    return json.loads(raw)

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
}
