"""
tests/unit/test_eo_research_cache.py — Patch 7e-2.

eo/research_cache.py had zero test coverage before this. Its whole
reason to exist is the per-scope TTL table (a "reddit search for X"
and a "news search for X" must cache independently and expire at very
different rates) -- these tests pin the TTL-by-scope lookup, the
DEFAULT_TTL fallback for an unlisted scope, and the scope-qualified
key shape, which is the part most likely to silently rot as new scopes
get added (per the module's own TTL_BY_SCOPE comment).

Isolation: research_cache.py does `from memory.bus import read, write,
slugify` (bound names in its own namespace) -- tests patch `read`/
`write` on the research_cache module object, same gotcha as every
other cache module in this batch. `slugify` is left un-mocked and
exercised for real (it's pure, deterministic, and already covered
directly in tests/unit/test_memory_bus.py), so a query-slug assertion
below matches what memory.bus.slugify() actually produces rather than
a hand-picked stand-in.

Unlike price_cache.py/spec_cache.py, this module relies on write()'s
native `ex` (TTL) param instead of hand-rolling its own `_cached_at`
staleness check (see the module's own docstring for why) -- so there's
no separate "read confirms it's expired" test here the way the other
cache modules have one; the ONLY behavior worth pinning on the read
side is that get_cached_research() passes the key straight through
to read(), which the "looks up by scope-qualified key" test below
covers directly.
"""
from eo import research_cache

# ---------------------------------------------------------------------
# _key
# ---------------------------------------------------------------------

def test_key_includes_scope_and_slugified_query():
    assert research_cache._key("news", "Fed rate decision") == \
        "research_cache:news:fed_rate_decision"


def test_key_differs_by_scope_for_the_same_query():
    """Same query text under two different scopes must produce two
    distinct keys -- the entire point of keying `scope:query_slug`
    instead of just `query_slug`."""
    news_key = research_cache._key("news", "ESP32 pinout")
    forum_key = research_cache._key("forum", "ESP32 pinout")
    assert news_key != forum_key


# ---------------------------------------------------------------------
# get_cached_research
# ---------------------------------------------------------------------

def test_get_cached_research_returns_none_on_a_miss(monkeypatch):
    monkeypatch.setattr(research_cache, "read", lambda key, default=None: default)
    assert research_cache.get_cached_research("news", "some query") is None


def test_get_cached_research_returns_the_stored_result_on_a_hit(monkeypatch):
    stored = {"results": [{"title": "Fed holds rates steady"}]}
    monkeypatch.setattr(research_cache, "read", lambda key, default=None: stored)
    assert research_cache.get_cached_research("news", "fed rates") == stored


def test_get_cached_research_looks_up_by_the_scope_qualified_key(monkeypatch):
    seen = {}

    def fake_read(key, default=None):
        seen["key"] = key
        return default

    monkeypatch.setattr(research_cache, "read", fake_read)
    research_cache.get_cached_research("academic", "graphene synthesis")

    assert seen["key"] == research_cache._key("academic", "graphene synthesis")


# ---------------------------------------------------------------------
# set_cached_research — per-scope TTL selection
# ---------------------------------------------------------------------

def test_set_cached_research_uses_the_matching_scope_ttl(monkeypatch):
    seen = {}

    def fake_write(key, value, ex=None):
        seen["key"] = key
        seen["value"] = value
        seen["ex"] = ex

    monkeypatch.setattr(research_cache, "write", fake_write)
    research_cache.set_cached_research("news", "fed rates", {"results": []})

    assert seen["ex"] == research_cache.TTL_BY_SCOPE["news"]


def test_set_cached_research_forum_scope_uses_its_own_ttl_not_news(monkeypatch):
    """Regression-style pin: forum (12h) and news (2h) must not be
    conflated -- a lookup bug that always resolved to the first/last
    dict entry would still pass a test that only checks one scope."""
    seen = {}
    monkeypatch.setattr(research_cache, "write",
                         lambda key, value, ex=None: seen.update({"ex": ex}))
    research_cache.set_cached_research("forum", "some thread", {"results": []})
    assert seen["ex"] == 60 * 60 * 12
    assert seen["ex"] != research_cache.TTL_BY_SCOPE["news"]


def test_set_cached_research_unlisted_scope_falls_back_to_default_ttl(monkeypatch):
    """A scope name not present in TTL_BY_SCOPE at all (e.g. a future
    scope added to web_researcher.py before this table is updated)
    must fall back to DEFAULT_TTL rather than raising a KeyError or
    silently caching with no expiry."""
    seen = {}
    monkeypatch.setattr(research_cache, "write",
                         lambda key, value, ex=None: seen.update({"ex": ex}))
    research_cache.set_cached_research("some_future_scope", "query", {"results": []})
    assert seen["ex"] == research_cache.DEFAULT_TTL


def test_set_cached_research_passes_result_through_unmodified(monkeypatch):
    """Unlike price_cache.py/spec_cache.py, this module relies on
    write()'s native `ex` param instead of stamping its own
    `_cached_at` into the value -- result must be written as-is, with
    no extra key injected."""
    seen = {}
    monkeypatch.setattr(research_cache, "write",
                         lambda key, value, ex=None: seen.update({"value": value}))
    result = {"results": [{"title": "some article"}], "fetched_via": "hn_search"}
    research_cache.set_cached_research("hackernews", "some query", result)
    assert seen["value"] == result


def test_set_cached_research_writes_under_the_scope_qualified_key(monkeypatch):
    seen = {}
    monkeypatch.setattr(research_cache, "write",
                         lambda key, value, ex=None: seen.update({"key": key}))
    research_cache.set_cached_research("academic", "graphene synthesis", {"results": []})
    assert seen["key"] == research_cache._key("academic", "graphene synthesis")
