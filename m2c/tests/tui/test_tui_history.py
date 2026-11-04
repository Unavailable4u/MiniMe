"""Patch C09a: the conversation rebuilt from the local log (m2c.tui.history)."""
import json

from m2c.link.client import _validate_outgoing
from m2c.tui.history import MAX_OUTPUT_CHARS, checkpoint_tools, rebuild_history

U, A, CALL, RES, CLOSED, CK, RW, UNSENT = ("user.message", "assistant.message", "tool.call", "tool.result",
                                           "tool.closed", "checkpoint", "rewind", "user.unsent")


def call(cid, tool="read_file", **args):
    return (CALL, {"call_id": cid, "tool": tool, "input": args or {"path": "a"}})


def done(cid, output="out", ok=True):
    return [(RES, {"call_id": cid, "ok": ok, "output": output}), (CLOSED, {"call_id": cid, "status": "ok"})]


def roles(history):
    return [i["role"] for i in history.items]


def valid(history, **extra):
    """The items must be accepted by the protocol's own run.start validation."""
    payload = {"mode": "ask", "cwd_label": "p", "history": history.items, **extra}
    try:
        _validate_outgoing("run.start", payload)
    except ValueError as exc:   # the other required fields differ by protocol revision; only history matters here
        assert "history" not in str(exc), exc


def test_a_plain_conversation():
    h = rebuild_history([(U, {"text": "hi"}), (A, {"text": "hello"}), (U, {"text": "bye"}), (A, {"text": "ok"})])
    assert h.items == [{"role": "user", "text": "hi"}, {"role": "assistant", "text": "hello"},
                       {"role": "user", "text": "bye"}, {"role": "assistant", "text": "ok"}]
    assert (h.omitted, h.shortened) == (0, 0)
    valid(h)


def test_a_tool_round_trip_is_an_assistant_item_then_its_tool_item():
    ev = [(U, {"text": "read it"}), (A, {"text": "reading"}), call("c1"), *done("c1", "file text"), (A, {"text": "it says x"})]
    h = rebuild_history(ev)
    assert roles(h) == ["user", "assistant", "tool", "assistant"]
    assert h.items[1]["tool_calls"] == [{"call_id": "c1", "tool": "read_file", "input": {"path": "a"}}]
    assert h.items[2] == {"role": "tool", "call_id": "c1", "ok": True, "output": "file text"}
    valid(h)


def test_parallel_calls_all_come_before_their_results_even_when_results_interleave():
    ev = [(U, {"text": "go"}), (A, {"text": ""}), call("c1"), *done("c1", "r1"), call("c2", path="b"), *done("c2", "r2")]
    h = rebuild_history(ev)
    assert roles(h) == ["user", "assistant", "tool", "tool"]
    assert [c["call_id"] for c in h.items[1]["tool_calls"]] == ["c1", "c2"]
    assert [i["call_id"] for i in h.items[2:]] == ["c1", "c2"]


def test_a_call_with_no_result_and_no_closing_is_dropped_never_left_dangling():
    ev = [(U, {"text": "go"}), (A, {"text": "trying"}), call("c1"), call("c2"), *done("c2", "kept")]
    h = rebuild_history(ev)
    assert [c["call_id"] for c in h.items[1]["tool_calls"]] == ["c2"]
    assert "c1" not in json.dumps(h.items)


def test_a_step_with_only_dangling_calls_and_no_text_vanishes():
    h = rebuild_history([(U, {"text": "go"}), (A, {"text": ""}), call("c1")])
    assert h.items == [{"role": "user", "text": "go"}]


def test_closed_without_output_gets_fixed_text_in_our_own_words():
    ev = [(U, {"text": "go"}), (A, {"text": "x"}), call("c1", "bash", command="ls"), (CLOSED, {"call_id": "c1", "status": "denied"})]
    h = rebuild_history(ev)
    assert h.items[2] == {"role": "tool", "call_id": "c1", "ok": False, "output": "The call was denied and did not run."}
    for status in ("cancelled", "timeout", "disconnected", "error", "ok"):
        item = rebuild_history([(U, {"text": "g"}), (A, {"text": ""}), call("c"), (CLOSED, {"call_id": "c", "status": status})]).items[2]
        assert item["ok"] == (status == "ok") and item["output"]
    odd = rebuild_history([(U, {"text": "g"}), (A, {"text": ""}), call("c"), (CLOSED, {"call_id": "c", "status": "weird"})])
    assert roles(odd) == ["user"]


def test_unsent_removes_the_last_user_message():
    h = rebuild_history([(U, {"text": "a"}), (A, {"text": "b"}), (U, {"text": "lost"}), (UNSENT, {})])
    assert [i["text"] for i in h.items] == ["a", "b"]
    assert rebuild_history([(UNSENT, {})]).items == []


def test_the_first_item_is_always_a_user_item():
    h = rebuild_history([(A, {"text": "orphan"}), (U, {"text": "hi"})])
    assert h.items == [{"role": "user", "text": "hi"}]


def steps_log():
    """user, then step 1 (edit a), step 2 (edit b), step 3 (edit c), each its own model step."""
    ev = [(U, {"text": "make changes"})]
    for n, cid in enumerate(("c1", "c2", "c3"), 1):
        ev += [(A, {"text": f"doing {n}"}), call(cid, "edit_file", path=f"f{n}"), *done(cid, f"edited {n}"),
               (CK, {"step": n, "call_id": cid, "tool": "edit_file"})]
    return ev


def test_without_a_rewind_marker_everything_is_kept():
    h = rebuild_history(steps_log() + [(A, {"text": "all done"})])
    assert roles(h).count("tool") == 3 and h.items[-1]["text"] == "all done"


