"""
m2c/tests/runner/test_runner_audit_logging.py -- Patch C24: what is recorded locally, and what is never logged.

Build plan 11.4: a log line holds a tool NAME, a call id, a decision and an error code, and nothing else: no input, no
output, no path, no command.  The local audit record (events type ``approval`` + the ``approvals`` table row) holds the
tool, the decision, the scope, who decided and when; its ``input_summary`` column is empty by design.
"""
from __future__ import annotations

import json
import logging
import re

from runner_rig import MARKER, RUN_ID, capture_runner_logs, make_rig, run

from m2c.runner import Choice
from m2c.store import read_events


def test_every_decision_is_recorded_without_the_input(tmp_path):
    rig = make_rig(tmp_path, allow=["bash(echo:*)"], ask_fn=lambda request: Choice("allow", "session") if request.tool == "bash" else Choice("deny"))
    (rig.root / f"{MARKER}.txt").write_text(MARKER)

    async def scenario():
        await rig.call("c1", "read_file", {"path": f"{MARKER}.txt"})                     # allowed by the mode
        await rig.call("c2", "bash", {"command": f"echo {MARKER}"}, "R2")               # allowed by a rule
        await rig.call("c3", "bash", {"command": f"printf {MARKER}"}, "R2")             # asked; allowed for the session
        await rig.call("c4", "write_file", {"path": f"{MARKER}.out", "content": MARKER}, "R1")   # asked; denied
        await rig.call("c5", "nonexistent_tool", {"x": MARKER})                          # denied by policy
    run(scenario())
    assert rig.approvals_rows() == [
        ("c1", "read_file", "allow", "once", "policy", ""),
        ("c2", "bash", "allow", "once", "policy", ""),
        ("c3", "bash", "allow", "session", "user", ""),
        ("c4", "write_file", "deny", "once", "user", ""),
        ("c5", "nonexistent_tool", "deny", "once", "policy", ""),
    ]
    events = [e for e in read_events(rig.store, RUN_ID) if e.type == "approval"]
    assert [e.data["call_id"] for e in events] == ["c1", "c2", "c3", "c4", "c5"]
    assert all(set(e.data) <= {"call_id", "tool", "decision", "scope", "decided_by", "edited"} for e in events)
    dump = json.dumps([e.data for e in read_events(rig.store, RUN_ID)]) + str(rig.approvals_rows())
    assert MARKER not in dump
    for row in rig.store.execute("SELECT ts FROM approvals"):
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d+Z", row[0])


def test_an_edited_allow_is_recorded_as_edited(tmp_path):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", edited_input={"command": "echo edited"}))
    run(rig.call("c1", "bash", {"command": "echo original"}, "R2"))
    (event,) = [e for e in read_events(rig.store, RUN_ID) if e.type == "approval"]
    assert event.data["edited"] is True and "edited" not in json.dumps({k: v for k, v in event.data.items() if k != "edited"})


def test_a_call_whose_decision_cannot_be_recorded_is_not_run(tmp_path):
    """A later step cannot pretend an approval happened: no record, no run (fail closed)."""
    ran = []
    rig = make_rig(tmp_path, mode="accept-edits", executor=lambda *a, **k: ran.append(1))
    rig.store.close()                                                                     # the store is gone
    run(rig.call("c1", "write_file", {"path": "a.txt", "content": "x"}, "R1"))
    assert ran == [] and not (rig.root / "a.txt").exists()
    assert rig.link.for_call("c1", "tool_result") == []


def test_logs_hold_no_input_output_or_path_text(tmp_path, caplog, monkeypatch):
    logs = capture_runner_logs(monkeypatch)
    rig = make_rig(tmp_path, mode="accept-edits", allow=["bash(echo:*)"],
                   ask_fn=lambda request: Choice("allow", edited_input={"command": f"echo edited-{MARKER}"}))
    (rig.root / f"{MARKER}-dir").mkdir()
    (rig.root / f"{MARKER}-dir" / "file.txt").write_text(f"contents {MARKER}\n")
    caplog.set_level(logging.DEBUG)

    async def scenario():
        await rig.call("c1", "read_file", {"path": f"{MARKER}-dir/file.txt"})
        await rig.call("c2", "grep", {"pattern": MARKER})
        await rig.call("c3", "write_file", {"path": f"{MARKER}-dir/new.txt", "content": MARKER}, "R1")
        await rig.call("c4", "bash", {"command": f"echo {MARKER}"}, "R2")
        await rig.call("c5", "bash", {"command": f"curl http://{MARKER}.invalid"}, "R2")         # asked, edited, run
        await rig.call("c6", "edit_file", {"path": f"../{MARKER}", "old_string": MARKER, "new_string": MARKER}, "R1")
        await rig.call("c7", "unknown_tool_" + "x", {"input": MARKER})
    run(scenario())
    assert MARKER not in caplog.text and MARKER.split("-")[0] not in caplog.text
    assert MARKER not in logs.text and MARKER.split("-")[0] not in logs.text
    assert logs.lines, "the runner logs nothing at all"
    joined = logs.text
    assert "call_id=c1" in joined and "tool=read_file" in joined and "decision=allow" in joined


def test_log_lines_use_only_safe_values(tmp_path, monkeypatch):
    """A tool name the server made up never reaches a log line as text, and a line is one line."""
    logs = capture_runner_logs(monkeypatch)
    rig = make_rig(tmp_path)
    run(rig.call("c1", "evil tool name\nINJECTED line", {"x": 1}))
    assert logs.lines and "INJECTED" not in logs.text and all("\n" not in line for line in logs.lines)
    assert any("tool=unknown" in line for line in logs.lines)
