"""
tests/unit/test_eo_price_cache.py — Patch 7e-2.

eo/price_cache.py had zero test coverage before this. The module's own
comment calls out a real prior bug (pricing-audit root cause 3): empty
("not found") results used to share the same 5-day TTL as a real
priced listing, which meant a part that failed for a transient reason
stayed stuck showing "not found" for days. EMPTY_TTL_SECONDS is the
fix -- these tests pin that dual-TTL behavior down as a regression
test, plus the tier-prefix key logic (T2b step 17's bd/intl split).

Isolation: same bound-name gotcha as spec_cache.py -- price_cache.py
does `from memory.bus import read, write`, so tests patch `read`/
`write` on the price_cache module object.
"""
import time

from eo import price_cache

# ---------------------------------------------------------------------
# _key
# ---------------------------------------------------------------------

def test_key_default_tier_uses_unprefixed_price_cache_namespace():
    assert price_cache._key("ATmega328P") == "price_cache:atmega328p"


def test_key_bd_tier_explicit_matches_default_tier():
    assert price_cache._key("ATmega328P", tier="bd") == price_cache._key("ATmega328P")


def test_key_intl_tier_uses_a_distinct_prefix_not_shared_with_bd():
    """intl and bd must never collide on the same key -- a cached BD
    listing must still be able to take priority over a cached
    international one, per the module's own _key() comment."""
    bd_key = price_cache._key("ATmega328P", tier="bd")
    intl_key = price_cache._key("ATmega328P", tier="intl")
    assert intl_key == "price_cache_intl:atmega328p"
    assert intl_key != bd_key


# ---------------------------------------------------------------------
# get_cached_price
# ---------------------------------------------------------------------

def test_get_cached_price_returns_none_on_a_miss(monkeypatch):
    monkeypatch.setattr(price_cache, "read", lambda key, default=None: default)
    assert price_cache.get_cached_price("ATmega328P") is None


def test_get_cached_price_with_listings_uses_the_5_day_ttl(monkeypatch):
    """A priced entry just past the 15-minute EMPTY_TTL but still well
    within the 5-day TTL_SECONDS must be served as a hit -- pins down
    that entry.get("listings") truthiness, not just presence of the
    entry, is what selects the longer TTL."""
    entry = {
        "listings": [{"vendor": "Rocket", "price": 120}],
        "_cached_at": time.time() - (60 * 30),  # 30 min ago
    }
    monkeypatch.setattr(price_cache, "read", lambda key, default=None: entry)
    assert price_cache.get_cached_price("ATmega328P") == entry


def test_get_cached_price_with_listings_expires_after_5_days(monkeypatch):
    entry = {
        "listings": [{"vendor": "Rocket", "price": 120}],
        "_cached_at": time.time() - price_cache.TTL_SECONDS - 1,
    }
    monkeypatch.setattr(price_cache, "read", lambda key, default=None: entry)
    assert price_cache.get_cached_price("ATmega328P") is None


def test_get_cached_price_empty_listings_uses_the_short_15_minute_ttl(monkeypatch):
    """Regression test for the pricing-audit bug: an empty-listings
    result 30 minutes old (well within the old 5-day TTL, but past the
    fixed 15-minute EMPTY_TTL_SECONDS) must read as a miss, so a retry
    naturally re-checks instead of trusting a possibly-spurious empty
    result for days."""
    stale_empty_entry = {
        "listings": [],
        "_cached_at": time.time() - (60 * 30),  # 30 min ago > 15 min EMPTY_TTL
    }
    monkeypatch.setattr(price_cache, "read", lambda key, default=None: stale_empty_entry)
    assert price_cache.get_cached_price("ATmega328P") is None


def test_get_cached_price_empty_listings_within_15_minutes_is_still_a_hit(monkeypatch):
    fresh_empty_entry = {
        "listings": [],
        "_cached_at": time.time() - 60,  # 1 min ago
    }
    monkeypatch.setattr(price_cache, "read", lambda key, default=None: fresh_empty_entry)
    assert price_cache.get_cached_price("ATmega328P") == fresh_empty_entry


def test_get_cached_price_missing_listings_key_entirely_is_treated_as_empty(monkeypatch):
    """entry.get("listings") on an entry with no "listings" key at all
    (not even an empty list) must fall back to the short TTL the same
    way an explicit empty list does, not raise or default to the long
    TTL."""
    entry_no_listings_key = {"_cached_at": time.time() - (60 * 30)}
    monkeypatch.setattr(price_cache, "read", lambda key, default=None: entry_no_listings_key)
    assert price_cache.get_cached_price("ATmega328P") is None


def test_get_cached_price_passes_tier_through_to_the_key(monkeypatch):
    seen = {}

    def fake_read(key, default=None):
        seen["key"] = key
        return default

    monkeypatch.setattr(price_cache, "read", fake_read)
    price_cache.get_cached_price("ATmega328P", tier="intl")
    assert seen["key"] == "price_cache_intl:atmega328p"


# ---------------------------------------------------------------------
# set_cached_price
# ---------------------------------------------------------------------

def test_set_cached_price_writes_under_the_tiered_key_with_a_timestamp(monkeypatch):
    seen = {}

    def fake_write(key, value):
        seen["key"] = key
        seen["value"] = value

    monkeypatch.setattr(price_cache, "write", fake_write)

    before = time.time()
    price_cache.set_cached_price("ATmega328P", {"listings": [{"price": 5}]}, tier="intl")
    after = time.time()

    assert seen["key"] == "price_cache_intl:atmega328p"
    assert seen["value"]["listings"] == [{"price": 5}]
    assert before <= seen["value"]["_cached_at"] <= after


def test_set_cached_price_does_not_mutate_the_caller_supplied_dict(monkeypatch):
    monkeypatch.setattr(price_cache, "write", lambda key, value: None)
    original = {"listings": []}
    price_cache.set_cached_price("part", original)
    assert "_cached_at" not in original
