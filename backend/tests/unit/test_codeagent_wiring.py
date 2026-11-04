"""Patch S15 -- unit tests for codeagent/wiring.py (the switch from hello.py's turn runner to the agent loop).

What is pinned here is the JOINING: which parts the process loop is made of, what install() registers on S14's run
handlers (and what it must not leave there), the kill switch, the capability rule and the configuration report.
tests/unit/test_codeagent_wiring_flow.py runs whole turns through the real router, handlers, broker and loop.

  * build_loop: the S07 loop over the real parts (the broker, the S14 emitter, S08's assembler, S10's metering) ... test_build_*
  * install: runner, hooks, idempotent, hello's hooks gone ................................................... test_install_*
  * the kill switch M2C_LOOP=hello ........................................................................... test_kill_switch_*
  * conn_offers_tools: "tools" in the connection's capabilities, nothing else, fail closed ................... test_conn_offers_tools_*
  * configuration_problems: names only, never a value ....................................................... test_configuration_*
"""
import asyncio
import logging
import types

import pytest

from codeagent import broker as broker_module
from codeagent import context, emitter as emitter_module, hello, loop as loop_module, metering, run_handlers, wiring
from codeagent.emitter import RunEmitter
from codeagent.loop import AgentLoop
from codeagent.run_handlers import RunHandlers

pytest.importorskip("jsonschema", reason="the loop validates tool arguments with jsonschema")

SECRET_LEN = 40


def fake_secret(char="s"):
    return char * SECRET_LEN                                        # built at run time: nothing here looks like a key


