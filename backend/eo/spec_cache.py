"""eo/spec_cache.py — plain TTL cache for component_spec_lookup.py results.

Same memory.bus read/write mechanism eo/price_cache.py already uses --
no new storage layer -- but a much longer TTL: unlike price_cache.py's
5-day window (prices genuinely move), a real part's physical
dimensions_mm/datasheet_url don't change once DigiKey or Mouser has
confirmed them. TTL here exists purely as a self-healing safety net
(a wrong parse getting fixed and needing to eventually re-populate,
a datasheet link going stale) rather than because the underlying data
is expected to drift -- so it's set to 180 days, not price_cache.py's
5.

Keeps DigiKey/Mouser quota usage low across repeated project
generations that reuse common parts (ESP32, common sensors, etc.) --
the same reasoning price_cache.py's own docstring gives for prices.

Only successful lookups are cached (get_real_spec() writes here only
after a real DigiKey/Mouser hit) -- a miss (part not found on either
vendor, or creds unset) is cheap enough, and ambiguous enough per-part
number, not to be worth caching as a standing negative result the way
price_cache.py caches an empty listings=[] search.
"""
import os
import sys
import re
import time
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write

TTL_SECONDS = 60 * 60 * 24 * 180  # 180 days — physical dimensions don't move

def _key(part_number: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", part_number.strip().lower())
    return f"spec_cache:{slug}"

def get_cached_spec(part_number: str) -> dict | None:
    entry = read(_key(part_number), default=None)
    if not entry:
        return None
    if time.time() - entry.get("_cached_at", 0) > TTL_SECONDS:
        return None
    return entry

def set_cached_spec(part_number: str, result: dict) -> None:
    write(_key(part_number), {**result, "_cached_at": time.time()})