def test_a_rewind_marker_cuts_to_just_before_the_model_step_of_that_call():
    h = rebuild_history(steps_log() + [(A, {"text": "all done"}), (RW, {"to_step": 2})])
    text = json.dumps(h.items)
    assert "doing 1" in text and "edited 1" in text
    assert "doing 2" not in text and "edited 3" not in text and "all done" not in text
    assert roles(h)[-1] == "tool"
    valid(h)


def test_rewind_to_the_first_step_leaves_only_what_came_before_it():
    h = rebuild_history(steps_log() + [(RW, {"to_step": 1})])
    assert h.items == [{"role": "user", "text": "make changes"}]


def test_what_happens_after_the_marker_is_kept_and_step_numbers_can_repeat():
    ev = steps_log() + [(RW, {"to_step": 2}), (U, {"text": "try differently"}), (A, {"text": "new 2"}),
                        call("d2", "edit_file", path="g"), *done("d2", "edited g"),
                        (CK, {"step": 2, "call_id": "d2", "tool": "edit_file"}), (RW, {"to_step": 2})]
    h = rebuild_history(ev)
    text = json.dumps(h.items)
    assert "new 2" not in text and "doing 1" in text        # cut before the model step that owns step 2 ...
    assert "try differently" in text                        # ... but the message that led to it stays
    ev2 = steps_log() + [(RW, {"to_step": 2}), (U, {"text": "try differently"}), (A, {"text": "new 2"})]
    assert "try differently" in json.dumps(rebuild_history(ev2).items)


def test_two_markers_in_a_row_cut_further_back():
    h = rebuild_history(steps_log() + [(RW, {"to_step": 3}), (RW, {"to_step": 2})])
    assert "doing 1" in json.dumps(h.items) and "doing 2" not in json.dumps(h.items)


def test_a_rewind_to_a_step_the_log_cannot_tie_to_a_call_cuts_conservatively():
    ev = [(U, {"text": "go"}), (A, {"text": "one"}), call("c1", "edit_file", path="a"), *done("c1"),
          (CK, {"step": 1, "call_id": "c1"}),
          (A, {"text": "two"}), call("c2", "write_file", path="b", content="x"),      # cut before its result: no checkpoint event
          (A, {"text": "three"}), call("c3", "edit_file", path="c"), *done("c3"), (CK, {"step": 3, "call_id": "c3"}),
          (RW, {"to_step": 2})]
    h = rebuild_history(ev)
    text = json.dumps(h.items)
    assert "one" in text and "two" not in text and "three" not in text


def test_a_checkpoint_event_without_a_call_id_attaches_to_the_latest_unmapped_checkpointed_call():
    ev = [(U, {"text": "go"}), (A, {"text": "one"}), call("c1", "edit_file", path="a"), *done("c1"), (CK, {"step": 1}),
          (A, {"text": "two"}), call("c2", "edit_file", path="b"), *done("c2"), (CK, {"step": 2}), (RW, {"to_step": 2})]
    h = rebuild_history(ev)
    assert "one" in json.dumps(h.items) and "two" not in json.dumps(h.items)


def test_checkpoint_tools_lists_the_tool_of_each_step():
    assert checkpoint_tools([(CK, {"step": 1, "tool": "edit_file"}), (CK, {"step": 2}), (U, {"text": "x"}),
                             (CK, {"step": 3, "tool": "write_file"})]) == {1: "edit_file", 3: "write_file"}


def test_a_big_tool_output_is_shortened_with_a_marker_and_flagged():
    big = "x" * (MAX_OUTPUT_CHARS * 3)
    h = rebuild_history([(U, {"text": "g"}), (A, {"text": ""}), call("c1"), *done("c1", big)])
    out = h.items[2]
    assert len(out["output"]) < MAX_OUTPUT_CHARS + 200 and out["truncated"] is True and "left out" in out["output"]
    assert h.shortened == 1
    valid(h)


def test_the_size_bound_drops_the_oldest_turns_whole_and_starts_at_a_user_item():
    ev = []
    for n in range(50):
        ev += [(U, {"text": f"question {n} " + "q" * 400}), (A, {"text": f"answer {n} " + "a" * 400})]
    h = rebuild_history(ev, max_bytes=10_000)
    assert 0 < len(h.items) < 100 and h.omitted == 100 - len(h.items)
    assert h.items[0]["role"] == "user" and "question 49" in h.items[-2]["text"]
    assert sum(len(json.dumps(i)) for i in h.items) <= 10_000


def test_a_tool_round_is_never_split_by_the_size_bound():
    ev = []
    for n in range(20):
        ev += [(U, {"text": f"u{n}"}), (A, {"text": f"a{n}"}), call(f"c{n}"), *done(f"c{n}", "o" * 500)]
    h = rebuild_history(ev, max_bytes=3000)
    seen = set()
    for item in h.items:
        if item["role"] == "assistant":
            seen |= {c["call_id"] for c in item.get("tool_calls", [])}
        if item["role"] == "tool":
            assert item["call_id"] in seen
    assert h.items[0]["role"] == "user"


def test_the_item_count_bound():
    ev = []
    for n in range(30):
        ev += [(U, {"text": "u"}), (A, {"text": "a"})]
    assert len(rebuild_history(ev, max_items=10).items) <= 10


def test_nothing_fits_gives_an_empty_history():
    assert rebuild_history([(U, {"text": "x" * 5000})], max_bytes=100).items == []


def test_garbage_in_the_log_is_ignored():
    ev = [(U, {"text": 5}), (U, "nope"), (A, None), (CALL, {"call_id": 1}), (RES, {"call_id": "zz"}), (CK, {"step": "x"}),
          (RW, {"to_step": "x"}), ("something.else", {}), (U, {"text": "ok"})]
    assert rebuild_history(ev).items == [{"role": "user", "text": "ok"}]
