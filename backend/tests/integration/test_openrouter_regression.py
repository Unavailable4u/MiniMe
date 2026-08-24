"""
tests/integration/test_openrouter_regression.py — OR-6
(reliability_overhaul_plan.md).

3f-7 was manual verification ("not a code change") against the two
now-deduplicated generate_text() branches, covering all 5 ErrorBucket
buckets on both providers live at the time. OR-6 is that same regression
pass, re-run against chains that use "openrouter" instead of "cerebras"
(now fully retired -- see OR-4's test_all_keys.py/test_openrouter.py
swap), automated instead of hand-verified, since the shared step-runner
(_run_chain_step()/_handle_transient_error(), from 3f-5) is real,
unmodified production code and there's no reason this has to stay manual.

What this confirms, against the REAL utils/llm_client.py code (nothing
about _run_chain_step/_handle_transient_error/classify_error is
reimplemented or stubbed here -- only the provider client at the very
edge, _get_openrouter(), is faked):

  1. PERMANENT_AUTH  on an openrouter step -> cooldown written, falls
     through to the next chain step.
  2. TRANSIENT_NETWORK on an openrouter step -> falls through, no
     cooldown written.
  3. MALFORMED_REQUEST on an openrouter step -> raises, does NOT fall
     through (never masked as a retryable failure).
  4. RATE_LIMIT_WINDOW  on an openrouter step, no headroom anywhere else
     in the chain -> ledger-derived cooldown written, bounded sleep,
     retries the SAME step in place, succeeds.
  5. CONTEXT_LENGTH_EXCEEDED on an openrouter step -> prompt shrunk,
     retries the SAME step in place, succeeds.
  6. Success path -> usage logged and correctly tagged provider="openrouter".

Previous pass on this file got RATE_LIMIT_WINDOW/CONTEXT_LENGTH_EXCEEDED
hanging because a throwaway manual harness let the real time.sleep()
inside _ledger_gate()/_handle_transient_error() actually fire. Fixed here
by monkeypatching utils.llm_client.time.sleep for every test in this
file -- deterministic and instant, same as everything else under
tests/integration already does for anything that would otherwise touch
real infra or real wall-clock time.

Run standalone:
    python -m pytest tests/integration/test_openrouter_regression.py -v
"""
from datetime import UTC, date, datetime

import pytest

from utils import llm_client
from utils.llm_client import OpenAIAPIStatusError

TODAY = date.today().isoformat()


# ---------------------------------------------------------------------------
# 0. Shared fakes.
# ---------------------------------------------------------------------------

class _FakeBus:
    """Same in-memory stand-in tests/integration/test_usage_logging.py
    uses -- avoids needing real Upstash credentials, and lets each test
    assert directly on what got written."""
    def __init__(self):
        self.store = {}

    def read(self, key, default=None):
        return self.store.get(key, default)

    def write(self, key, value):
        self.store[key] = value


class _FakeEmitter:
    def __init__(self):
        self.calls = []

    def __call__(self, event_type, session_id=None, agent=None, tier=None, payload=None):
        self.calls.append({
            "event_type": event_type, "session_id": session_id,
            "agent": agent, "tier": tier, "payload": payload,
        })
        return True


class _FakeProviderError(OpenAIAPIStatusError):
    """A duck-typed provider exception: a real instance of
    OpenAIAPIStatusError (so it's caught by llm_client._TRANSIENT_ERRORS
    -- OpenRouter rides the OpenAI SDK, same as mistral/gemini/
    huggingface), built without needing a real httpx response object.
    Carries exactly what classify_error()/_status_code_from_exc()/
    _body_text() actually read off an exception (status_code, body,
    response), so classification runs through the real, unmodified
    classify_error() rather than being pre-decided here."""
    def __init__(self, message, status_code=None, body_text=""):
        Exception.__init__(self, message)  # bypass the real SDK __init__,
        # which requires a real httpx response object we have no reason
        # to construct for a unit-level test of the dispatch logic.
        self.status_code = status_code
        self.body = body_text
        self.response = None


class _FakeUsage:
    def __init__(self, total_tokens):
        self.total_tokens = total_tokens


class _FakeChoice:
    def __init__(self, content, finish_reason):
        self.message = type("M", (), {"content": content})
        self.finish_reason = finish_reason


class _FakeChatResponse:
    def __init__(self, content, total_tokens, finish_reason):
        self.choices = [_FakeChoice(content, finish_reason)]
        self.usage = _FakeUsage(total_tokens)


