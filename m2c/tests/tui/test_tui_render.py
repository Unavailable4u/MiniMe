"""Patch C09a: server events become display blocks (m2c.tui.render)."""
from types import SimpleNamespace as NS

from m2c.tui.diff import Existing
from m2c.tui.render import STYLES, Renderer, error_blocks, summarize_call

CID = "call_1"


def run_event(sub, **payload):
    return NS(type="run_event", subtype=sub, payload={"type": sub, **payload})


def proposed(tool, args, call_id=CID, risk="R1"):
    return NS(type="tool_proposed", subtype=None, payload={"call_id": call_id, "tool": tool, "input": args, "risk": risk})


def text_of(blocks):
    return "\n".join(b.text for b in blocks)


def test_text_delta_is_returned_as_it_arrives_and_text_done_closes_the_line():
    r = Renderer()
    out = r.event(run_event("text_delta", text="Hel")) + r.event(run_event("text_delta", text="lo"))
    assert [b.kind for b in out] == ["text", "text"] and r.reply_text == "Hello"
    done = r.event(run_event("text_done", stop_reason="end_turn"))
    assert [(b.kind, b.text) for b in done] == [("line", "")] and r.reply_text == ""


def test_text_done_after_a_full_line_adds_nothing():
    r = Renderer()
    r.event(run_event("text_delta", text="done\n"))
    assert r.event(run_event("text_done")) == []


def test_reply_restarted_discards_the_partial_text_and_shows_one_short_line():
    r = Renderer()
    r.event(run_event("text_delta", text="half a sent"))
    out = r.event(run_event("notice", code="reply_restarted", severity="info", message="x"))
    assert r.reply_text == ""
    lines = [b for b in out if b.text]
    assert len(lines) == 1 and "written again" in lines[0].text and lines[0].style == "dim"
    r.event(run_event("text_delta", text="final"))
    assert r.reply_text == "final"


def test_other_notices_keep_severity_and_are_cleaned():
    r = Renderer()
    out = r.event(run_event("notice", code="x", severity="warning", message="careful \x1b[31mred"))
    assert out[-1].style == "warning" and out[-1].text.startswith("[warning] careful") and "\x1b" not in out[-1].text
    assert r.event(run_event("notice", code="x", severity="info", message="fyi"))[-1].style == "info"


def test_tool_card_for_a_read_and_a_finished_line():
    r = Renderer()
    card = r.event(proposed("read_file", {"path": "src/a.py"}))
    assert card[-1].text == "● read_file  src/a.py   [R1]" and card[-1].style == "tool"
    fin = r.event(run_event("tool_finished", call_id=CID, tool="read_file", status="ok", duration_ms=12))
    assert fin[-1].text == "  ✓ read_file ok (12 ms)" and fin[-1].style == "ok"


def test_every_finished_status_has_a_line():
    r = Renderer()
    for status in ("ok", "error", "denied", "timeout", "disconnected", "cancelled"):
        out = r.event(run_event("tool_finished", call_id=CID, tool="bash", status=status, duration_ms=2500))
        assert out and out[-1].style in STYLES and "bash" in out[-1].text
    assert "(2.5 s)" in out[-1].text


def test_an_edit_proposal_carries_a_collapsed_diff_and_expand_returns_all():
    r = Renderer(head_lines=3)
    new = "\n".join(f"n{i}" for i in range(40))
    blocks = r.event(proposed("edit_file", {"path": "a.py", "old_string": "old", "new_string": new}))
    diff = blocks[-1]
    assert diff.kind == "diff" and len(diff.lines) == 3 and diff.hidden > 0 and diff.ref == CID
    full = r.expand()
    assert full[0].kind == "diff" and full[0].hidden == 0 and len(full[0].lines) == diff.hidden + 3
    assert "no collapsed diff" in r.expand()[0].text      # nothing left to expand
    assert r.expand(CID)[0].kind == "diff"                 # but an explicit one still works


def test_very_large_diff_stays_small_when_collapsed():
    r = Renderer(read_existing=lambda p: Existing("missing"))
    big = "\n".join(f"line {i}" for i in range(30_000))
    blocks = r.event(proposed("write_file", {"path": "b.txt", "content": big}))
    assert len(blocks[-1].lines) <= 12 and blocks[-1].hidden > 1000
    assert "+30000 -0" in blocks[-1].text


def test_a_change_too_big_to_diff_shows_one_note_and_the_counts():
    r = Renderer(read_existing=lambda p: Existing("missing"))
    huge = "x" * 500 + "\n"
    blocks = r.event(proposed("write_file", {"path": "h.txt", "content": huge * 1000}))
    assert len(blocks[-1].lines) == 1 and blocks[-1].hidden == 0 and "+1000 -0" in blocks[-1].text


