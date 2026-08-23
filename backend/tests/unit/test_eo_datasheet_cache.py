"""
tests/unit/test_eo_datasheet_cache.py — Patch 7e-2.

eo/datasheet_cache.py had zero test coverage before this. Structurally
almost identical to spec_cache.py (same 180-day TTL reasoning), with
one meaningful difference worth its own coverage: it's keyed by a
sha256 hash of the datasheet_url rather than a slugified part number,
specifically because a raw URL isn't safe/short key material -- these
tests pin that hashing (not slugifying) is what actually happens here.

Isolation: same bound-name gotcha as spec_cache.py/price_cache.py --
datasheet_cache.py does `from memory.bus import read, write`, so tests
patch `read`/`write` on the datasheet_cache module object.
"""
import hashlib
import time

import eo.datasheet_cache as datasheet_cache


# ---------------------------------------------------------------------
# _key
# ---------------------------------------------------------------------

def test_key_is_a_sha256_hash_of_the_url_not_a_slugified_string():
    url = "https://vendor.example.com/datasheets/ATMEGA328P-PU.pdf"
    expected_digest = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]
    assert datasheet_cache._key(url) == f"datasheet_cache:{expected_digest}"


def test_key_strips_surrounding_whitespace_before_hashing():
    url = "https://vendor.example.com/ds.pdf"
    assert datasheet_cache._key(f"  {url}  ") == datasheet_cache._key(url)


def test_key_is_stable_and_deterministic_across_calls():
    url = "https://vendor.example.com/ds.pdf"
    assert datasheet_cache._key(url) == datasheet_cache._key(url)


def test_key_differs_for_different_urls():
    key_a = datasheet_cache._key("https://vendor.example.com/a.pdf")
    key_b = datasheet_cache._key("https://vendor.example.com/b.pdf")
    assert key_a != key_b


# ---------------------------------------------------------------------
# get_cached_datasheet
# ---------------------------------------------------------------------

def test_get_cached_datasheet_returns_none_on_a_miss(monkeypatch):
    monkeypatch.setattr(datasheet_cache, "read", lambda key, default=None: default)
    assert datasheet_cache.get_cached_datasheet("https://vendor.example.com/ds.pdf") is None


def test_get_cached_datasheet_returns_none_when_entry_is_falsy(monkeypatch):
    monkeypatch.setattr(datasheet_cache, "read", lambda key, default=None: {})
    assert datasheet_cache.get_cached_datasheet("https://vendor.example.com/ds.pdf") is None


def test_get_cached_datasheet_returns_entry_when_still_within_ttl(monkeypatch):
    entry = {
        "dimensions_mm": [18, 25.5, 3.1],
        "source": "digikey",
        "_cached_at": time.time() - 10,
    }
    monkeypatch.setattr(datasheet_cache, "read", lambda key, default=None: entry)
    assert datasheet_cache.get_cached_datasheet("https://vendor.example.com/ds.pdf") == entry


def test_get_cached_datasheet_returns_none_once_past_the_180_day_ttl(monkeypatch):
    stale_entry = {
        "dimensions_mm": [18, 25.5, 3.1],
        "_cached_at": time.time() - datasheet_cache.TTL_SECONDS - 1,
    }
    monkeypatch.setattr(datasheet_cache, "read", lambda key, default=None: stale_entry)
    assert datasheet_cache.get_cached_datasheet("https://vendor.example.com/ds.pdf") is None


def test_get_cached_datasheet_entry_with_no_cached_at_field_is_treated_as_expired(monkeypatch):
    monkeypatch.setattr(
        datasheet_cache, "read",
        lambda key, default=None: {"dimensions_mm": [1, 2, 3]},
    )
    assert datasheet_cache.get_cached_datasheet("https://vendor.example.com/ds.pdf") is None


def test_get_cached_datasheet_looks_up_by_the_hashed_key(monkeypatch):
    seen = {}
    url = "https://vendor.example.com/ds.pdf"

    def fake_read(key, default=None):
        seen["key"] = key
        return default

    monkeypatch.setattr(datasheet_cache, "read", fake_read)
    datasheet_cache.get_cached_datasheet(url)
    assert seen["key"] == datasheet_cache._key(url)


# ---------------------------------------------------------------------
# set_cached_datasheet
# ---------------------------------------------------------------------

def test_set_cached_datasheet_writes_under_the_hashed_key_with_a_timestamp(monkeypatch):
    seen = {}
    url = "https://vendor.example.com/ds.pdf"

    def fake_write(key, value):
        seen["key"] = key
        seen["value"] = value

    monkeypatch.setattr(datasheet_cache, "write", fake_write)

    before = time.time()
    datasheet_cache.set_cached_datasheet(url, {"dimensions_mm": [1, 2, 3], "source": "mouser"})
    after = time.time()

    assert seen["key"] == datasheet_cache._key(url)
    assert seen["value"]["dimensions_mm"] == [1, 2, 3]
    assert seen["value"]["source"] == "mouser"
    assert before <= seen["value"]["_cached_at"] <= after


def test_set_cached_datasheet_does_not_mutate_the_caller_supplied_dict(monkeypatch):
    monkeypatch.setattr(datasheet_cache, "write", lambda key, value: None)
    original = {"dimensions_mm": [1, 2, 3]}
    datasheet_cache.set_cached_datasheet("https://vendor.example.com/ds.pdf", original)
    assert "_cached_at" not in original