class _FakeRawResponse:
    """Matches what _call_step() actually reads off
    client.chat.completions.with_raw_response.create(...)'s return
    value -- a .headers mapping and a .parse() method, NOT a plain
    ChatCompletion object (that's the .create() shape, one level up)."""
    def __init__(self, parsed, headers=None):
        self._parsed = parsed
        self.headers = headers or {}

    def parse(self):
        return self._parsed


class _ScriptedOpenRouterClient:
    """Fake client for the exact call path _call_step() uses for every
    OpenAI-SDK-shaped provider including openrouter:
    client.chat.completions.with_raw_response.create(**kwargs).

    `script` is a list consumed one item per call to create() -- one
    item per attempt _run_chain_step()'s retry loop actually makes.
    Each item is either an exception instance (raised) or a
    (content, finish_reason, total_tokens) tuple (returned as a
    successful raw response)."""
    def __init__(self, script):
        self._script = list(script)
        self.call_count = 0
        outer = self

        class _WithRawResponse:
            @staticmethod
            def create(**kwargs):
                outer.call_count += 1
                if not outer._script:
                    raise AssertionError(
                        f"_ScriptedOpenRouterClient ran out of scripted "
                        f"responses on call #{outer.call_count} -- the "
                        f"real retry/fallback logic made more attempts "
                        f"than this test scripted for.")
                item = outer._script.pop(0)
                if isinstance(item, BaseException):
                    raise item
                content, finish_reason, total_tokens = item
                return _FakeRawResponse(_FakeChatResponse(content, total_tokens, finish_reason))

        class _Completions:
            with_raw_response = _WithRawResponse()

        class _Chat:
            completions = _Completions()

        self.chat = _Chat()


@pytest.fixture
def harness(monkeypatch):
    """Wires up everything every test below needs: fake bus/emitter (so
    usage logging and cooldown writes are inspectable, not hitting real
    Upstash), an always-True ledger pre-flight gate (so each test
    exercises the target ErrorBucket's dispatch logic specifically,
    not Phase 2/3f-1's separate pre-flight gating -- that's this
    fixture's job to neutralize, not this file's job to re-test), and
    deterministic time.sleep (so RATE_LIMIT_WINDOW's real, bounded sleep
    doesn't actually block the test suite -- this is exactly what hung
    the previous pass at this file).

    Returns a dict of everything a test might want to assert against or
    configure further: fake_bus, fake_emitter, sleep_calls, and
    make_client(script) to build a scripted fake client for a given
    OPENROUTER_API_KEY_* slot.
    """
    fake_bus = _FakeBus()
    fake_emitter = _FakeEmitter()
    monkeypatch.setattr(llm_client, "bus_read", fake_bus.read)
    monkeypatch.setattr(llm_client, "bus_write", fake_bus.write)
    monkeypatch.setattr(llm_client, "emit_event", fake_emitter)

    # Phase 2/3f-1 pre-flight gate neutralized -- see docstring above.
    monkeypatch.setattr(llm_client.rate_ledger, "can_proceed", lambda *a, **k: (True, 0.0))

    sleep_calls = []
    monkeypatch.setattr(llm_client.time, "sleep", lambda secs: sleep_calls.append(secs))

    clients = {}

    def make_client(key_env, script):
        # _remaining_chain_headroom() (Phase 3b's reroute-vs-wait look-ahead)
        # checks os.getenv(step["key_env"]) directly to decide whether a
        # later chain step is even eligible, independent of the patched
        # _get_openrouter() below -- a key with no env var set is treated
        # as "not configured" and skipped from consideration entirely, same
        # as production would skip a genuinely-unset key. Set a dummy value
        # so a scripted key is treated as configured, matching what
        # _get_openrouter() itself will actually return for it.
        monkeypatch.setenv(key_env, "test-value")
        clients[key_env] = _ScriptedOpenRouterClient(script)
        return clients[key_env]

    def _get_openrouter(key_env, timeout=None):
        return clients.get(key_env)

    monkeypatch.setattr(llm_client, "_get_openrouter", _get_openrouter)

    return {
        "fake_bus": fake_bus,
        "fake_emitter": fake_emitter,
        "sleep_calls": sleep_calls,
        "make_client": make_client,
    }


def _chain_step(key_env, model="openrouter/free"):
    return {"provider": "openrouter", "model": model, "key_env": key_env}


# ---------------------------------------------------------------------------
# 1. Success path -- usage logged, correctly tagged provider="openrouter".
# ---------------------------------------------------------------------------

