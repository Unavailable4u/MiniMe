"""eo/datasheet_cache.py — plain TTL cache for component_spec_lookup.py's
get_datasheet_detail() results (F3 Part 5).

Same memory.bus read/write mechanism eo/spec_cache.py/eo/price_cache.py
already use, and the same 180-day TTL reasoning as spec_cache.py: a
given datasheet_url's PDF content doesn't change once published, so
this is a self-healing safety net (a bad parse getting fixed, a link
going stale) rather than a sign the underlying data is expected to
drift.

Keyed by datasheet_url rather than part_number -- deliberately a
separate cache from spec_cache.py, not a new field bolted onto it,
since a datasheet_url is looked up (and worth caching) independently
of whether the w/h/d dimensions_mm parse succeeded, and this holds
much larger values (full extracted PDF text) than spec_cache.py's
small dimensions/url dict -- keeping them separate means a spec_cache
read never pulls that extra weight along for callers (Part 4's
_populate_dimensions) that only need dimensions_mm/datasheet_url/
source.

Only successful ingests are cached (get_datasheet_detail() writes here
only after a real download+parse succeeds) -- a miss (download
failure, non-PDF response, parse error) is cheap enough, and
transient enough (a flaky fetch, a momentarily-down vendor CDN), not
to be worth caching as a standing negative result.
"""
import hashlib
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write

TTL_SECONDS = 60 * 60 * 24 * 180  # 180 days — a published PDF's content doesn't move


def _key(datasheet_url: str) -> str:
    # URL itself isn't a safe/short bus key (length, characters) --
    # hash it, same "don't trust external strings as key material"
    # reasoning slug-ing part_number gets in spec_cache.py's own _key().
    digest = hashlib.sha256(datasheet_url.strip().encode("utf-8")).hexdigest()[:32]
    return f"datasheet_cache:{digest}"


def get_cached_datasheet(datasheet_url: str) -> dict | None:
    entry = read(_key(datasheet_url), default=None)
    if not entry:
        return None
    if time.time() - entry.get("_cached_at", 0) > TTL_SECONDS:
        return None
    return entry


def set_cached_datasheet(datasheet_url: str, result: dict) -> None:
    write(_key(datasheet_url), {**result, "_cached_at": time.time()})
