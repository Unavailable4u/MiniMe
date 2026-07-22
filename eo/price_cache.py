"""eo/price_cache.py — plain TTL cache for part_price_finder.py results.
Prices don't move minute to minute; re-searching the same part every
page load would blow through the free search-API quota for no reason.
Same memory.bus read/write mechanism the rest of the system already uses
for persistence — no new storage layer.
"""
import os, sys, re, time
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write

TTL_SECONDS = 60 * 60 * 24 * 5  # 5 days — parts pricing is slow-moving

def _key(part_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", part_name.strip().lower())
    return f"price_cache:{slug}"

def get_cached_price(part_name: str) -> dict | None:
    entry = read(_key(part_name), default=None)
    if not entry:
        return None
    if time.time() - entry.get("_cached_at", 0) > TTL_SECONDS:
        return None
    return entry

def set_cached_price(part_name: str, result: dict) -> None:
    write(_key(part_name), {**result, "_cached_at": time.time()})