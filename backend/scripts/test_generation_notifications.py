"""
Notebooks Chat-First refinement, Phase 4 step 4.8.

"Test: trigger a generation from chat, confirm the notification appears
live with no polling/refresh."

SCOPE NOTE, same spirit as scripts/test_capability_coverage.py's own
module docstring: this does NOT import api/server.py. That module's
import chain pulls in agents/source_manager.py -> agents/voice_ingestor.py
-> faster_whisper (a heavy, unrelated optional dependency not needed for
anything this step touches) -- so importing it just to reach
notebooks_generate() would mean standing up the whole app's dependency
surface for a test of three lines inside one endpoint.

What this DOES exercise directly, with no mocking of the actual logic
under test: eo/notify.py's notify()/_deliver() and relay/emitter.py's
emit_event() -- the exact functions api/server.py's notebooks_generate()
(step 4.4) calls, with the exact event_type/payload shapes it passes.
The only things replaced are the two actual network sinks neither of
those files' own logic owns:

  - relay/emitter.py's Pusher client (_get_client()) -- swapped for a
    FakeChannelClient that just records .trigger() calls, so this runs
    with no PUSHER_* env vars and no real network call.
  - eo/notify.py's ws_registry push (_ws_push) -- swapped for a no-op
    recorder for the same reason (no real /ws/{session_id} socket to
    push to in a script).

This means a real bug in notify()'s validation, _deliver()'s dual-
transport mirroring, or emit_event()'s channel-naming/payload-shape
logic WOULD be caught here. What this can't catch -- and what step
4.8's own wording ("appears live with no polling/refresh") is really
asking about -- is the browser-side, live-Pusher-subscription part:
WorkspaceDockContext.jsx actually receiving these over a real Pusher
connection and the notification row (step 4.6) actually re-rendering
with no refresh. See the MANUAL VERIFICATION checklist printed at the
end of this script for that half.

Usage (bash):
    python scripts/test_generation_notifications.py

Usage (PowerShell):
    python scripts/test_generation_notifications.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eo.notify import notify  # the exact function notebooks_generate() calls
import relay.emitter as emitter


class FakeChannelClient:
    """Stands in for relay/emitter.py's real pusher.Pusher() client.
    Records every .trigger() call instead of hitting the network --
    same shape (channel_name, event_type, event_dict) the real client's
    .trigger() receives, so assertions below read the real call shape.
    """

    def __init__(self):
        self.calls = []

    def trigger(self, channel_name, event_type, event):
        self.calls.append((channel_name, event_type, event))


def _capability_label(target: str, labels: dict) -> str:
    """Mirrors api/server.py's _capability_label() (step 4.4) closely
    enough for this harness's purposes -- title-cased fallback for a
    target with no manifest entry, same as the real one."""
    return labels.get(target) or target.replace("_", " ").title()


# Small stand-in for api/server.py's CAPABILITIES_MANIFEST-derived
# _CAPABILITY_LABELS -- just the two keys this harness's scenarios need.
_LABELS = {"clusters": "Clusters", "study_guide": "Study guide"}


def _replay_notebooks_generate(session_id, ws_id, targets, results_by_target):
    """
    Replays notebooks_generate()'s own notify() call sequence (step 4.4)
    for a given (session_id, ws_id, targets) call, WITHOUT importing
    api/server.py -- see module docstring. `results_by_target` maps each
    target to either a result dict (success) or an Exception instance
    (what run_target() would have raised).

    Deliberately duplicates the real endpoint's control flow (started ->
    done/error per target, "not wired" -> immediate error) rather than
    importing it, so a mismatch between the two IS the thing this test
    would need to catch by inspection if api/server.py's real loop ever
    changes shape without this harness being updated to match -- same
    documented trade-off scripts/test_capability_coverage.py's
    REAL_MANIFEST already accepts.
    """
    branches = []
    for target in targets:
        label = _capability_label(target, _LABELS)
        outcome = results_by_target.get(target, "NOT_WIRED")
        if outcome == "NOT_WIRED":
            error = f"'{target}' isn't wired to Generate yet"
            branches.append({"panel_key": target, "status": "error", "error": error})
            notify(session_id, "generation_error",
                   {"panel_key": target, "workspace_id": ws_id, "label": error})
            continue
        notify(session_id, "generation_started",
               {"panel_key": target, "workspace_id": ws_id, "label": label})
        if isinstance(outcome, Exception):
            branches.append({"panel_key": target, "status": "error", "error": str(outcome)})
            notify(session_id, "generation_error",
                   {"panel_key": target, "workspace_id": ws_id, "label": str(outcome)})
        else:
            branches.append({"panel_key": target, "status": "done", "result": outcome})
            notify(session_id, "generation_done",
                   {"panel_key": target, "workspace_id": ws_id, "label": label})
    return branches


def main() -> None:
    fake_client = FakeChannelClient()
    any_issue = False

    with patch.object(emitter, "_get_client", return_value=fake_client), \
         patch.object(emitter, "_pusher_unavailable", False), \
         patch("eo.notify._ws_push") as fake_ws_push:

        session_id = "sess-test-123"
        ws_id = "ws-test-abc"

        print("=" * 70)
        print("SCENARIO 1: two targets, one succeeds, one raises")
        print("=" * 70)
        branches = _replay_notebooks_generate(
            session_id, ws_id, ["clusters", "study_guide"],
            {"clusters": {"status": "done_ok"}, "study_guide": RuntimeError("No sources to summarize.")},
        )
        print("branches:", branches)

        print("\n" + "=" * 70)
        print("SCENARIO 2: target not wired to Generate yet")
        print("=" * 70)
        _replay_notebooks_generate(session_id, ws_id, ["podcast"], {})

        print("\n" + "=" * 70)
        print("SCENARIO 3: session_id=None -- must no-op cleanly, no exception")
        print("=" * 70)
        _replay_notebooks_generate(None, ws_id, ["clusters"], {"clusters": {"status": "done_ok"}})

    # ------------------------------------------------------------------
    # Assertions
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("ASSERTIONS")
    print("=" * 70)

    calls = fake_client.calls
    expected_channel = f"session-{session_id}"

    def check(label, cond):
        nonlocal any_issue
        status = "OK" if cond else "FAIL"
        if not cond:
            any_issue = True
        print(f"  [{status}] {label}")

    check("exactly 5 Pusher .trigger() calls for scenarios 1+2 "
          "(clusters: started+done, study_guide: started+error, podcast: error)",
          len(calls) == 5)
    check("scenario 3 (session_id=None) emitted NO Pusher call",
          all(c[2].get("session_id") is not None for c in calls) and
          not any(c[2].get("payload", {}).get("panel_key") == "clusters" and
                  c[2].get("session_id") is None for c in calls))
    check("all calls landed on the expected session channel",
          all(c[0] == expected_channel for c in calls))
    check("clusters: started before done, both correct event_type",
          [c[1] for c in calls if c[2]["payload"]["panel_key"] == "clusters"]
          == ["generation_started", "generation_done"])
    check("study_guide: started then error, error label carries the exception message",
          [c[1] for c in calls if c[2]["payload"]["panel_key"] == "study_guide"]
          == ["generation_started", "generation_error"] and
          [c[2]["payload"]["label"] for c in calls
           if c[2]["payload"]["panel_key"] == "study_guide" and c[1] == "generation_error"]
          == ["No sources to summarize."])
    check("podcast (not wired): single generation_error, no generation_started",
          [c[1] for c in calls if c[2]["payload"]["panel_key"] == "podcast"]
          == ["generation_error"])
    check("every payload carries panel_key + workspace_id + label (step 4.3's shape)",
          all(set(c[2]["payload"].keys()) == {"panel_key", "workspace_id", "label"} for c in calls))
    check("ws_registry mirror (_ws_push) was ALSO called once per notify() "
          "(step 4.2's dual-transport _deliver(), still intact)",
          fake_ws_push.call_count == 5)  # scenario 3's session_id=None never reaches _deliver()

    print("=" * 70)
    if any_issue:
        print("One or more checks FAILED -- see above.")
        sys.exit(1)
    print("All checks passed: notify()/emit_event()'s generation_* wiring "
          "(steps 4.3/4.4) behaves exactly as notebooks_generate() expects.")

    print("\n" + "=" * 70)
    print("MANUAL VERIFICATION (the live-browser half this script can't cover)")
    print("=" * 70)
    print("""\
  1. Set real PUSHER_APP_ID/PUSHER_KEY/PUSHER_SECRET/PUSHER_CLUSTER env
     vars and run the actual API (uvicorn api.server:app --reload).
  2. Open a workspace's chat in the browser, open devtools' Network tab,
     confirm no polling requests to /api/workspaces/... fire on an
     interval (there shouldn't be any -- this is Pusher push, not a
     polling loop, but worth eyeballing once).
  3. Trigger a generation (chat-triggered, once Phase 2 lands, or via
     POST /api/workspaces/{ws_id}/notebooks/generate with a real
     session_id in the body for now).
  4. Confirm a GenerationNotificationRow pill appears in that chat's
     WorkspaceChatPanel within ~1s, with no page refresh or re-fetch --
     starts as a spinner, flips to a check (or error pill) when the
     matching generation_done/generation_error event lands.
  5. Repeat with a second, DIFFERENT open tab on the same workspace/chat
     to confirm both tabs update from the same Pusher event (session-
     scoped channel, not per-tab state).
""")


if __name__ == "__main__":
    main()
