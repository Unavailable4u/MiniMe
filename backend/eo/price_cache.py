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

def _key(part_name: str, tier: str = "bd") -> str:
    # tier: NEW (T2b, step 17) -- "bd" (default, unchanged prefix) or
    # "intl" for the AliExpress/eBay fallback tier. Kept as a distinct
    # key prefix, not a shared one, so a later BD listing (if one ever
    # appears) can still take priority over a cached international one.
    slug = re.sub(r"[^a-z0-9]+", "_", part_name.strip().lower())
    prefix = "price_cache" if tier == "bd" else f"price_cache_{tier}"
    return f"{prefix}:{slug}"

def get_cached_price(part_name: str, tier: str = "bd") -> dict | None:
    entry = read(_key(part_name, tier), default=None)
    if not entry:
        return None
    if time.time() - entry.get("_cached_at", 0) > TTL_SECONDS:
        return None
    return entry

def set_cached_price(part_name: str, result: dict, tier: str = "bd") -> None:
    write(_key(part_name, tier), {**result, "_cached_at": time.time()})