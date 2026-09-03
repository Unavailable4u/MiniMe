"""
eo/chat_page_cache.py — server-side cache for HOT, CLOSED pages of chat
history (perf audit item #7). Deliberately narrow in scope, on purpose:

Per the perf-audit risk assessment this follows from -- get_chat() is
already an indexed, paginated Postgres query (chat_messages_chat_id_seq_idx
covers it directly), and items #5 (after_seq delta fetch) and #6 (client-
side IndexedDB cache) already make the PER-USER case -- the same person
reopening a chat they've had open before -- cheap without any server-side
cache at all. What those two items don't help with is FAN-OUT: many
DIFFERENT users (or the same user, cold, on a device with no client cache
yet) independently paging through the older history of the same popular/
shared chat, each one a full cache miss that hits Postgres. That's the one
scenario this module exists for. If you're not seeing that pattern in
production, this module earns its keep by sitting unused at effectively
zero cost (see HIT_THRESHOLD below) -- don't wire it in until you've
confirmed via real traffic that some chats' older-message pages actually
get requested by more than a couple of distinct callers.

WHAT GETS CACHED, AND WHY ONLY THAT: exclusively before_seq-paginated
pages -- i.e. calls where before_seq is not None. That's the "closed,
immutable seq range" case: a page bounded above by a fixed before_seq is,
by construction, a range of messages that already existed at request time
and (per chat_store.py's own docs -- messages are append-only, no
edit/delete path exists) can never change underneath it. The unpaginated
full-chat fetch (limit=None) and the plain "latest N" fetch (limit given,
before_seq=None) are deliberately NEVER cached here -- both include the
live tail of the chat, which grows every time someone sends a message, so
"cache it" would mean either serving stale tails or re-deriving an
invalidation story this module is specifically trying to avoid needing.
Same reasoning rules out caching after_seq delta responses: those are
already cheap by design (item #5), and by definition read past whatever
was last known, i.e. never a fixed/closed range.

HIT-THRESHOLD GATING, NOT UNCONDITIONAL CACHING: caching every page on
first read would spend Redis memory on cold, one-off requests that were
never going to repeat -- exactly the "spending memory for nothing" risk
flagged in the review this implements. Instead: every miss increments a
short-lived hit counter for that exact (chat_id, before_seq, limit) key;
only once a single page has been independently requested HIT_THRESHOLD
times within HIT_WINDOW_SECONDS does the NEXT miss actually populate the
cache. A page that's genuinely hot (many different callers) crosses the
threshold quickly; a page nobody else ever asks for again just has its
counter key expire, unused, at effectively zero storage cost.

AUTH IS UNTOUCHED: every call into get_cached_page()/note_page_miss()
happens from inside chat_store.get_chat(), which is only ever reached
after api/deps._resolve_chat_or_404() has already confirmed the requester
has access to this chat_id (owner or workspace collaborator) -- see that
function's own docstring. This module never bypasses that check or knows
anything about who's asking; it only ever gets called once access is
already confirmed, exactly like the underlying Postgres query it's
standing in front of.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import incr, read, write

# How many independent misses on the exact same (chat_id, before_seq,
# limit) page, within HIT_WINDOW_SECONDS, before the page is considered
# "hot" and actually gets cached. 3 is a starting judgment call, not a
# measured number -- tune once you have real hit-count data; there's
# nothing structural tying this to 3.
HIT_THRESHOLD = 3

# Perf audit item #3 follow-up: once /api/system/chat-page-cache-stats's
# hit_rate and /api/system/backend-latency-probe's numbers are both in,
# decide whether the HIT_THRESHOLD/HIT_WINDOW_SECONDS two-tunable
# rolling-window gating above is worth its complexity, or whether a
# simpler rule -- cache a page the moment it's been independently
# missed twice, full stop, with its counter's TTL tied to
# CACHE_TTL_SECONDS instead of its own separate HIT_WINDOW_SECONDS --
# gets similar real-world behavior with fewer moving parts (one less
# tunable to reason about) at the same round-trip cost (still a single
# incr() per miss either way -- see note_page_miss() below).
#
# Defaults to False, preserving the original threshold=3/1-hour-window
# behavior exactly. This is a decision the perf audit explicitly says
# needs real hit_rate data behind it, not something to flip blindly --
# don't set this true until that data actually supports it. Once it
# does, set CHAT_PAGE_CACHE_SIMPLE_HEURISTIC=true (no code change
# needed) to switch over.
SIMPLE_HEURISTIC = os.getenv("CHAT_PAGE_CACHE_SIMPLE_HEURISTIC", "false").lower() == "true"

# The "requested twice" threshold for SIMPLE_HEURISTIC above. Kept as
# its own constant (rather than reusing HIT_THRESHOLD) since the two
# modes are deliberately allowed to disagree on their number --
# SIMPLE_HEURISTIC's whole premise is "lower the bar, drop the
# rolling-window nuance," not "keep the same threshold with less
# bookkeeping."
SIMPLE_HIT_THRESHOLD = 2

# Rolling window the hit counter lives in. Deliberately much shorter than
# CACHE_TTL_SECONDS below -- this window is only about detecting "hit
# repeatedly in a short burst" (real fan-out), not "hit 3 times ever, a
# year apart" (not actually hot, just old).
HIT_WINDOW_SECONDS = 60 * 60  # 1 hour

# TTL for an actually-cached page. Long, not infinite, even though the
# underlying data is immutable -- see the module docstring's fan-out
# framing: this is deliberately a bet on CURRENT popularity, not a
# permanent record. A page that was hot a month ago and cold since is
# better evicted and, if it somehow gets hot again, recomputed once
# (one Postgres read) and re-cached, than held in memory forever on the
# strength of a burst that's long over. Tune independently of
# HIT_THRESHOLD/HIT_WINDOW_SECONDS.
CACHE_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days


# Perf audit item #3: this module already tracked a PER-PAGE miss
# counter (see note_page_miss() below) for its own hit-threshold
# gating, but that's not the same thing as knowing whether the cache
# is actually worth its per-call Redis REST overhead (B4) overall.
# These two keys are a separate, deliberately coarse GLOBAL counter —
# every before_seq-paginated lookup bumps exactly one of them — so
# real traffic can answer that go/no-go question via get_cache_stats()
# before anyone spends more engineering time tuning HIT_THRESHOLD or
# deciding whether to rip this module out. Not scoped per-chat or
# per-page; that's what the per-page counters above are for.
_STATS_HIT_KEY = "chat_page_cache:stats:hits"
_STATS_MISS_KEY = "chat_page_cache:stats:misses"

# How long the aggregate counters are kept before expiring. Long
# enough to accumulate a meaningful sample across normal usage
# patterns (unlike HIT_WINDOW_SECONDS, which is intentionally short —
# that one's detecting a burst, this one's just tallying totals), but
# not infinite, so a stale measurement from months ago doesn't linger
# forever if nobody ever reads it.
STATS_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days


def _hits_key(chat_id: str, before_seq: int, limit: int) -> str:
    return f"chat_page_hits:{chat_id}:{before_seq}:{limit}"


def _page_key(chat_id: str, before_seq: int, limit: int) -> str:
    return f"chat_page_cache:{chat_id}:{before_seq}:{limit}"


def record_cache_result(hit: bool) -> None:
    """Bumps the global hit or miss counter. Call this once per
    before_seq-paginated lookup, right after get_cached_page() —
    exactly the same call site that already knows whether `cached`
    came back None or not, so there's no extra Redis round trip beyond
    the one this module already made for the actual lookup.

    Uses memory.bus.incr() (atomic INCR, 1 REST round trip — 2 on a
    counter's first increment in a STATS_TTL_SECONDS cycle, for the
    follow-up EXPIRE), instead of the read-then-write pattern this
    function used originally. That earlier version cost 2 round trips
    on every single call and had a lost-update race under concurrent
    requests; INCR is both cheaper and race-free."""
    key = _STATS_HIT_KEY if hit else _STATS_MISS_KEY
    incr(key, ex=STATS_TTL_SECONDS)


def get_cache_stats() -> dict:
    """Returns {"hits": int, "misses": int, "hit_rate": float | None,
    "simple_heuristic_enabled": bool} for the global before_seq-page-
    cache counters. hit_rate is None (rather than 0.0) when there's no
    data yet at all, so "definitely cold" isn't confused with "haven't
    measured." This is the number perf-audit item #3 asks for before
    deciding whether item #7 (this whole module) is worth keeping
    enabled given B4's per-call Redis REST overhead: a low hit rate
    here means every before_seq page load is paying for a Redis GET
    (and often a second GET+SET for the per-page hit counter) that
    essentially never pays off.

    simple_heuristic_enabled reports which gating mode (see
    SIMPLE_HEURISTIC above) actually produced these numbers -- relevant
    context when reading hit_rate, since the two modes cache pages
    under different conditions (HIT_THRESHOLD=3/rolling-window vs.
    SIMPLE_HIT_THRESHOLD=2/fixed-window) and aren't directly comparable
    across a mode switch."""
    hits = read(_STATS_HIT_KEY, default=0) or 0
    misses = read(_STATS_MISS_KEY, default=0) or 0
    total = hits + misses
    return {
        "hits": hits,
        "misses": misses,
        "hit_rate": (hits / total) if total else None,
        "simple_heuristic_enabled": SIMPLE_HEURISTIC,
    }


def get_cached_page(chat_id: str, before_seq: int, limit: int) -> dict | None:
    """Returns {"messages": [...], "has_more": bool} if this exact page
    is currently cached, else None. chat_store.get_chat() should only
    call this for the before_seq-paginated branch — see module
    docstring for why the other two shapes of get_chat() call are never
    routed through this cache at all."""
    return read(_page_key(chat_id, before_seq, limit), default=None)


def note_page_miss(chat_id: str, before_seq: int, limit: int,
                    messages: list, has_more: bool) -> None:
    """Call this on a cache miss, right after the real Postgres query
    has run — increments this exact page's hit counter and, only once
    it crosses HIT_THRESHOLD, writes the page into the cache so the
    NEXT request for it is a cache hit instead of another Postgres
    round trip. Cheap and safe to call on every miss unconditionally:
    a page that never gets requested again just leaves an unused
    counter key to expire after HIT_WINDOW_SECONDS, nothing more.

    Uses memory.bus.incr() (atomic INCR, 1 REST round trip — 2 on this
    key's first increment in a HIT_WINDOW_SECONDS cycle, for the
    follow-up EXPIRE) instead of the original read-then-write. One
    real behavior change from that switch: the window is now FIXED
    from the first miss in a cycle, not sliding/reset on every miss
    (read()/write(key, ..., ex=...) refreshed the TTL on every call;
    incr()'s EXPIRE only fires on creation) — acceptable here since
    this was always an approximate "should this become hot?" heuristic,
    not a precise rolling window, and the atomicity this buys removes
    the lost-update race the previous version's docstring called out
    as a known (accepted) limitation.

    When SIMPLE_HEURISTIC is on, this instead uses SIMPLE_HIT_THRESHOLD
    (2) and ties the counter's own TTL to CACHE_TTL_SECONDS rather than
    the separate HIT_WINDOW_SECONDS -- "cache it the moment two
    independent callers have missed on it, ever (within a week)," no
    rolling-window nuance at all. Same round-trip cost either way: one
    incr() call.
    """
    hits_key = _hits_key(chat_id, before_seq, limit)
    if SIMPLE_HEURISTIC:
        hits = incr(hits_key, ex=CACHE_TTL_SECONDS)
        threshold = SIMPLE_HIT_THRESHOLD
    else:
        hits = incr(hits_key, ex=HIT_WINDOW_SECONDS)
        threshold = HIT_THRESHOLD
    if hits >= threshold:
        write(
            _page_key(chat_id, before_seq, limit),
            {"messages": messages, "has_more": has_more},
            ex=CACHE_TTL_SECONDS,
        )
