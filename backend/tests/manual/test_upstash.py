"""
tests/manual/test_upstash.py — moved from tests/test_upstash.py (B1
manual-tier migration). Hits the real Upstash Redis REST endpoint with
UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN; not run in CI. Note
this package's conftest.py disables the autouse fake_bus fixture for
exactly this reason -- this test wants the real Redis, not FakeRedis.
"""
import os

import pytest
from dotenv import load_dotenv
from upstash_redis import Redis

load_dotenv()

pytestmark = pytest.mark.manual


@pytest.mark.skipif(
    not (os.getenv("UPSTASH_REDIS_REST_URL") and os.getenv("UPSTASH_REDIS_REST_TOKEN")),
    reason="UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not set",
)
def test_upstash_set_and_get_round_trip():
    redis = Redis(
        url=os.getenv("UPSTASH_REDIS_REST_URL"),
        token=os.getenv("UPSTASH_REDIS_REST_TOKEN"),
    )

    redis.set("test_key", "hello from ai_loop")
    value = redis.get("test_key")
    assert value == "hello from ai_loop"
    print("Value read back:", value)
