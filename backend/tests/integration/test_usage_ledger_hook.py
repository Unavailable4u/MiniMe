"""S01 end to end through the real generate_text(): a mocked provider call
produces a ledger row for the right user and surface, thread-pool fan-out is
attributed, and a broken ledger never breaks the call.

Same mocking style as tests/integration/test_usage_logging.py (fake groq
client, fake bus/emitter); only the ledger's own commit() is intercepted.
"""
from concurrent.futures import ThreadPoolExecutor

from eo import usage_context, usage_ledger
from tests.integration.test_usage_logging import (
    _FakeChatResponse, _FakeRawResponse, _make_fake_client, _patch_bus_and_emitter)
from utils import llm_client

CHAIN = [{"provider": "groq", "model": "openai/gpt-oss-120b", "key_env": "GROQ_API_KEY"}]


class _RealisticUsage:
    """What a real OpenAI-compatible response carries: an input/output split
    and a cached-prompt count (the shared fake only has total_tokens)."""
    prompt_tokens, completion_tokens, total_tokens = 1000, 500, 1500
    prompt_tokens_details = type("D", (), {"cached_tokens": 800})


def _realistic_client(content):
    resp = _FakeChatResponse(content, 1500)
    resp.usage = _RealisticUsage()

    class _C:
        class chat:
            class completions:
                class with_raw_response:
                    @staticmethod
                    def create(**kwargs):
                        return _FakeRawResponse(resp)
    return _C()


def _setup(monkeypatch, rows):
    _patch_bus_and_emitter(monkeypatch)
    monkeypatch.setattr(llm_client, "_get_groq",
                        lambda key_env, timeout=None: _realistic_client("hello"))
    monkeypatch.setattr(llm_client, "_record_call_log", lambda **k: None)  # no DB here
    monkeypatch.setattr(usage_ledger, "commit", lambda **row: rows.append(row) or {"credits": 0})


def test_web_task_writes_one_row_for_its_owner_on_surface_web(monkeypatch):
    rows = []
    _setup(monkeypatch, rows)
    with usage_context.scope("user-1", "web", "sess-1"):
        out = llm_client.generate_text("sys", "usr", CHAIN, agent_name="T", session_id="sess-1")
    assert out == "hello"
    assert len(rows) == 1
    row = rows[0]
    assert (row["user_id"], row["surface"], row["session_id"]) == ("user-1", "web", "sess-1")
    assert (row["provider"], row["model"]) == ("groq", "openai/gpt-oss-120b")
    assert (row["input_tokens"], row["cached_input_tokens"], row["output_tokens"]) == (1000, 800, 500)
    assert row["cost_usd"] == llm_client._compute_cost_usd("groq", "openai/gpt-oss-120b", 1000, 500)
    assert row["cost_usd"] > 0                              # priced model


def test_call_outside_any_scope_is_recorded_as_system(monkeypatch):
    rows = []
    _setup(monkeypatch, rows)
    llm_client.generate_text("sys", "usr", CHAIN, agent_name="T")
    assert (rows[0]["user_id"], rows[0]["surface"]) == (None, "system")


def test_pool_worker_call_with_a_session_id_is_attributed_to_the_owner(monkeypatch):
    """A pool worker starts with an empty contextvars context; the call is
    matched to its owner by the session_id it carries (eo/usage_context.py).
    One call, not several: the rate-limit gate would ration concurrent calls
    on this single fake key, which is irrelevant to attribution."""
    rows = []
    _setup(monkeypatch, rows)
    with usage_context.scope("user-1", "web", "sess-1"):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(llm_client.generate_text, "sys", "usr", CHAIN,
                        agent_name="W", session_id="sess-1").result()
    assert [(r["user_id"], r["surface"]) for r in rows] == [("user-1", "web")]


def test_a_ledger_failure_does_not_fail_the_call(monkeypatch, capsys):
    _patch_bus_and_emitter(monkeypatch)
    monkeypatch.setattr(llm_client, "_get_groq",
                        lambda key_env, timeout=None: _make_fake_client("still works", 10))
    monkeypatch.setattr(llm_client, "_record_call_log", lambda **k: None)
    monkeypatch.setattr(usage_ledger, "commit",
                        lambda **k: (_ for _ in ()).throw(RuntimeError("ledger down")))
    with usage_context.scope("user-1", "web", "sess-1"):
        out = llm_client.generate_text("sys", "usr", CHAIN, agent_name="T", session_id="sess-1")
    assert out == "still works"
    assert "usage-ledger write failed (non-fatal)" in capsys.readouterr().out


def test_a_missing_database_does_not_fail_the_call(monkeypatch):
    """The un-mocked path: no DATABASE_URL, so db.cursor() itself raises."""
    _patch_bus_and_emitter(monkeypatch)
    monkeypatch.setattr(llm_client, "_get_groq",
                        lambda key_env, timeout=None: _make_fake_client("ok", 10))
    monkeypatch.setattr(llm_client, "_record_call_log", lambda **k: None)
    monkeypatch.setattr(usage_ledger.db, "DATABASE_URL", None, raising=False)
    monkeypatch.setattr(usage_ledger.db, "_pool", None, raising=False)
    with usage_context.scope("user-1", "web", "sess-1"):
        assert llm_client.generate_text("sys", "usr", CHAIN, session_id="sess-1") == "ok"
