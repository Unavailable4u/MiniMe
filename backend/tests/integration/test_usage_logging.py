"""
tests/integration/test_usage_logging.py — Part 11 of the v5 Master
Blueprint's testing plan: "confirm usage:* keys get written with correct
provider/key_id/date after a mocked LLM call."

Covers utils/llm_client.py's log_usage() (the public logger, Part 6.7) and
its internal adapter _log_usage(), which generate_text() calls after every
successful chat-completion step. No real Upstash, no real Pusher, no real
provider network calls -- everything below is mocked, same style as
tests/integration/test_eo_inspector.py and tests/integration/test_event_emission.py.

Note on key naming (see memory/bus.py's _namespaced()): usage:* keys are
deliberately NOT app_slug-namespaced -- quota is a property of your
accounts, not any one project -- so the keys asserted on below are exactly
what ends up in Redis, with no prefix to account for.

Moved from tests/test_usage_logging.py (B1 reorg) and updated against the
quota-reality fix (Patch 1 of the "Full Fix & Quota-Reality Guide") and
Migration Part 27 §1, both of which landed after this file was originally
written and changed three things the assertions below didn't yet account
for: (1) _log_usage() gained a `path` positional param between `tier` and
`agent_name`; (2) log_usage()'s stored record gains a "model" key
whenever generate_text() passes one through, so the two end-to-end
dict-equality assertions need it added; (3) tier/path now live INSIDE
emit_event's payload dict, not as top-level emit_event kwargs, and
daily_limit is resolved per-model (QUOTA_CONFIG[provider][model]) rather
than as a flat per-provider number, so it's None whenever no model was
passed.

Run standalone:
    python -m pytest tests/test_usage_logging.py -v
"""
import os
import sys
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils import llm_client

TODAY = date.today().isoformat()


class _FakeBus:
    """In-memory stand-in for memory.bus's read/write, scoped to one test
    via monkeypatch -- avoids needing real Upstash credentials or network
    access. Mirrors bus.read/write's own (key, default) / (key, value)
    signatures exactly, since log_usage() calls bus_read/bus_write with
    those signatures."""
    def __init__(self):
        self.store = {}

    def read(self, key, default=None):
        return self.store.get(key, default)

    def write(self, key, value):
        self.store[key] = value


class _FakeEmitter:
    """Records every emit_event() call instead of touching relay/emitter.py
    (already covered by test_event_emission.py) -- this file only needs
    to confirm log_usage() CALLS emit_event correctly, not that Pusher
    delivery itself works."""
    def __init__(self):
        self.calls = []

    def __call__(self, event_type, session_id=None, agent=None, tier=None, payload=None):
        self.calls.append({
            "event_type": event_type, "session_id": session_id,
            "agent": agent, "tier": tier, "payload": payload,
        })
        return True


def _patch_bus_and_emitter(monkeypatch):
    fake_bus = _FakeBus()
    fake_emitter = _FakeEmitter()
    monkeypatch.setattr(llm_client, "bus_read", fake_bus.read)
    monkeypatch.setattr(llm_client, "bus_write", fake_bus.write)
    monkeypatch.setattr(llm_client, "emit_event", fake_emitter)
    return fake_bus, fake_emitter


# ---------------------------------------------------------------------------
# 1. log_usage() directly -- the public logger any agent can call, incl.
#    ones with no chat-completion usage object at all (HF embeddings).
# ---------------------------------------------------------------------------

def test_usage_key_written_with_correct_provider_key_id_date(monkeypatch):
    fake_bus, _ = _patch_bus_and_emitter(monkeypatch)

    llm_client.log_usage("groq", "GROQ_API_KEY_6", 150, agent_name="Test Agent")

    key = f"usage:groq:GROQ_API_KEY_6:{TODAY}"
    assert key in fake_bus.store
    assert fake_bus.store[key] == {"requests": 1, "tokens": 150}


def test_usage_increments_on_repeated_calls(monkeypatch):
    fake_bus, _ = _patch_bus_and_emitter(monkeypatch)

    # OR-4 (reliability_overhaul_plan.md): was "cerebras"/"CEREBRAS_API_KEY_1" --
    # log_usage() is provider-agnostic, this is arbitrary test data, renamed to
    # match Cerebras' full retirement rather than testing a retired provider.
    llm_client.log_usage("openrouter", "OPENROUTER_API_KEY_1", 100, agent_name="Test Agent")
    llm_client.log_usage("openrouter", "OPENROUTER_API_KEY_1", 200, agent_name="Test Agent")

    key = f"usage:openrouter:OPENROUTER_API_KEY_1:{TODAY}"
    assert fake_bus.store[key] == {"requests": 2, "tokens": 300}