def test_write_file_uses_the_reader_for_the_current_text():
    r = Renderer(read_existing=lambda p: Existing("ok", "a\nb\n"))
    d = r.event(proposed("write_file", {"path": "f", "content": "a\nc\n"}))[-1]
    assert "+1 -1" in d.text


def test_state_changes():
    r = Renderer()
    budget = r.event(run_event("state_changed", state="paused_budget"))
    assert "press g" in text_of(budget).lower() and "$" not in text_of(budget)
    assert "new run" in text_of(r.event(run_event("state_changed", state="paused_error")))
    assert "reconnect" in text_of(r.event(run_event("state_changed", state="paused_disconnected")))
    assert "failed" in text_of(r.event(run_event("state_changed", state="failed")))
    assert "expired" in text_of(r.event(run_event("state_changed", state="expired")))
    assert r.event(run_event("state_changed", state="running")) == []
    assert r.event(run_event("state_changed", state="cancelled")) == []
    assert r.event(run_event("state_changed", state="awaiting_approval")) == []


def test_budget_warning_for_run_and_account_in_credits_only():
    r = Renderer()
    run = r.event(NS(type="budget_warning", subtype=None, payload={"scope": "run", "spent_pct": 80}))
    assert "80%" in text_of(run) and "this run" in text_of(run).lower()
    acct = r.event(NS(type="budget_warning", subtype=None,
                      payload={"scope": "account", "spent_pct": 90, "reset_at": "2026-12-01T00:00:00Z"}))
    assert "90%" in text_of(acct) and "2026-12-01T00:00:00Z" in text_of(acct)
    assert "$" not in text_of(run + acct)


def test_usage_tool_started_unknown_and_malformed_events_print_nothing():
    r = Renderer()
    assert r.event(run_event("usage", model="m", input_tokens=1, cached_input_tokens=0, output_tokens=1, credits=0.1)) == []
    assert r.event(run_event("tool_started", call_id=CID, tool="bash")) == []
    assert r.event(run_event("something_new")) == []
    assert r.event(NS(type="other", subtype=None, payload={})) == []
    assert r.event(NS(type="run_event", subtype=None, payload="not a dict")) == []


def test_hostile_server_text_never_reaches_a_block_unclean():
    r = Renderer()
    hostile = "\x1b[2J\x1b]0;t\x07[bold]x[/bold]\u202e"
    blocks = []
    blocks += r.event(run_event("text_delta", text=hostile))
    blocks += r.event(run_event("notice", code="c", severity="info", message=hostile))
    blocks += r.event(proposed("bash", {"command": hostile}))
    blocks += r.event(proposed("[red]evil", {"path": hostile}))
    blocks += r.event(run_event("tool_finished", call_id=hostile, tool="\x1b[31mx", status="ok"))
    for b in blocks:
        for t in [b.text] + [l.text for l in b.lines]:
            assert "\x1b" not in t and "\x07" not in t and "\u202e" not in t
    assert "unknown" in text_of(blocks)       # the unsafe tool name is replaced


def test_summaries_of_the_real_input():
    assert summarize_call("bash", {"command": "ls -la\nrm x"}) == "ls -la …"
    assert summarize_call("grep", {"pattern": "foo", "path": "src"}) == "foo in src"
    assert summarize_call("read_file", {"path": "a", "start_line": 3, "end_line": 9}) == "a:3-9"
    assert summarize_call("plan_exit", {"plan": "x"}) == "plan ready for review"
    assert summarize_call("mystery", {"a": 1}) == "" and summarize_call("bash", None) == ""
    assert len(summarize_call("bash", {"command": "x" * 1000})) <= 100


def test_memory_of_calls_and_diffs_is_bounded():
    r = Renderer()
    for i in range(500):
        r.event(proposed("edit_file", {"path": "a", "old_string": "a", "new_string": "b"}, call_id=f"c{i}"))
    assert r.summary_for("c0") is None and r.summary_for("c499") is not None and r.diff_for("c499") is not None


def test_error_blocks_use_fixed_text_and_numbers_only():
    out = error_blocks("rate_limited", {"retry_after_s": 12})
    assert out[0].style == "error" and "12 seconds" in out[0].text and "[error: rate_limited]" in out[0].text
    quota = error_blocks("quota_exhausted", {"reset_at": "2026-12-01T00:00:00Z"})
    assert "resets 2026-12-01T00:00:00Z" in quota[0].text and "$" not in quota[0].text
    odd = error_blocks("\x1b[31mevil", {"retry_after_s": "x"})
    assert "internal" in odd[0].text and "\x1b" not in odd[0].text
    for code in ("unauthorized", "device_revoked", "run_not_found", "provider_unavailable", "tool_timeout",
                 "invalid_message", "protocol_unsupported", "internal"):
        assert error_blocks(code)[0].text.startswith(f"[error: {code}]")
