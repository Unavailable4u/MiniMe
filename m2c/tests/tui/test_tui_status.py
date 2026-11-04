"""Patch C09a: the status line and the live mode cycle (m2c.tui.status)."""
from types import SimpleNamespace as NS

from m2c.tui.status import (DEFAULT_WINDOW_TOKENS, StatusState, context_percent, cycle_mode, format_credits,
                            format_status, observe, status_lines, window_from_env)


def ev(sub, **payload):
    return NS(type="run_event", subtype=sub, payload={"type": sub, **payload})


def test_status_line_text():
    s = StatusState(mode="accept-edits", input_tokens=64_000, window_tokens=128_000, run_credits=1.5)
    assert format_status(s) == "mode: accept-edits | ctx ~50% | credits: 1.5"


def test_tainted_flag_only_when_true():
    s = StatusState()
    assert "TAINTED" not in format_status(s)
    s.tainted = True
    assert "TAINTED" in format_status(s)


def test_no_usage_yet_shows_a_dash_and_no_dollars_anywhere():
    text = "\n".join(status_lines(StatusState(run_credits=3.14159)))
    assert "ctx -" in text and "$" not in text and "USD" not in text and "dollar" not in text.lower()


def test_context_percent_is_clamped_and_guarded():
    assert context_percent(None, 1000) is None and context_percent(5, 0) is None and context_percent(-1, 10) is None
    assert context_percent(10**9, 1000) == 100 and context_percent(250, 1000) == 25


def test_the_footnote_says_approximate():
    assert "approximate" in status_lines(StatusState())[-1]


def test_window_from_env():
    assert window_from_env({}) == DEFAULT_WINDOW_TOKENS
    assert window_from_env({"M2C_CONTEXT_WINDOW_TOKENS": "32000"}) == 32000
    for bad in ("abc", "-5", "5", "99999999999"):
        assert window_from_env({"M2C_CONTEXT_WINDOW_TOKENS": bad}) == DEFAULT_WINDOW_TOKENS


def test_mode_cycle_never_reaches_plan_and_never_leaves_it():
    assert cycle_mode("ask") == "accept-edits" and cycle_mode("accept-edits") == "ask"
    assert cycle_mode("plan") == "plan" and cycle_mode("whatever") == "whatever"
    mode = "ask"
    for _ in range(9):
        mode = cycle_mode(mode)
        assert mode != "plan"


def test_usage_events_feed_context_and_credits():
    s = StatusState()
    observe(s, ev("usage", model="m-1", input_tokens=12_800, cached_input_tokens=0, output_tokens=5, credits=0.4,
                  run_credits_total=0.4))
    observe(s, ev("usage", model="m-1", input_tokens=25_600, cached_input_tokens=0, output_tokens=5, credits=0.6,
                  run_credits_total=1.0))
    assert (s.input_tokens, s.run_credits, s.session_credits, s.model) == (25_600, 1.0, 1.0, "m-1")
    assert "ctx ~20%" in format_status(s) and "credits: 1" in format_status(s)


def test_a_new_run_resets_run_credits_but_the_session_total_keeps_counting():
    s = StatusState()
    observe(s, ev("usage", model="m", input_tokens=1, cached_input_tokens=0, output_tokens=1, credits=2.0,
                  run_credits_total=2.0))
    s.start_run()
    observe(s, ev("usage", model="m", input_tokens=1, cached_input_tokens=0, output_tokens=1, credits=1.0,
                  run_credits_total=1.0))
    assert s.run_credits == 1.0 and s.session_credits == 3.0
    assert "credits: 1 (session 3)" in format_status(s)


def test_credits_without_a_run_total_are_summed():
    s = StatusState()
    for c in (0.25, 0.25):
        observe(s, ev("usage", model="m", input_tokens=1, cached_input_tokens=0, output_tokens=1, credits=c))
    assert s.run_credits == 0.5


def test_quality_class_is_read_from_model_changed_only_with_a_strict_pattern():
    s = StatusState()
    observe(s, ev("notice", code="model_changed", severity="info",
                  message="The strong model was not available, so a standard model is answering instead."))
    assert s.quality_class == "standard" and "model: standard" in format_status(s)
    s2 = StatusState()
    observe(s2, ev("notice", code="model_changed", severity="info",
                   message="so a \x1b[31mred model is answering instead"))
    assert s2.quality_class is None
    observe(s2, ev("notice", code="other", severity="info", message="a fake model is answering instead"))
    assert s2.quality_class is None


def test_model_id_is_shown_when_the_class_is_unknown_and_is_cleaned():
    s = StatusState()
    observe(s, ev("usage", model="evil\x1b[31m-model", input_tokens=1, cached_input_tokens=0, output_tokens=1,
                  credits=0.0))
    assert "\x1b" not in format_status(s)


def test_paused_working_and_queued_are_shown():
    s = StatusState(working=True, queued=2)
    assert "working" in format_status(s) and "2 queued" in format_status(s)
    observe(s, ev("state_changed", state="paused_budget"))
    assert "paused budget" in format_status(s) and s.paused == "paused_budget"
    observe(s, ev("state_changed", state="running"))
    assert s.paused is None


def test_malformed_events_change_nothing():
    s = StatusState()
    observe(s, NS(type="run_event", subtype="usage", payload={"type": "usage", "input_tokens": "x",
                                                              "credits": float("nan"), "run_credits_total": True}))
    observe(s, NS(type="tool_proposed", subtype=None, payload={}))
    assert s.input_tokens is None and s.run_credits == 0.0


def test_notes_become_extra_lines():
    s = StatusState(notes=["project settings are ignored"])
    assert status_lines(s)[1] == "project settings are ignored"


def test_format_credits():
    assert format_credits(0) == "0" and format_credits(1.5) == "1.5" and format_credits(2.004) == "2"
    assert format_credits(float("inf")) == "?" and format_credits(True) == "?"
