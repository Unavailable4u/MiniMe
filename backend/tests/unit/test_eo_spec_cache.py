"""
tests/unit/test_eo_spec_cache.py — Patch 7e-2.

eo/spec_cache.py had zero test coverage before this. Thin, but the TTL
math and the key-slugging are exactly the kind of thing that silently
regresses (an off-by-something in the TTL comparison either serves a
stale part forever, or never serves the cache at all -- defeating the
whole point of this module, which exists purely to keep DigiKey/Mouser
quota usage down).

Isolation: spec_cache.py does `from memory.bus import read, write`
(bound names in its own namespace), so tests patch them on the
spec_cache module object, not on memory.bus -- same gotcha
conftest.py documents for generate_text / test_eo_tags.py documents
for chat_workspace.
"""
import time

from eo import spec_cache

# ---------------------------------------------------------------------
# _key
# ---------------------------------------------------------------------

def test_key_slugifies_part_number_and_prefixes_namespace():
    assert spec_cache._key("ESP32-WROOM-32") == "spec_cache:esp32_wroom_32"


def test_key_strips_surrounding_whitespace_and_lowercases():
    assert spec_cache._key("  Some Part  ") == spec_cache._key("some part")


# ---------------------------------------------------------------------
# get_cached_spec
# ---------------------------------------------------------------------

def test_get_cached_spec_returns_none_on_a_miss(monkeypatch):
    monkeypatch.setattr(spec_cache, "read", lambda key, default=None: default)
    assert spec_cache.get_cached_spec("ESP32") is None


def test_get_cached_spec_returns_none_when_entry_is_falsy(monkeypatch):
    """read() returning an explicit falsy value (empty dict, not just
    None) must also be treated as a miss -- `if not entry` covers both,
    pinned so a future switch to `is None` doesn't silently start
    treating {} as a hit."""
    monkeypatch.setattr(spec_cache, "read", lambda key, default=None: {})
    assert spec_cache.get_cached_spec("ESP32") is None


def test_get_cached_spec_returns_entry_when_still_within_ttl(monkeypatch):
    entry = {"dimensions_mm": [10, 5, 2], "_cached_at": time.time() - 10}
    monkeypatch.setattr(spec_cache, "read", lambda key, default=None: entry)
    assert spec_cache.get_cached_spec("ESP32") == entry


def test_get_cached_spec_returns_none_once_past_the_180_day_ttl(monkeypatch):
    stale_entry = {
        "dimensions_mm": [10, 5, 2],
        "_cached_at": time.time() - spec_cache.TTL_SECONDS - 1,
    }
    monkeypatch.setattr(spec_cache, "read", lambda key, default=None: stale_entry)
    assert spec_cache.get_cached_spec("ESP32") is None


def test_get_cached_spec_entry_with_no_cached_at_field_is_treated_as_expired(monkeypatch):
    """entry.get("_cached_at", 0) defaulting to 0 means any entry
    missing that field reads as maximally stale (time.time() - 0 is
    always > TTL_SECONDS) rather than raising a KeyError or -- worse --
    being treated as freshly cached."""
    monkeypatch.setattr(spec_cache, "read", lambda key, default=None: {"dimensions_mm": [1]})
    assert spec_cache.get_cached_spec("ESP32") is None


# ---------------------------------------------------------------------
# set_cached_spec
# ---------------------------------------------------------------------

def test_set_cached_spec_writes_under_the_slugified_key_with_a_timestamp(monkeypatch):
    seen = {}

    def fake_write(key, value):
        seen["key"] = key
        seen["value"] = value

    monkeypatch.setattr(spec_cache, "write", fake_write)

    before = time.time()
    spec_cache.set_cached_spec("ESP32-WROOM-32", {"dimensions_mm": [18, 25.5, 3.1]})
    after = time.time()

    assert seen["key"] == "spec_cache:esp32_wroom_32"
    assert seen["value"]["dimensions_mm"] == [18, 25.5, 3.1]
    assert before <= seen["value"]["_cached_at"] <= after


def test_set_cached_spec_does_not_mutate_the_caller_supplied_dict(monkeypatch):
    """{**result, "_cached_at": ...} must build a new dict -- a caller
    reusing `result` after this call shouldn't see a _cached_at key it
    never added itself."""
    monkeypatch.setattr(spec_cache, "write", lambda key, value: None)
    original = {"dimensions_mm": [1, 2, 3]}
    spec_cache.set_cached_spec("part", original)
    assert "_cached_at" not in original
