"""
tests/manual/ is explicitly the "hits real APIs, run by hand" tier (see
Master Guide §4 B1). The parent tests/conftest.py's `fake_bus` fixture is
autouse and would otherwise swap memory.bus.redis for FakeRedis here too
— which would silently defeat the whole point of these tests. This local
override replaces it with a no-op so anything under tests/manual/ keeps
talking to real Redis/real providers, same as before the B1 reorg.

If a manual test *specifically* wants isolation for one run, import
FakeRedis from tests.conftest directly inside that test instead of
relying on this fixture.

Same override, same reason, for tests/conftest.py's `_no_real_web_search`
autouse fixture: tests/manual/ is explicitly the tier that hits real
Tavily/Exa on purpose (test_capability_coverage.py etc.), so it must keep
talking to the real utils.web_search.search(), not the mocked one every
other test gets by default.
"""
import pytest


@pytest.fixture(autouse=True)
def fake_bus():
    yield None


@pytest.fixture(autouse=True)
def _no_real_web_search():
    yield None
