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
from memory.bus import read, write

# How many independent misses on the exact same (chat_id, before_seq,
# limit) page, within HIT_WINDOW_SECONDS, before the page is considered
# "hot" and actually gets cached. 3 is a starting judgment call, not a
# measured number -- tune once you have real hit-count data; there's
# nothing structural tying this to 3.
HIT_THRESHOLD = 3

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


def _hits_key(chat_id: str, before_seq: int, limit: int) -> str:
    return f"chat_page_hits:{chat_id}:{before_seq}:{limit}"


def _page_key(chat_id: str, before_seq: int, limit: int) -> str:
    return f"chat_page_cache:{chat_id}:{before_seq}:{limit}"


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

    Deliberately not atomic (read-modify-write, not INCR) — a lost
    update under concurrent misses just means the threshold gets
    crossed a request or two later than the exact Nth hit, which is
    harmless for a "should this become hot?" heuristic. Not worth a
    second Redis primitive for that.
    """
    hits_key = _hits_key(chat_id, before_seq, limit)
    hits = (read(hits_key, default=0) or 0) + 1
    write(hits_key, hits, ex=HIT_WINDOW_SECONDS)
    if hits >= HIT_THRESHOLD:
        write(
            _page_key(chat_id, before_seq, limit),
            {"messages": messages, "has_more": has_more},
            ex=CACHE_TTL_SECONDS,
        )
