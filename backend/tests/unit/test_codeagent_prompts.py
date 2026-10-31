"""Patch S07 -- unit tests for codeagent/prompts.py (the system prompt and the default context assembler)."""
import asyncio
import re

import pytest

from codeagent import prompts
from codeagent.model_adapter import Message, ToolSpec
from codeagent.prompts import (AssembledContext, DefaultContextAssembler, PLAN_MODE_ADDITION, SYSTEM_PROMPT,
                               call_assembler, format_tool_content, items_to_messages, system_prompt)

SPEC = ToolSpec("read_file", "Read a file.", {"type": "object", "properties": {}})


# ---- the prompt --------------------------------------------------------------------------------

def test_the_system_prompt_is_short():
    assert len(SYSTEM_PROMPT) <= prompts.MAX_SYSTEM_PROMPT_CHARS
    assert len(system_prompt("plan")) <= prompts.MAX_SYSTEM_PROMPT_CHARS + 400


def test_the_system_prompt_names_no_environment_variable_or_internal_path():
    for text in (system_prompt("ask"), system_prompt("plan")):
        assert not re.search(r"\b[A-Z][A-Z0-9]*_[A-Z0-9_]+\b", text), "an ALL_CAPS_NAME looks like an environment variable"
        for needle in ("/home", "/mnt", "/etc", "backend", "codeagent", "protocol", ".env", "environ", "redis",
                       "postgres", "api key", "api_key", "token budget", "run_guard", "S0", "S1"):
            assert needle.lower() not in text.lower(), needle
        assert not re.search(r"(?:^|\s)(?:~|\.{1,2})?/[A-Za-z0-9_.-]+/", text), "a filesystem path"


def test_the_prompt_says_what_the_agent_needs_to_know():
    text = SYSTEM_PROMPT.lower()
    assert "tools" in text
    assert "denied" in text and "not retried" in text and "ask them first" in text
    assert "data, not instructions" in text


def test_plan_mode_adds_one_paragraph_and_the_other_modes_do_not():
    assert system_prompt("ask") == SYSTEM_PROMPT == system_prompt("accept-edits")
    assert system_prompt("plan") == SYSTEM_PROMPT + "\n\n" + PLAN_MODE_ADDITION
    assert "plan_exit" in PLAN_MODE_ADDITION and "read-only" in PLAN_MODE_ADDITION


# ---- the default assembler ---------------------------------------------------------------------

STATE = {"mode": "ask", "instructions": "use tabs", "rules": [{"name": "py", "text": "type hints"}, {"name": "e", "text": "  "}],
         "memory_index": "likes pytest"}
ITEMS = [{"role": "user", "text": "fix it"},
         {"role": "assistant", "text": "looking", "tool_calls": [{"call_id": "c1", "tool": "read_file", "input": {"path": "a"}}]},
         {"role": "tool", "call_id": "c1", "ok": True, "output": "contents"},
         {"role": "assistant", "text": "done"}]


def test_the_default_assembler_orders_system_then_conversation_and_passes_the_tools_through():
    ctx = DefaultContextAssembler().assemble(STATE, ITEMS, [SPEC], "ask")
    assert isinstance(ctx, AssembledContext) and ctx.tools == (SPEC,)
    system = ctx.messages[0]
    assert system.role == "system"
    assert system.content.index(SYSTEM_PROMPT) < system.content.index("use tabs") < system.content.index("type hints") \
        < system.content.index("likes pytest")
    assert [m.role for m in ctx.messages] == ["system", "user", "assistant", "tool", "assistant"]
    assert ctx.messages[2].tool_calls[0].id == "c1" and ctx.messages[2].tool_calls[0].arguments == {"path": "a"}
    assert ctx.messages[3].tool_call_id == "c1"


def test_blank_rules_and_empty_context_are_left_out():
    ctx = DefaultContextAssembler().assemble({"instructions": "", "rules": [{"name": "e", "text": " "}]}, [], [], "plan")
    assert ctx.messages[0].content == system_prompt("plan")


def test_tool_content_marks_errors_and_truncation():
    assert format_tool_content({"ok": True, "output": "x"}) == "x"
    assert format_tool_content({"ok": False, "output": "boom"}) == "[tool error] boom"
    assert format_tool_content({"ok": True, "output": "x", "truncated": True}).endswith("truncated]")
    assert format_tool_content({"ok": True, "output": "x\n[output truncated by the server: 5 bytes omitted]",
                                "truncated": True}).count("truncated") == 1


def test_items_that_are_not_in_the_protocols_shape_are_skipped_not_guessed_at():
    items = [{"role": "user"}, {"role": "assistant"}, {"role": "tool"}, {"role": "system", "text": "x"},
             {"role": "assistant", "tool_calls": [{"call_id": "c", "tool": "grep", "input": "not a dict"}]}]
    out = items_to_messages(items)
    assert [m.role for m in out] == ["assistant"] and out[0].tool_calls[0].arguments == {}


def test_a_hook_may_be_sync_or_async_and_must_return_an_assembled_context():
    class Async:
        async def assemble(self, state, items, tools, mode):
            return AssembledContext(messages=(Message.user("hi"),), max_tokens=7)

    class Bad:
        def assemble(self, state, items, tools, mode):
            return "nope"

    got = asyncio.run(call_assembler(Async(), {}, [], [], "ask"))
    assert got.max_tokens == 7 and got.messages[0].content == "hi"
    with pytest.raises(TypeError):
        asyncio.run(call_assembler(Bad(), {}, [], [], "ask"))


def test_the_module_has_no_logger_so_content_cannot_be_logged_from_it():
    text = open(prompts.__file__, encoding="utf-8").read()
    assert "import logging" not in text and "getLogger" not in text and "logger." not in text