def test_tokens_none_only_increments_requests(monkeypatch):
    """The Cloudflare/HF caveat (module docstring): a request with no
    usage object still counts as a request, but must not corrupt the
    token total by adding None to it."""
    fake_bus, _ = _patch_bus_and_emitter(monkeypatch)

    llm_client.log_usage("huggingface", "HUGGINGFACE_API_KEY", None, agent_name="Duplication Checker")
    llm_client.log_usage("huggingface", "HUGGINGFACE_API_KEY", None, agent_name="Duplication Checker")

    key = f"usage:huggingface:HUGGINGFACE_API_KEY:{TODAY}"
    assert fake_bus.store[key] == {"requests": 2, "tokens": 0}


def test_usage_update_event_fires_with_correct_payload(monkeypatch):
    fake_bus, fake_emitter = _patch_bus_and_emitter(monkeypatch)

    llm_client.log_usage("groq", "GROQ_API_KEY_6", 150, session_id="sess_abc",
                          tier=3, agent_name="Reviewer")

    assert len(fake_emitter.calls) == 1
    call = fake_emitter.calls[0]
    assert call["event_type"] == "usage_update"
    assert call["session_id"] == "sess_abc"
    # quota-reality fix, §1: tier/path live INSIDE payload now, not as a
    # top-level emit_event kwarg -- emit_event itself is only ever called
    # with (event_type, session_id, agent, payload) from log_usage().
    assert call["payload"]["tier"] == 3
    assert call["payload"]["provider"] == "groq"
    assert call["payload"]["key_id"] == "GROQ_API_KEY_6"
    assert call["payload"]["tokens_used_today"] == 150
    # No `model` was passed to this log_usage() call, so daily_limit can't
    # be resolved (QUOTA_CONFIG is keyed by [provider][model], not a flat
    # per-provider number, since the quota-reality fix) -- None is correct
    # here, not a KeyError or a guess.
    assert call["payload"]["daily_limit"] is None


def test_no_session_id_still_writes_usage(monkeypatch):
    """log_usage's own bus write must not depend on session_id being set
    -- only the relay event (handled by emit_event's own no-op path,
    already covered in test_event_emission.py) is session-gated."""
    fake_bus, fake_emitter = _patch_bus_and_emitter(monkeypatch)

    llm_client.log_usage("github", "GITHUB_MODELS_PAT", 42, agent_name="Test Writer")

    key = f"usage:github:GITHUB_MODELS_PAT:{TODAY}"
    assert fake_bus.store[key]["tokens"] == 42
    # emit_event is still CALLED (log_usage doesn't branch on session_id
    # itself), just with session_id=None -- confirm that, rather than
    # asserting it was never called at all.
    assert len(fake_emitter.calls) == 1
    assert fake_emitter.calls[0]["session_id"] is None


def test_log_usage_never_raises_on_bus_failure(monkeypatch):
    def _broken_write(key, value):
        raise RuntimeError("Upstash is down")

    monkeypatch.setattr(llm_client, "bus_read", lambda key, default=None: default)
    monkeypatch.setattr(llm_client, "bus_write", _broken_write)
    monkeypatch.setattr(llm_client, "emit_event", lambda *a, **kw: True)

    # Must not raise -- log_usage wraps its body in try/except per its
    # own docstring ("Never raises").
    llm_client.log_usage("groq", "GROQ_API_KEY_6", 100, agent_name="Test Agent")


def test_log_usage_adapter_extracts_from_dict_shaped_usage(monkeypatch):
    """Cloudflare's response returns usage as a plain dict, not an SDK
    object with attributes -- _log_usage() must handle both shapes."""
    fake_bus, _ = _patch_bus_and_emitter(monkeypatch)

    # Migration Part 27 §1 inserted `path` as its own positional param,
    # between `tier` and `agent_name` -- session_id, tier, path, agent_name.
    llm_client._log_usage("cloudflare", "CLOUDFLARE_ACCOUNT_ID_2",
                           {"total_tokens": 88}, None, None, None, "Reviewer")

    key = f"usage:cloudflare:CLOUDFLARE_ACCOUNT_ID_2:{TODAY}"
    assert fake_bus.store[key] == {"requests": 1, "tokens": 88}


def test_log_usage_adapter_handles_missing_usage_entirely(monkeypatch):
    fake_bus, _ = _patch_bus_and_emitter(monkeypatch)

    llm_client._log_usage("cloudflare", "CLOUDFLARE_ACCOUNT_ID_2", None, None, None, None, "Reviewer")

    key = f"usage:cloudflare:CLOUDFLARE_ACCOUNT_ID_2:{TODAY}"
    assert fake_bus.store[key] == {"requests": 1, "tokens": 0}


# ---------------------------------------------------------------------------
# 2. End-to-end through generate_text() -- "after a mocked LLM call"
#    (Part 11's exact phrasing), confirming the full chat-completion ->
#    usage-extraction -> log_usage() pipeline, not just log_usage() in
#    isolation.
# ---------------------------------------------------------------------------