def test_success_path_logs_usage_tagged_openrouter(harness):
    harness["make_client"]("OPENROUTER_API_KEY_TEST_1",
                            script=[("hello from openrouter", "stop", 123)])
    chain = [_chain_step("OPENROUTER_API_KEY_TEST_1")]

    result = llm_client.generate_text(
        "system prompt", "user content", chain,
        agent_name="OR-6 Test Agent", session_id="sess_or6", tier=1,
    )

    assert result == "hello from openrouter"
    key = f"usage:openrouter:OPENROUTER_API_KEY_TEST_1:{TODAY}"
    record = harness["fake_bus"].store[key]
    assert record["requests"] == 1
    assert record["tokens"] == 123
    assert record["model"] == "openrouter/free"


# ---------------------------------------------------------------------------
# 2. PERMANENT_AUTH -- cooldown set, falls through to next step.
# ---------------------------------------------------------------------------

def test_permanent_auth_cools_down_and_falls_through(harness):
    bad_key_exc = _FakeProviderError(
        "Incorrect API key provided", status_code=401, body_text="invalid api key")
    harness["make_client"]("OPENROUTER_API_KEY_TEST_1", script=[bad_key_exc])
    harness["make_client"]("OPENROUTER_API_KEY_TEST_2",
                            script=[("fallback succeeded", "stop", 50)])
    chain = [_chain_step("OPENROUTER_API_KEY_TEST_1"),
             _chain_step("OPENROUTER_API_KEY_TEST_2")]

    before = datetime.now(UTC).timestamp()
    result = llm_client.generate_text(
        "system prompt", "user content", chain, agent_name="OR-6 Test Agent")

    assert result == "fallback succeeded"
    cooldown_key = "cooldown_until:openrouter:OPENROUTER_API_KEY_TEST_1"
    assert cooldown_key in harness["fake_bus"].store
    assert harness["fake_bus"].store[cooldown_key] > before  # a real future cooldown, not 0/None
    # The second key must NOT have been cooled down -- it succeeded.
    assert "cooldown_until:openrouter:OPENROUTER_API_KEY_TEST_2" not in harness["fake_bus"].store


# ---------------------------------------------------------------------------
# 3. TRANSIENT_NETWORK -- falls through, no cooldown written.
# ---------------------------------------------------------------------------

def test_transient_network_falls_through_without_cooldown(harness):
    network_exc = _FakeProviderError(
        "Service temporarily unavailable", status_code=503, body_text="upstream error")
    harness["make_client"]("OPENROUTER_API_KEY_TEST_1", script=[network_exc])
    harness["make_client"]("OPENROUTER_API_KEY_TEST_2",
                            script=[("fallback succeeded", "stop", 50)])
    chain = [_chain_step("OPENROUTER_API_KEY_TEST_1"),
             _chain_step("OPENROUTER_API_KEY_TEST_2")]

    result = llm_client.generate_text(
        "system prompt", "user content", chain, agent_name="OR-6 Test Agent")

    assert result == "fallback succeeded"
    # 3e: TRANSIENT_NETWORK is deliberately NOT cooled down -- a single
    # network blip doesn't mean the key/account itself is bad.
    assert "cooldown_until:openrouter:OPENROUTER_API_KEY_TEST_1" not in harness["fake_bus"].store


# ---------------------------------------------------------------------------
# 4. MALFORMED_REQUEST -- raises, does not fall through to next step.
# ---------------------------------------------------------------------------

def test_malformed_request_raises_and_does_not_fall_through(harness):
    bad_request_exc = _FakeProviderError(
        "Invalid request body", status_code=400, body_text="unrecognized field 'foo'")
    client1 = harness["make_client"]("OPENROUTER_API_KEY_TEST_1", script=[bad_request_exc])
    client2 = harness["make_client"]("OPENROUTER_API_KEY_TEST_2",
                                      script=[("should never be reached", "stop", 1)])
    chain = [_chain_step("OPENROUTER_API_KEY_TEST_1"),
             _chain_step("OPENROUTER_API_KEY_TEST_2")]

    with pytest.raises(OpenAIAPIStatusError):
        llm_client.generate_text(
            "system prompt", "user content", chain, agent_name="OR-6 Test Agent")

    assert client1.call_count == 1
    # The whole point of MALFORMED_REQUEST: never masked as "try the next
    # provider" -- that would just mean the next provider fails the same
    # way, or silently succeeds against a request we know is malformed.
    assert client2.call_count == 0


