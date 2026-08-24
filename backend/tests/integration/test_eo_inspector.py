"""
tests/integration/test_eo_inspector.py — Part 8.4 (fixture set) + Part 11
(testing plan) of the v5 Master Blueprint.

Three layers, cheapest/most-deterministic first:

1. Schema validation (`_validate`) — pure unit tests, no network, always run.
2. Fallback-chain engagement — mocks the provider clients so a "bad key on
   the primary" scenario is reproducible without needing an actually-bad
   real key or real network access. Always run.
3. Live fixtures (Part 8.4's actual task list) — real classify() calls
   against real providers. Skipped automatically if EO_INSPECTOR_GROQ_KEY_1
   isn't set, since these need real credentials and real network access,
   and their point is calibrating against real model behavior, not CI
   hygiene. Run these yourself once your .env has real keys:

    python -m pytest tests/integration/test_eo_inspector.py -v -s

Moved from tests/test_eo_inspector.py (B1 audit) and rewritten for two
migrations that landed since the original file was written:

  - Migration Part 12 §8.2/§8.4: output schema is now {path, ...}, not
    {tier, ...} -- "tier" is an int 0-3, "path" is one of "instant"/
    "direct"/"fixed"/"adaptive". Every _validate() fixture and every
    fallback-chain assertion below uses the new schema; the old
    tier-int fixtures fail _validate() outright (KeyError on
    parsed["path"] before the int/str question is even reached).

  - Quota-reality fix §4 (2026-07-30, see eo/inspector.py's own module
    docstring): GitHub Models retired -- utils/llm_client.py's
    _get_github no longer exists at all. The Inspector's live CHAIN is
    now Groq x2 (EO_INSPECTOR_GROQ_KEY_1/_2) -> Gemini x2
    (GEMINI_API_KEY_10/_11), so the fallback-chain tests below mock
    _get_groq and _get_gemini, not _get_groq and _get_github.
"""
import os

from eo import inspector

# ---------------------------------------------------------------------------
# 1. Schema validation — no network.
# ---------------------------------------------------------------------------

def test_valid_instant_path_passes():
    result = inspector._validate({
        "path": "instant", "directed_task_type": None, "confidence": 0.9,
        "suggested_agents": ["responder"], "reasoning": "trivial",
    })
    assert result["path"] == "instant"


def test_valid_fixed_path_with_directed_task_type_passes():
    result = inspector._validate({
        "path": "fixed", "directed_task_type": "debug", "confidence": 0.8,
        "suggested_agents": ["reviewer", "fixer_pool"], "reasoning": "bug report",
    })
    assert result["directed_task_type"] == "debug"