class _FakeUsage:
    """Mimics an OpenAI-SDK-shaped usage object (groq/openrouter/mistral/
    gemini/huggingface all return this shape) -- .total_tokens as an
    attribute, not a dict key."""
    def __init__(self, total_tokens):
        self.total_tokens = total_tokens


class _FakeChoice:
    def __init__(self, content, finish_reason="stop"):
        self.message = type("M", (), {"content": content})
        self.finish_reason = finish_reason


class _FakeChatResponse:
    def __init__(self, content, total_tokens, finish_reason="stop"):
        self.choices = [_FakeChoice(content, finish_reason)]
        self.usage = _FakeUsage(total_tokens)


class _FakeRawResponse:
    """Bug fix: Phase 3c (reliability_overhaul_plan.md) changed
    _call_step() to call client.chat.completions.WITH_RAW_RESPONSE.create(
    ...) instead of .create(...) directly, so it can read response
    headers for the rate ledger (see llm_client.py's own docstring on
    that change). The two fakes below used to only define a plain
    .create() staticmethod, which .with_raw_response.create(...) doesn't
    have -- these two end-to-end tests were raising AttributeError under
    current code (found while doing the OR-4 provider-swap pass; the
    fake's shape had simply gone stale against Phase 3c, unrelated to
    any provider migration). Matches what real .with_raw_response.create()
    actually returns: a .headers mapping and a .parse() method."""
    def __init__(self, parsed, headers=None):
        self._parsed = parsed
        self.headers = headers or {}

    def parse(self):
        return self._parsed


def _make_fake_client(content, total_tokens):
    """Factory (not a shared class) so each test's content/token values
    are captured cleanly per-call, no shared mutable state between tests."""
    class _FakeClient:
        class chat:
            class completions:
                class with_raw_response:
                    @staticmethod
                    def create(**kwargs):
                        return _FakeRawResponse(_FakeChatResponse(content, total_tokens))
    return _FakeClient()


def test_generate_text_logs_usage_after_mocked_chat_completion(monkeypatch):
    fake_bus, fake_emitter = _patch_bus_and_emitter(monkeypatch)
    monkeypatch.setattr(
        llm_client, "_get_groq",
        lambda key_env, timeout=None: _make_fake_client("hello world", 321),
    )

    # llama-3.3-70b-versatile decommissioned by Groq; migrated to
    # openai/gpt-oss-120b (this test only asserts the chain step's model
    # string passes through unchanged, so any live Groq model works here).
    chain = [{"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"}]
    result = llm_client.generate_text(
        "system prompt", "user content", chain,
        agent_name="Test Agent", session_id="sess_xyz", tier=1,
    )

    assert result == "hello world"
    key = f"usage:groq:GROQ_API_KEY:{TODAY}"
    # quota-reality fix, §1: generate_text() now forwards the chain step's
    # `model` through to log_usage(), which stores it on the daily record
    # (record["model"]) so get_quota_snapshot() can resolve per-model
    # limits -- the record has one more key than it used to.
    assert fake_bus.store[key] == {
        "requests": 1, "tokens": 321, "model": "openai/gpt-oss-120b",
    }
    assert fake_emitter.calls[0]["payload"]["tokens_used_today"] == 321


def test_generate_text_logs_zero_tokens_when_usage_object_missing(monkeypatch):
    """Rare for groq/openrouter in practice, but generate_text()'s
    own getattr(response, "usage", None) fallback must hold either way --
    same "request-only logging" contract as the Cloudflare-missing-usage
    case in the module docstring."""
    fake_bus, fake_emitter = _patch_bus_and_emitter(monkeypatch)

    class _NoUsageResponse:
        def __init__(self, content):
            self.choices = [_FakeChoice(content)]
            # deliberately no .usage attribute at all

    class _NoUsageClient:
        class chat:
            class completions:
                class with_raw_response:
                    @staticmethod
                    def create(**kwargs):
                        return _FakeRawResponse(_NoUsageResponse("hi"))

    monkeypatch.setattr(llm_client, "_get_groq", lambda key_env, timeout=None: _NoUsageClient())

    # llama-3.3-70b-versatile decommissioned by Groq; migrated to
    # openai/gpt-oss-120b (same passthrough-only reasoning as the test
    # above).
    chain = [{"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"}]
    llm_client.generate_text("sys", "user", chain, agent_name="Test Agent")

    key = f"usage:groq:GROQ_API_KEY:{TODAY}"
    assert fake_bus.store[key] == {
        "requests": 1, "tokens": 0, "model": "openai/gpt-oss-120b",
    }


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    failures = 0
    for t in tests:
        try:
            import inspect
            if "monkeypatch" in inspect.signature(t).parameters:
                print(f"  SKIP  {t.__name__} (needs pytest's monkeypatch -- run via pytest)")
                continue
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:
            failures += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    sys.exit(1 if failures else 0)