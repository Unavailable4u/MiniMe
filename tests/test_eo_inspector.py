"""
tests/test_eo_inspector.py — Part 8.4 (fixture set) + Part 11 (testing plan)
of the v5 Master Blueprint.

Three layers, cheapest/most-deterministic first:

1. Schema validation (`_validate`) — pure unit tests, no network, always run.
2. Fallback-chain engagement — mocks the provider clients so a "bad key on
   the primary" scenario is reproducible without needing an actually-bad
   real key or real network access. Always run.
3. Live fixtures (Part 8.4's actual task list) — real classify() calls
   against real providers. Skipped automatically if EO_INSPECTOR_GROQ_KEY
   isn't set, since these need real credentials and real network access,
   and their point is calibrating against real model behavior, not CI
   hygiene. Run these yourself once your .env has real keys:

    python -m pytest tests/test_eo_inspector.py -v -s
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import eo.inspector as inspector

# ---------------------------------------------------------------------------
# 1. Schema validation — no network.
# ---------------------------------------------------------------------------

def test_valid_tier0_passes():
    result = inspector._validate({
        "tier": 0, "directed_task_type": None, "confidence": 0.9,
        "suggested_agents": ["responder"], "reasoning": "trivial",
    })
    assert result["tier"] == 0


def test_valid_tier2_with_directed_task_type_passes():
    result = inspector._validate({
        "tier": 2, "directed_task_type": "debug", "confidence": 0.8,
        "suggested_agents": ["reviewer", "fixer_pool"], "reasoning": "bug report",
    })
    assert result["directed_task_type"] == "debug"


def test_invalid_tier_rejected():
    try:
        inspector._validate({"tier": 7, "directed_task_type": None,
                              "confidence": 0.9, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_directed_task_type_without_tier2_rejected():
    # This is the "tier says 1 but directed_task_type is set anyway"
    # inconsistency — must be surfaced, not silently resolved either way.
    try:
        inspector._validate({"tier": 1, "directed_task_type": "debug",
                              "confidence": 0.9, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_invalid_directed_task_type_rejected():
    try:
        inspector._validate({"tier": 2, "directed_task_type": "make_coffee",
                              "confidence": 0.9, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_out_of_range_confidence_rejected():
    try:
        inspector._validate({"tier": 0, "directed_task_type": None,
                              "confidence": 1.4, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_non_list_suggested_agents_rejected():
    try:
        inspector._validate({"tier": 0, "directed_task_type": None,
                              "confidence": 0.9, "suggested_agents": "responder", "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# 2. Fallback-chain engagement — mocked providers, no real network/keys.
# ---------------------------------------------------------------------------

class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})


class _FakeResponse:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


def _make_fake_rate_limit_error():
    """Builds a real, correctly-constructed groq.RateLimitError — the SDK's
    __init__ dereferences response.request, so a bare `response=None`
    blows up in the exception's own constructor rather than testing
    anything about our fallback logic."""
    import httpx
    from groq import RateLimitError
    request = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    response = httpx.Response(status_code=429, request=request)
    return RateLimitError("simulated rate limit", response=response, body=None)


class _FakeFailingClient:
    """Simulates a provider whose primary key is bad / rate-limited."""
    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                raise _make_fake_rate_limit_error()


class _FakeWorkingClient:
    """Simulates the fallback provider succeeding with a valid classification."""
    GOOD_JSON = (
        '{"tier": 1, "directed_task_type": null, "confidence": 0.82, '
        '"suggested_agents": ["prompt_writer_lean", "code_writer_1worker"], '
        '"reasoning": "small single-file script"}'
    )

    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                return _FakeResponse(_FakeWorkingClient.GOOD_JSON)


def test_fallback_chain_engages_when_primary_provider_fails(monkeypatch):
    import utils.llm_client as llm_client

    def fake_get_groq(key_env):
        return _FakeFailingClient()

    def fake_get_github(key_env):
        return _FakeWorkingClient()

    monkeypatch.setitem(llm_client._PROVIDER_GETTERS, "groq", fake_get_groq)
    monkeypatch.setitem(llm_client._PROVIDER_GETTERS, "github", fake_get_github)

    result = inspector.classify("write a small script that reverses a string")
    assert result["tier"] == 1
    assert result["confidence"] == 0.82


def test_raises_when_every_provider_in_chain_fails(monkeypatch):
    import utils.llm_client as llm_client

    def fake_get_groq(key_env):
        return _FakeFailingClient()

    def fake_get_github(key_env):
        return None  # simulates key_env not set at all

    monkeypatch.setitem(llm_client._PROVIDER_GETTERS, "groq", fake_get_groq)
    monkeypatch.setitem(llm_client._PROVIDER_GETTERS, "github", fake_get_github)

    try:
        inspector.classify("anything")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# 3. Live fixtures, Part 8.4 — real network/keys, skipped if unavailable.
# ---------------------------------------------------------------------------

FIXTURES = [
    # (task_text, expected_tier, note)
    ("What's the difference between a list and a tuple in Python?", 0,
     "obviously trivial — factual question"),
    ("Write a small Python script that reverses a string from stdin", 1,
     "obviously small build — single file"),
    ("There's a bug where the login form accepts empty passwords, fix it", 2,
     "directed: debug"),
    ("Review the auth module for issues, don't change anything", 2,
     "directed: review"),
    ("Add unit tests for the payment module", 2,
     "directed: add_tests"),
    ("Refactor the user service to remove duplicated validation logic", 2,
     "directed: refactor"),
    ("Run a security scan on the dependencies", 2,
     "directed: security_scan"),
    ("Write documentation for the API endpoints", 2,
     "directed: write_docs"),
    ("Explain what the task_repository module does", 2,
     "directed: explain_code"),
    ("Just make me a todo app with users, auth, and persistence", 3,
     "sounds casual but implies multi-file/multi-module scope — the "
     "under-routing case Part 8.4 flags to test carefully"),
    ("Build and keep improving a full recipe-sharing app", 3,
     "obviously tier 3 — ongoing multi-cycle project"),
]

_HAS_REAL_KEY = bool(os.getenv("EO_INSPECTOR_GROQ_KEY_1"))


def test_live_fixtures():
    if not _HAS_REAL_KEY:
        print("\n  SKIPPED (no EO_INSPECTOR_GROQ_KEY_1 set — set it in .env "
              "and rerun with -s to see live classification results)")
        return

    correct = 0
    for task_text, expected_tier, note in FIXTURES:
        result = inspector.classify(task_text)
        got = result["tier"]
        status = "OK" if got == expected_tier else "MISS"
        if got == expected_tier:
            correct += 1
        print(f"  [{status}] expected={expected_tier} got={got} "
              f"conf={result['confidence']:.2f} :: {note}")
    print(f"\n  {correct}/{len(FIXTURES)} fixtures matched expected tier "
          f"(informational — use this to calibrate the 0.75 threshold per "
          f"Part 8.3, not as a pass/fail gate)")


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            # monkeypatch-dependent tests need pytest; skip them in the
            # standalone runner rather than crashing on a missing fixture.
            import inspect
            if "monkeypatch" in inspect.signature(t).parameters:
                print(f"  SKIP  {t.__name__} (needs pytest's monkeypatch — run via pytest)")
                continue
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
