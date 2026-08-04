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
"""
import pytest


@pytest.fixture(autouse=True)
def fake_bus():
    yield None
