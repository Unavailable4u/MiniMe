"""Patch I01 -- unit tests for codeagent/hello.py (the walking skeleton's turn runner)."""
import asyncio
import logging
from pathlib import Path

import pytest

from codeagent import hello, run_handlers
from codeagent.model_adapter import Message
from codeagent.run_handlers import RunHandlers

SERVER = Path(__file__).resolve().parents[2] / "api" / "server.py"


# ---- configuration --------------------------------------------------------------------------

def test_no_provider_key_gives_an_empty_chain():
    assert hello.build_chain({}) == []


def test_the_first_provider_with_a_key_wins_and_only_the_name_is_recorded():
    fake_key = "k" * 12
    env = {"MISTRAL_API_KEY": fake_key, "OPENROUTER_API_KEY": fake_key}
    (step,) = hello.build_chain(env)
    assert step == {"provider": "mistral", "model": "mistral-medium-latest", "key_env": "MISTRAL_API_KEY"}
    assert fake_key not in repr(step)                  # the chain names the variable, never its value
    both = dict(env, GROQ_API_KEY_3=fake_key)
    assert hello.build_chain(both)[0]["provider"] == "groq"
    assert hello.build_chain(both)[0]["key_env"] == "GROQ_API_KEY_3"


def test_the_model_can_be_named_by_environment():
    env = {"GROQ_API_KEY": "k" * 12, "M2C_HELLO_MODEL": "some/model-1"}
    assert hello.build_chain(env)[0]["model"] == "some/model-1"


# ---- message mapping ----------------------------------------------------------------------

def test_the_conversation_becomes_system_user_assistant_messages():
    items = [{"role": "user", "text": "a"}, {"role": "assistant", "text": "b"}, {"role": "user", "text": "c"}]
    msgs = hello._to_messages(items)
    assert [m.role for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[0] == Message.system(hello.SYSTEM_PROMPT)


def test_items_that_are_not_text_are_skipped_not_guessed_at():
    items = [{"role": "user", "text": "a"}, {"role": "assistant", "text": ""}, {"role": "tool", "text": "x"},
             {"role": "assistant"}]
    assert [m.role for m in hello._to_messages(items)] == ["system", "user"]


# ---- installation -----------------------------------------------------------------------------

@pytest.fixture
def handlers(monkeypatch):
    from codeagent import emitter as emitter_module
    em = emitter_module.RunEmitter()
    monkeypatch.setattr(emitter_module, "_EMITTER", em)
    h = RunHandlers(emitter=em)
    monkeypatch.setattr(run_handlers, "_HANDLERS", h)
    return h


def test_install_wires_the_runner_and_three_hooks_and_is_safe_twice(handlers):
    hello.install()
    hello.install()
    assert handlers._turn_runner is hello.run_turn
    for name, fn in (("_on_cancel", hello._on_cancel), ("_on_resume", hello._on_resume),
                     ("_on_run_disconnect", hello._on_run_disconnect)):
        hooks = [h for h in getattr(handlers, name) if h is fn]
        assert len(hooks) == 1, f"{name} registered {len(hooks)} times"


def test_install_keeps_the_openai_sdk_from_logging_prompts():
    sdk = logging.getLogger("openai")
    before = sdk.level
    try:
        sdk.setLevel(logging.DEBUG)
        hello.install()
        assert sdk.getEffectiveLevel() >= logging.INFO
        sdk.setLevel(logging.ERROR)
        hello.install()
        assert sdk.level == logging.ERROR              # never lowered
    finally:
        sdk.setLevel(before)


def test_server_installs_hello_right_after_the_run_handlers():
    text = SERVER.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    i = next(n for n, ln in enumerate(lines) if ln.startswith("codeagent_run_handlers.install()"))
    assert lines[i + 1].startswith("codeagent_hello.install()")
    assert "from codeagent import hello as codeagent_hello" in text


def test_the_lock_registry_does_not_keep_locks_alive():
    import gc

    async def use():
        async with hello._turn_lock("run-x"):
            assert "run-x" in hello._LOCKS

    asyncio.run(use())
    gc.collect()
    assert "run-x" not in hello._LOCKS