def test_invalid_path_rejected():
    try:
        inspector._validate({"path": "sga", "directed_task_type": None,
                              "confidence": 0.9, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_numeric_tier_instead_of_path_rejected():
    # The pre-migration shape must not silently pass -- "path" is a hard
    # enum check now, same discipline the old "tier" int check had.
    try:
        inspector._validate({"tier": 0, "directed_task_type": None,
                              "confidence": 0.9, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_directed_task_type_without_fixed_path_rejected():
    # This is the "path says 'direct' but directed_task_type is set
    # anyway" inconsistency — must be surfaced, not silently resolved
    # either way.
    try:
        inspector._validate({"path": "direct", "directed_task_type": "debug",
                              "confidence": 0.9, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_invalid_directed_task_type_rejected():
    try:
        inspector._validate({"path": "fixed", "directed_task_type": "make_coffee",
                              "confidence": 0.9, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_out_of_range_confidence_rejected():
    try:
        inspector._validate({"path": "instant", "directed_task_type": None,
                              "confidence": 1.4, "suggested_agents": [], "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_non_list_suggested_agents_rejected():
    try:
        inspector._validate({"path": "instant", "directed_task_type": None,
                              "confidence": 0.9, "suggested_agents": "responder", "reasoning": ""})
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_unrecognized_domain_defaults_to_none_rather_than_rejecting():
    result = inspector._validate({
        "path": "adaptive", "directed_task_type": None, "confidence": 0.9,
        "suggested_agents": ["writer"], "reasoning": "", "domain": "not_a_real_domain",
    })
    assert result["domain"] is None


def test_execution_order_drops_roles_not_in_suggested_agents():
    result = inspector._validate({
        "path": "adaptive", "directed_task_type": None, "confidence": 0.9,
        "suggested_agents": ["implementer"], "reasoning": "",
        "execution_order": ["implementer", "a_role_never_suggested"],
    })
    assert result["execution_order"] == ["implementer"]


# ---------------------------------------------------------------------------
# 2. Fallback-chain engagement — mocked providers, no real network/keys.
# ---------------------------------------------------------------------------

class _FakeChoice:
    def __init__(self, content):
        self.message = type("M", (), {"content": content})
        self.finish_reason = "stop"


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
        '{"path": "direct", "directed_task_type": null, "confidence": 0.82, '
        '"suggested_agents": ["prompt_writer_lean", "code_writer_1worker"], '
        '"reasoning": "small single-file script"}'
    )

    class chat:
        class completions:
            @staticmethod
            def create(**kwargs):
                return _FakeResponse(_FakeWorkingClient.GOOD_JSON)


def test_fallback_chain_engages_when_both_groq_accounts_fail(monkeypatch):
    """CHAIN is Groq KEY_1 -> Groq KEY_2 -> Gemini KEY_10 -> Gemini KEY_11
    now (GitHub Models retired) -- both Groq steps must be exhausted
    before the Gemini fallback engages."""
    from utils import llm_client

    def fake_get_groq(key_env, timeout=None):
        return _FakeFailingClient()

    def fake_get_gemini(key_env, timeout=None):
        return _FakeWorkingClient()

    monkeypatch.setattr(llm_client, "_get_groq", fake_get_groq)
    monkeypatch.setattr(llm_client, "_get_gemini", fake_get_gemini)
    monkeypatch.setenv("EO_INSPECTOR_GROQ_KEY_1", "fake")
    monkeypatch.setenv("EO_INSPECTOR_GROQ_KEY_2", "fake")
    monkeypatch.setenv("GEMINI_API_KEY_10", "fake")
    monkeypatch.setenv("GEMINI_API_KEY_11", "fake")

    result = inspector.classify("write a small script that reverses a string")
    assert result["path"] == "direct"
    assert result["confidence"] == 0.82


def test_raises_when_every_provider_in_chain_fails(monkeypatch):
    from utils import llm_client

    def fake_get_groq(key_env, timeout=None):
        return _FakeFailingClient()

    def fake_get_gemini(key_env, timeout=None):
        return None  # simulates key_env not set at all

    monkeypatch.setattr(llm_client, "_get_groq", fake_get_groq)
    monkeypatch.setattr(llm_client, "_get_gemini", fake_get_gemini)
    monkeypatch.setenv("EO_INSPECTOR_GROQ_KEY_1", "fake")
    monkeypatch.setenv("EO_INSPECTOR_GROQ_KEY_2", "fake")

    try:
        inspector.classify("anything")
        assert False, "expected RuntimeError"
    except RuntimeError:
        pass


# ---------------------------------------------------------------------------
# 3. Live fixtures, Part 8.4 — real network/keys, skipped if unavailable.
# ---------------------------------------------------------------------------

FIXTURES = [
    # (task_text, expected_path, note)
    ("What's the difference between a list and a tuple in Python?", "instant",
     "obviously trivial — factual question"),
    ("Write a small Python script that reverses a string from stdin", "direct",
     "obviously small build — single file"),
    ("There's a bug where the login form accepts empty passwords, fix it", "fixed",
     "directed: debug"),
    ("Review the auth module for issues, don't change anything", "fixed",
     "directed: review"),
    ("Add unit tests for the payment module", "fixed",
     "directed: add_tests"),
    ("Refactor the user service to remove duplicated validation logic", "fixed",
     "directed: refactor"),
    ("Run a security scan on the dependencies", "fixed",
     "directed: security_scan"),
    ("Write documentation for the API endpoints", "fixed",
     "directed: write_docs"),
    ("Explain what the task_repository module does", "fixed",
     "directed: explain_code"),
    ("Just make me a todo app with users, auth, and persistence", "adaptive",
     "sounds casual but implies multi-file/multi-module scope — the "
     "under-routing case Part 8.4 flags to test carefully"),
    ("Build and keep improving a full recipe-sharing app", "adaptive",
     "obviously adaptive — ongoing multi-cycle project"),
]

_HAS_REAL_KEY = bool(os.getenv("EO_INSPECTOR_GROQ_KEY_1"))


def test_live_fixtures():
    if not _HAS_REAL_KEY:
        print("\n  SKIPPED (no EO_INSPECTOR_GROQ_KEY_1 set — set it in .env "
              "and rerun with -s to see live classification results)")
        return

    correct = 0
    for task_text, expected_path, note in FIXTURES:
        result = inspector.classify(task_text)
        got = result["path"]
        status = "OK" if got == expected_path else "MISS"
        if got == expected_path:
            correct += 1
        print(f"  [{status}] expected={expected_path} got={got} "
              f"conf={result['confidence']:.2f} :: {note}")
    print(f"\n  {correct}/{len(FIXTURES)} fixtures matched expected path "
          f"(informational — use this to calibrate the 0.75 threshold per "
          f"Part 8.3, not as a pass/fail gate)")
