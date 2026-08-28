"""
tests/unit/test_eo_tool_budget.py — Patch B6.

eo/tool_budget.py is a small, generic per-session counter on top of
memory.bus — it has no idea what "chat tab" means (see that module's own
docstring for why the chat-tab gating lives in the caller instead). This
file only pins the counter's own contract: increment() bumps and
returns the running total, over_threshold() reads without mutating, and
different session_ids never share a counter. memory.bus read/write need
no mocking (autouse fake_bus fixture, same established pattern every
other registry:-prefixed store's tests in this suite already uses).
"""
from eo import tool_budget


def test_increment_starts_at_one():
    assert tool_budget.increment("session-a") == 1


def test_increment_accumulates():
    tool_budget.increment("session-a")
    tool_budget.increment("session-a")
    assert tool_budget.increment("session-a") == 3


def test_increment_is_scoped_per_session():
    tool_budget.increment("session-a")
    tool_budget.increment("session-a")
    assert tool_budget.increment("session-b") == 1


def test_over_threshold_false_below_default_budget():
    for _ in range(tool_budget.DEFAULT_TOOL_CALL_BUDGET - 1):
        tool_budget.increment("session-a")
    assert tool_budget.over_threshold("session-a") is False


def test_over_threshold_true_at_default_budget():
    for _ in range(tool_budget.DEFAULT_TOOL_CALL_BUDGET):
        tool_budget.increment("session-a")
    assert tool_budget.over_threshold("session-a") is True


def test_over_threshold_respects_custom_threshold():
    tool_budget.increment("session-a")
    tool_budget.increment("session-a")
    assert tool_budget.over_threshold("session-a", threshold=2) is True
    assert tool_budget.over_threshold("session-a", threshold=3) is False


def test_over_threshold_does_not_mutate_the_counter():
    tool_budget.increment("session-a")
    tool_budget.over_threshold("session-a", threshold=1)
    tool_budget.over_threshold("session-a", threshold=1)
    # Two over_threshold() reads above must not have bumped the counter —
    # the next increment() should land on 2, not 4.
    assert tool_budget.increment("session-a") == 2


def test_over_threshold_false_for_untouched_session():
    assert tool_budget.over_threshold("never-incremented") is False


def test_reset_zeroes_the_counter():
    tool_budget.increment("session-a")
    tool_budget.increment("session-a")
    tool_budget.reset("session-a")
    assert tool_budget.increment("session-a") == 1