@pytest.fixture
def handlers(monkeypatch):
    em = RunEmitter()
    monkeypatch.setattr(emitter_module, "_EMITTER", em)
    h = RunHandlers(emitter=em)
    monkeypatch.setattr(run_handlers, "_HANDLERS", h)
    monkeypatch.setattr(loop_module, "_LOOP", None)
    monkeypatch.setattr(wiring, "_BUILT", None)
    monkeypatch.delenv("M2C_LOOP", raising=False)
    for name in ("GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return h


def count(hooks, fn):
    return len([h for h in hooks if h is fn])


# ---- build_loop --------------------------------------------------------------------------------

def test_build_makes_an_agent_loop_over_the_real_parts(handlers, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", fake_secret("k"))
    monkeypatch.setenv("M2C_CONTEXT_WINDOW_TOKENS", "64000")
    built = wiring.build_loop()
    assert isinstance(built, AgentLoop)
    assert [(c.provider, c.key_env) for c in built.chain] == [("groq", "GROQ_API_KEY")]      # default_chain()
    assert built._broker is broker_module.get_broker()                                       # the real broker
    assert isinstance(built._assembler, context.BudgetedContextAssembler)                    # S08, not the default
    assert built._assembler.window_tokens == 64000                                           # and it reads the env
    assert built._offer_tools is wiring.conn_offers_tools                                    # the capability rule
    assert built._metering_factory is not None
    run = types.SimpleNamespace(id="run-wire-0001", user_id="user-1", device_id="device-1", state="running")
    assert isinstance(built._metering_factory(run), metering.RunMetering)                    # S10's, not the no-op


def test_build_without_a_provider_key_has_an_empty_chain_not_a_default_key(handlers):
    built = wiring.build_loop()
    assert built.chain == ()                                         # every turn will say provider_unavailable


def test_build_gives_the_emitter_functions_that_look_the_emitter_up_late(handlers, monkeypatch):
    built = wiring.build_loop()
    calls = []

    class Spy:
        async def emit(self, run_id, t, payload):
            calls.append(("emit", run_id, t))

        async def emit_final(self, run_id, to_state, why=None):
            calls.append(("final", run_id, to_state, why))

        def unbind(self, run_id):
            calls.append(("unbind", run_id))

    monkeypatch.setattr(emitter_module, "_EMITTER", Spy())            # swapped AFTER the loop was built
    asyncio.run(built._emit("run-a", "run_event", {}))
    asyncio.run(built._emit_final("run-a", "cancelled", "x"))
    built._unbind("run-a")
    assert calls == [("emit", "run-a", "run_event"), ("final", "run-a", "cancelled", "x"), ("unbind", "run-a")]


def test_build_passes_overrides_through_and_does_not_touch_the_process_state(handlers):
    marker = object()
    built = wiring.build_loop(environ={}, chain=[], assembler=marker, config=loop_module.LoopConfig(step_limit=3))
    assert built._assembler is marker and built.config.step_limit == 3
    assert loop_module._LOOP is None and wiring._BUILT is None and handlers.get_turn_runner() is None


# ---- install -----------------------------------------------------------------------------------

def test_install_registers_the_loops_runner_and_hooks_and_builds_the_loop_once(handlers):
    active = wiring.install()
    assert isinstance(active, AgentLoop) and loop_module.get_loop() is active
    assert handlers.get_turn_runner() is loop_module.run_turn
    for hooks, name in ((handlers.on_resume_hooks(), "on_resume"), (handlers.on_cancel_hooks(), "on_cancel"),
                        (handlers.on_run_disconnect_hooks(), "on_run_disconnect")):
        assert count(hooks, active.hooks[name]) == 1


def test_install_is_idempotent_one_loop_one_copy_of_every_hook(handlers):
    first = wiring.install()
    second = wiring.install()
    assert first is second and loop_module.get_loop() is first
    assert handlers.get_turn_runner() is loop_module.run_turn
    assert count(handlers.on_resume_hooks(), first.hooks["on_resume"]) == 1
    assert count(handlers.on_resume_hooks(), wiring._announce_restart) == 1
    assert count(handlers.on_cancel_hooks(), first.hooks["on_cancel"]) == 1
    assert count(handlers.on_run_disconnect_hooks(), first.hooks["on_run_disconnect"]) == 1


def test_install_puts_the_restart_notice_hook_before_the_loops_resume_hook(handlers):
    active = wiring.install()
    order = list(handlers.on_resume_hooks())
    assert order.index(wiring._announce_restart) < order.index(active.hooks["on_resume"])


def test_install_leaves_none_of_hellos_hooks_active(handlers):
    wiring.install()
    for hooks, fn in ((handlers.on_cancel_hooks(), hello._on_cancel), (handlers.on_resume_hooks(), hello._on_resume),
                      (handlers.on_run_disconnect_hooks(), hello._on_run_disconnect)):
        assert count(hooks, fn) == 0
    assert handlers.get_turn_runner() is not hello.run_turn


def test_install_removes_hellos_hooks_and_runner_if_something_installed_them_first(handlers):
    hello.install()
    assert handlers.get_turn_runner() is hello.run_turn and count(handlers.on_resume_hooks(), hello._on_resume) == 1
    active = wiring.install()
    assert handlers.get_turn_runner() is loop_module.run_turn            # replaced, not refused
    for hooks, fn in ((handlers.on_cancel_hooks(), hello._on_cancel), (handlers.on_resume_hooks(), hello._on_resume),
                      (handlers.on_run_disconnect_hooks(), hello._on_run_disconnect)):
        assert count(hooks, fn) == 0
    assert count(handlers.on_resume_hooks(), active.hooks["on_resume"]) == 1


def test_install_takes_an_explicit_loop_and_makes_it_the_process_loop(handlers):
    mine = wiring.build_loop(environ={}, chain=[])
    assert wiring.install(loop=mine) is mine
    assert loop_module.get_loop() is mine and handlers.get_turn_runner() is loop_module.run_turn
    assert count(handlers.on_resume_hooks(), mine.hooks["on_resume"]) == 1


def test_install_never_raises_for_a_missing_setting_it_reports_names(handlers, monkeypatch, caplog):
    monkeypatch.delenv("RUN_BUFFER_SECRET", raising=False)
    monkeypatch.delenv("DEVICE_JWT_SECRET", raising=False)
    with caplog.at_level(logging.WARNING, logger="codeagent.wiring"):
        wiring.install()                                              # the web app must still boot
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "RUN_BUFFER_SECRET" in log and "DEVICE_JWT_SECRET" in log


# ---- the kill switch ---------------------------------------------------------------------------

@pytest.mark.parametrize("value, mode", [(None, "loop"), ("", "loop"), ("loop", "loop"), ("LOOP", "loop"),
                                          (" hello ", "hello"), ("hello", "hello"), ("HELLO", "hello"),
                                          ("helo", "loop"), ("0", "loop")])
def test_the_mode_comes_from_m2c_loop_and_defaults_to_the_loop(value, mode):
    env = {} if value is None else {"M2C_LOOP": value}
    assert wiring.selected_mode(env) == mode


def test_kill_switch_hello_installs_hellos_runner_and_hooks_and_not_the_loops(handlers, monkeypatch):
    monkeypatch.setenv("M2C_LOOP", "hello")
    assert wiring.install() is None                                   # no loop was built
    assert handlers.get_turn_runner() is hello.run_turn
    for hooks, fn in ((handlers.on_cancel_hooks(), hello._on_cancel), (handlers.on_resume_hooks(), hello._on_resume),
                      (handlers.on_run_disconnect_hooks(), hello._on_run_disconnect)):
        assert count(hooks, fn) == 1
    assert wiring._announce_restart not in handlers.on_resume_hooks()
    assert wiring._BUILT is None


def test_kill_switch_is_safe_twice_and_a_flip_back_leaves_only_one_side(handlers, monkeypatch):
    monkeypatch.setenv("M2C_LOOP", "hello")
    wiring.install()
    wiring.install()
    assert count(handlers.on_resume_hooks(), hello._on_resume) == 1
    monkeypatch.delenv("M2C_LOOP")
    active = wiring.install()                                         # a hot flip in one process (tests, reloads)
    assert handlers.get_turn_runner() is loop_module.run_turn and count(handlers.on_resume_hooks(), hello._on_resume) == 0
    monkeypatch.setenv("M2C_LOOP", "hello")
    wiring.install()
    assert handlers.get_turn_runner() is hello.run_turn
    for name, hooks in (("on_resume", handlers.on_resume_hooks()), ("on_cancel", handlers.on_cancel_hooks()),
                        ("on_run_disconnect", handlers.on_run_disconnect_hooks())):
        assert count(hooks, active.hooks[name]) == 0                  # the loop's hooks are gone again
    assert wiring._announce_restart not in handlers.on_resume_hooks()


def test_an_unknown_value_uses_the_loop_and_says_so_without_echoing_it(handlers, monkeypatch, caplog):
    monkeypatch.setenv("M2C_LOOP", "CANARY-not-a-mode")
    with caplog.at_level(logging.WARNING, logger="codeagent.wiring"):
        active = wiring.install()
    assert isinstance(active, AgentLoop) and handlers.get_turn_runner() is loop_module.run_turn
    log = "\n".join(r.getMessage() for r in caplog.records)
    assert "M2C_LOOP" in log and "CANARY" not in log


# ---- conn_offers_tools -------------------------------------------------------------------------

@pytest.mark.parametrize("capabilities, expected", [
    (("tools",), True), (("sandbox", "tools"), True), (["tools"], True), (("acp", "sandbox"), False), ((), False),
    (None, False), ("tools", False), (b"tools", False), (5, False), (("Tools",), False), (("tools-extra",), False)])
def test_conn_offers_tools_only_for_the_exact_capability(capabilities, expected):
    assert wiring.conn_offers_tools(types.SimpleNamespace(capabilities=capabilities)) is expected


def test_conn_offers_tools_is_false_for_a_connection_that_has_no_capabilities_at_all():
    assert wiring.conn_offers_tools(object()) is False
    assert wiring.conn_offers_tools(None) is False


# ---- configuration_problems --------------------------------------------------------------------

def test_configuration_reports_every_missing_setting_by_name(monkeypatch):
    for name in ("RUN_BUFFER_SECRET", "DEVICE_JWT_SECRET", "GROQ_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    names = [name for name, _ in wiring.configuration_problems()]
    assert names == ["RUN_BUFFER_SECRET", "DEVICE_JWT_SECRET", "GROQ_API_KEY / MISTRAL_API_KEY / OPENROUTER_API_KEY"]


def test_configuration_is_quiet_when_everything_is_set(monkeypatch):
    monkeypatch.setenv("RUN_BUFFER_SECRET", fake_secret("b"))
    monkeypatch.setenv("DEVICE_JWT_SECRET", fake_secret("d"))
    monkeypatch.setenv("GROQ_API_KEY_2", fake_secret("k"))            # a numbered key counts, like hello's chain
    assert wiring.configuration_problems() == []


def test_configuration_names_a_weak_secret_without_ever_showing_it(monkeypatch):
    weak = "CANARY-weak"
    monkeypatch.setenv("RUN_BUFFER_SECRET", weak)
    monkeypatch.setenv("DEVICE_JWT_SECRET", weak)
    monkeypatch.setenv("GROQ_API_KEY", fake_secret("k"))
    problems = wiring.configuration_problems()
    assert [name for name, _ in problems] == ["RUN_BUFFER_SECRET", "DEVICE_JWT_SECRET"]
    assert weak not in repr(problems) and "CANARY" not in repr(problems)


def test_configuration_refuses_a_device_secret_equal_to_the_supabase_one(monkeypatch):
    same = fake_secret("z")
    monkeypatch.setenv("RUN_BUFFER_SECRET", fake_secret("b"))
    monkeypatch.setenv("DEVICE_JWT_SECRET", same)
    monkeypatch.setenv("SUPABASE_JWT_SECRET", same)
    monkeypatch.setenv("GROQ_API_KEY", fake_secret("k"))
    problems = wiring.configuration_problems()
    assert [name for name, _ in problems] == ["DEVICE_JWT_SECRET"] and same not in repr(problems)