# ---------------------------------------------------------------------------
# 5. RATE_LIMIT_WINDOW, no headroom anywhere in the remaining chain --
#    ledger-derived cooldown, bounded sleep, retries THIS step in place.
# ---------------------------------------------------------------------------

def test_rate_limit_window_waits_and_retries_in_place(harness):
    rate_limit_exc = _FakeProviderError(
        "Rate limit exceeded", status_code=429,
        body_text="you have exceeded your requests per minute rate limit")
    client = harness["make_client"](
        "OPENROUTER_API_KEY_TEST_1",
        script=[rate_limit_exc, ("succeeded after waiting", "stop", 77)])
    # Single-step chain -- no other step to reroute to, so this MUST hit
    # the "wait and retry in place" arm, not "reroute".
    chain = [_chain_step("OPENROUTER_API_KEY_TEST_1")]

    result = llm_client.generate_text(
        "system prompt", "user content", chain, agent_name="OR-6 Test Agent")

    assert result == "succeeded after waiting"
    assert client.call_count == 2  # one failed attempt, one retry-in-place
    # Bounded: _LEDGER_WAIT_CAP_SECONDS caps the wait regardless of the
    # provider's own (here, absent) Retry-After signal.
    assert harness["sleep_calls"], "expected a bounded sleep before the in-place retry"
    assert all(0 < s <= llm_client._LEDGER_WAIT_CAP_SECONDS for s in harness["sleep_calls"])
    cooldown_key = "cooldown_until:openrouter:OPENROUTER_API_KEY_TEST_1"
    assert cooldown_key in harness["fake_bus"].store


def test_rate_limit_window_reroutes_when_a_later_step_has_headroom(harness):
    """Same bucket, different chain shape: a SECOND openrouter step
    exists and (per the mocked can_proceed()) has headroom, so this
    should reroute immediately rather than waiting -- confirming the
    reroute-vs-wait branch (_decide_ledger_action()) isn't accidentally
    always taking the wait path just because our fixture mocks
    can_proceed() to always say True."""
    rate_limit_exc = _FakeProviderError(
        "Rate limit exceeded", status_code=429, body_text="requests per minute")
    client1 = harness["make_client"]("OPENROUTER_API_KEY_TEST_1", script=[rate_limit_exc])
    harness["make_client"]("OPENROUTER_API_KEY_TEST_2",
                            script=[("rerouted", "stop", 30)])
    chain = [_chain_step("OPENROUTER_API_KEY_TEST_1"),
             _chain_step("OPENROUTER_API_KEY_TEST_2")]

    result = llm_client.generate_text(
        "system prompt", "user content", chain, agent_name="OR-6 Test Agent")

    assert result == "rerouted"
    assert client1.call_count == 1
    # Rerouted, not waited-and-retried -- no sleep, and (per
    # _handle_transient_error()'s RATE_LIMIT_WINDOW/reroute arm) no
    # cooldown write either, since the key itself was never confirmed
    # to be actually rate-limited long enough to matter here.
    assert not harness["sleep_calls"]
    assert "cooldown_until:openrouter:OPENROUTER_API_KEY_TEST_1" not in harness["fake_bus"].store


# ---------------------------------------------------------------------------
# 6. CONTEXT_LENGTH_EXCEEDED -- prompt shrunk, retries in place.
# ---------------------------------------------------------------------------

def test_context_length_exceeded_shrinks_and_retries_in_place(harness):
    context_exc = _FakeProviderError(
        "Bad request", status_code=400,
        body_text="this model's maximum context length is 4096 tokens")
    client = harness["make_client"](
        "OPENROUTER_API_KEY_TEST_1",
        script=[context_exc, ("succeeded after shrinking", "stop", 90)])
    chain = [_chain_step("OPENROUTER_API_KEY_TEST_1")]

    # A user_content with real length to shrink, so _shrink_prompt_for_retry()
    # has something to actually cut.
    long_content = "relevant context. " * 500

    result = llm_client.generate_text(
        "system prompt", long_content, chain, agent_name="OR-6 Test Agent")

    assert result == "succeeded after shrinking"
    assert client.call_count == 2  # one failed attempt, one shrunk retry-in-place
    # CONTEXT_LENGTH_EXCEEDED never cools the key down -- it isn't an
    # account/key problem, so nothing should be written here.
    assert "cooldown_until:openrouter:OPENROUTER_API_KEY_TEST_1" not in harness["fake_bus"].store
