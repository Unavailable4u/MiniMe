"""
eo/research_cache.py — TTL cache for the web_researcher agent's search
results (task 13b). Same memory.bus read/write persistence
price_cache.py already uses -- no new storage layer -- but with a
DIFFERENT TTL PER SCOPE instead of price_cache.py's one fixed 5-day
number: that 5-day figure is honest only because BD_VENDOR_DOMAINS
pricing genuinely is that stale-tolerant. Research scopes aren't uniform
the same way -- a news query goes stale in hours, a forum thread accrues
replies over a day, and there's no single TTL that's honest for both.

Keyed `scope:query_slug`, not just `query_slug` alone -- so a "reddit
search for X" and a "news search for X" cache independently even when
the underlying query string is identical. The same result set can be
perfectly fresh under a 5-day academic-source TTL and stale garbage under
a 2-hour news TTL, so scope has to be part of the key, not just part of
the TTL lookup.

ONE DELIBERATE DEVIATION from price_cache.py's actual mechanics (not just
its numbers): price_cache.py tracks its own `_cached_at` timestamp and
checks staleness by hand on every read, because at the time it was
written memory.bus.write() had no TTL support of its own. write() has
since grown a native `ex` (seconds) param (see bus.py's own write()
docstring). This module uses that instead of reimplementing manual
staleness tracking -- functionally equivalent from every caller's POV
(an expired entry reads back as a miss either way), but Redis actually
evicts the key at the boundary rather than leaving a permanently-growing
set of stale, silently-ignored entries sitting in the bus forever. If you'd
rather this stay byte-for-byte structurally identical to price_cache.py
(manual `_cached_at`, no `ex`), say so and I'll switch it -- this was a
judgment call, not a requirement from the guide.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, slugify, write

# Per-scope TTLs, in seconds. Keys here must match the scope names
# web_researcher.py (task 13c) actually passes in -- see that module's
# own scope-preset list once it exists (13c's plan: general / forum /
# news / hackernews / academic / no-scope). "general" doubles as the
# no-scope-selected default. DEFAULT_TTL below covers any scope name
# NOT listed here, so adding a new scope later doesn't silently get zero
# caching or a KeyError -- it just falls back to a middle-of-the-road
# number until someone deliberately tunes an entry for it.
TTL_BY_SCOPE = {
    "general": 60 * 60 * 24,        # 1 day -- general web results shift, but not fast
    "forum": 60 * 60 * 12,          # 12h -- Reddit/forum threads accrue replies within a day
    "news": 60 * 60 * 2,            # 2h -- news queries need to stay close to current
    "hackernews": 60 * 60 * 6,      # 6h -- HN discussion moves, but slower than a news wire
    "academic": 60 * 60 * 24 * 5,   # 5 days -- same stale-tolerance as price_cache.py's pricing
}
DEFAULT_TTL = 60 * 60 * 6  # 6h fallback for any scope not listed above


def _key(scope: str, query: str) -> str:
    return f"research_cache:{scope}:{slugify(query)}"


def get_cached_research(scope: str, query: str) -> dict | None:
    """Returns the cached result dict, or None on a miss (never cached,
    or evicted past its scope's TTL -- write()'s `ex` makes those two
    cases indistinguishable at the Redis level, which is fine: every
    caller treats them the same way, same as price_cache.py's callers
    already do)."""
    return read(_key(scope, query), default=None)


def set_cached_research(scope: str, query: str, result: dict) -> None:
    ttl = TTL_BY_SCOPE.get(scope, DEFAULT_TTL)
    write(_key(scope, query), result, ex=ttl)
