"""
tests/unit/test_seed_bootstrap.py — rebuilt around the current
eo/registry.py. The store's shape widened (Part 2 §2.2) from bare
{role_name: brief_string} to {role_name: {brief, source, updated_at,
times_hired, pinned, pinned_at}}, and reads now go through a per-run
ContextVar cache (Performance Audit §5A) — see the local
`reset_role_prompts_cache` fixture below, which clears that cache
before every test in this file so one test's bootstrap can never leak
into the next test's "should it reseed or just read?" assertion.
"""
import pytest

import eo.registry as registry
from memory.bus import read, write
from eo.registry import get_role_prompt, get_role_metadata, ROLE_PROMPTS_KEY, ROLE_PROMPTS_SEED


@pytest.fixture(autouse=True)
def reset_role_prompts_cache():
    # _load_prompts() caches the whole store per store-key in a
    # ContextVar for the life of one request/run (see registry.py's
    # Performance Audit §5A comment). Tests run sequentially in the
    # same thread/context, so without this reset, a later test would
    # see an earlier test's already-cached result instead of hitting
    # FakeRedis fresh -- exactly the kind of cross-test leakage the
    # ContextVar's own design intentionally avoids across real
    # requests.
    registry._role_prompts_cache_ctx.set(None)
    yield
    registry._role_prompts_cache_ctx.set(None)


def test_first_call_bootstraps_from_seed_and_persists_it():
    assert read(ROLE_PROMPTS_KEY) is None  # nothing written yet this test

    result = get_role_prompt("implementer")
    assert result == ROLE_PROMPTS_SEED["implementer"]

    raw_after_first = read(ROLE_PROMPTS_KEY)
    assert sorted(raw_after_first.keys()) == sorted(ROLE_PROMPTS_SEED.keys())
    # Part 2 §2.2 schema widening: bootstrap wraps each seed brief into
    # the richer {brief, source, ...} shape, not a bare string.
    entry = raw_after_first["implementer"]
    assert entry["brief"] == ROLE_PROMPTS_SEED["implementer"]
    assert entry["source"] == "seed"
    assert entry["times_hired"] == 0
    assert entry["pinned"] is False


def test_second_call_just_reads_without_reseeding():
    result1 = get_role_prompt("implementer")
    raw_after_first = dict(read(ROLE_PROMPTS_KEY))  # snapshot before 2nd call

    registry._role_prompts_cache_ctx.set(None)  # force a fresh bus read
    result2 = get_role_prompt("implementer")

    assert result1 == result2 == ROLE_PROMPTS_SEED["implementer"]
    assert read(ROLE_PROMPTS_KEY) == raw_after_first  # unchanged, no reseed


def test_get_role_prompt_returns_none_for_a_role_with_no_brief():
    assert get_role_prompt("some_role_never_briefed_xyz") is None


def test_get_role_metadata_returns_the_full_object_not_just_the_brief():
    get_role_prompt("implementer")  # trigger bootstrap
    meta = get_role_metadata("implementer")
    assert meta["brief"] == ROLE_PROMPTS_SEED["implementer"]
    assert set(meta.keys()) >= {"brief", "source", "updated_at", "times_hired", "pinned", "pinned_at"}


def test_legacy_bare_string_entries_get_migrated_on_read():
    # Simulate a pre-migration store: bare {role_name: brief_string}.
    write(ROLE_PROMPTS_KEY, {"implementer": ROLE_PROMPTS_SEED["implementer"],
                              "custom_role": "a hand-written legacy brief"})
    registry._role_prompts_cache_ctx.set(None)

    assert get_role_prompt("custom_role") == "a hand-written legacy brief"

    raw = read(ROLE_PROMPTS_KEY)
    # Both entries migrated to the object shape in the same pass.
    assert raw["custom_role"]["brief"] == "a hand-written legacy brief"
    # Byte-for-byte matches the current seed value -> tagged "seed",
    # honestly reflecting how it actually got there.
    assert raw["implementer"]["source"] == "seed"
    # A genuinely different string -> tagged "panel_brief_writer", the
    # only other way a bare string could have landed pre-migration.
    assert raw["custom_role"]["source"] == "panel_brief_writer"
