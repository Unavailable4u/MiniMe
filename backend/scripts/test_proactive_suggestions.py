"""
Notebooks Chat-First refinement, Phase 3 step 3.9.

"End-to-end test with real topics/generations."

Exercises step 3.8's session-dedupe ("don't repeat the same nudge/pairing
twice in a session") on the backend half of Phase 3 directly and with no
mocking of the logic under test: eo/prerequisite_suggestions.py's
find_prerequisite_suggestions(), _already_nudged(), and _mark_nudged().

SCOPE NOTE, same posture scripts/test_generation_notifications.py already
documents for its own boundary: this does NOT import api/server.py or
api/task_runner.py (task_runner's own import chain pulls in
agents/source_manager.py -> agents/voice_ingestor.py -> faster_whisper,
same heavy unrelated dependency that script already avoids). What this
DOES cover, unmocked: the real find_prerequisite_suggestions() candidate
loop, including step 3.5's untouched-topic filter and step 3.8's new
per-session pairing dedupe, against a hand-built packet shaped exactly
like eo/source_index.py:get_packet()'s real return value. The only thing
replaced is get_packet() itself (swapped for a fixed fake packet) and
panel_content.get_content()/knowledge_graph.list_nodes() (swapped for
empty results) — neither of those is Phase 3 logic, they're Phase 3's
own upstream data sources, same "don't stand up the whole DB/graph to
test three lines of filtering" reasoning the existing scripts already
use.

What this can't catch, and what step 3.9's own wording ("real
topics/generations") is really asking about, is: (a) the browser-side
half of step 3.8 (WorkspaceDockContext.jsx's shownSuggestionPairs dedupe
for the post-generation cross-sell suggestion), and (b) the full
chat -> nudge/suggestion -> one-tap accept -> re-dispatch loop against a
live workspace. See the MANUAL VERIFICATION checklist printed at the end
of this script for both.

Usage (bash):
    python scripts/test_proactive_suggestions.py

Usage (PowerShell):
    python scripts/test_proactive_suggestions.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import eo.prerequisite_suggestions as ps
from eo.prerequisite_suggestions import find_prerequisite_suggestions

# ----------------------------------------------------------------------
# Fixed fake packet — shaped exactly like eo/source_index.py:get_packet()
# would return for a small workspace with:
#   "recursion" --prerequisite-of--> "dynamic_programming" --prerequisite-of--> "memoization_patterns"
# and one unrelated pair with no prerequisite edge at all.
# ----------------------------------------------------------------------
_FAKE_TOPICS = {
    "recursion": {"name": "Recursion", "summary": "Functions that call themselves.", "covers": ["n1"]},
    "dynamic_programming": {"name": "Dynamic Programming", "summary": "Overlapping subproblems.", "covers": ["n2"]},
    "memoization_patterns": {"name": "Memoization Patterns", "summary": "Caching subproblem results.", "covers": ["n3"]},
    "graph_theory": {"name": "Graph Theory", "summary": "Nodes and edges.", "covers": ["n4"]},
}
_FAKE_CONNECTIONS = [
    {"relation": "prerequisite-of", "from_topic": "recursion", "to_topic": "dynamic_programming"},
    {"relation": "prerequisite-of", "from_topic": "dynamic_programming", "to_topic": "memoization_patterns"},
]


def _fake_get_packet(workspace_id, scope="project", session_id=None):
    return {"topics": dict(_FAKE_TOPICS), "connections": list(_FAKE_CONNECTIONS)}


def main() -> None:
    any_issue = False

    def check(label, cond):
        nonlocal any_issue
        status = "OK" if cond else "FAIL"
        if not cond:
            any_issue = True
        print(f"  [{status}] {label}")

    with patch.object(ps, "get_packet", _fake_get_packet), \
         patch.object(ps, "_topic_workflow_topic_ids", return_value=set()), \
         patch.object(ps, "_note_node_ids", return_value=set()):

        ws = "ws-test-abc"

        print("=" * 70)
        print("SCENARIO 1: first turn discussing Dynamic Programming — should")
        print("offer Recursion as a prerequisite (fresh session, nothing nudged yet)")
        print("=" * 70)
        session_a = "sess-a"
        first = find_prerequisite_suggestions(ws, ["n2"], session_id=session_a)
        print("suggestions:", [(s["topic_id"], s["for_topic_id"]) for s in first])
        check("first call for session A returns the recursion->dynamic_programming pairing",
              any(s["topic_id"] == "recursion" and s["for_topic_id"] == "dynamic_programming"
                  for s in first))

        print("\n" + "=" * 70)
        print("SCENARIO 2: SAME session, next turn, still discussing Dynamic")
        print("Programming — step 3.8 says do NOT repeat the identical pairing")
        print("=" * 70)
        second = find_prerequisite_suggestions(ws, ["n2"], session_id=session_a)
        print("suggestions:", [(s["topic_id"], s["for_topic_id"]) for s in second])
        check("second call, same session, same pairing: suppressed (empty)",
              len(second) == 0)

        print("\n" + "=" * 70)
        print("SCENARIO 3: a DIFFERENT session asking about the same topic — must")
        print("NOT be suppressed by session A's history (dedupe is per-session)")
        print("=" * 70)
        session_b = "sess-b"
        third = find_prerequisite_suggestions(ws, ["n2"], session_id=session_b)
        print("suggestions:", [(s["topic_id"], s["for_topic_id"]) for s in third])
        check("fresh session B still gets the recursion->dynamic_programming pairing",
              any(s["topic_id"] == "recursion" and s["for_topic_id"] == "dynamic_programming"
                  for s in third))

        print("\n" + "=" * 70)
        print("SCENARIO 4: back in session A, a DIFFERENT pairing (memoization_patterns'")
        print("own prerequisite, dynamic_programming) — different pairing, same session,")
        print("must still be offered (dedupe is per-pairing, not per-session-wide)")
        print("=" * 70)
        fourth = find_prerequisite_suggestions(ws, ["n3"], session_id=session_a)
        print("suggestions:", [(s["topic_id"], s["for_topic_id"]) for s in fourth])
        check("session A still gets the DIFFERENT dynamic_programming->memoization_patterns pairing",
              any(s["topic_id"] == "dynamic_programming" and s["for_topic_id"] == "memoization_patterns"
                  for s in fourth))

        print("\n" + "=" * 70)
        print("SCENARIO 5: session_id=None — must never suppress (no session to key off)")
        print("=" * 70)
        fifth_a = find_prerequisite_suggestions(ws, ["n2"], session_id=None)
        fifth_b = find_prerequisite_suggestions(ws, ["n2"], session_id=None)
        check("session_id=None, called twice in a row, both return the pairing",
              len(fifth_a) > 0 and len(fifth_b) > 0)

    # ------------------------------------------------------------------
    # Internal bookkeeping sanity checks (_already_nudged/_mark_nudged
    # directly, independent of the packet-shaped scenarios above)
    # ------------------------------------------------------------------
    print("\n" + "=" * 70)
    print("SCENARIO 6: _already_nudged()/_mark_nudged() unit-level behavior")
    print("=" * 70)
    check("not nudged before being marked",
          ps._already_nudged("sess-x", "topicA", "topicB") is False)
    ps._mark_nudged("sess-x", "topicA", "topicB")
    check("nudged after being marked",
          ps._already_nudged("sess-x", "topicA", "topicB") is True)
    check("a DIFFERENT (from_id, to_id) pair in the same session is unaffected",
          ps._already_nudged("sess-x", "topicA", "topicC") is False)
    check("the same pair in a DIFFERENT session is unaffected",
          ps._already_nudged("sess-y", "topicA", "topicB") is False)

    print("=" * 70)
    if any_issue:
        print("One or more checks FAILED — see above.")
        sys.exit(1)
    print("All checks passed: step 3.8's per-session pairing dedupe behaves as the")
    print("guide's own wording asks for ('never repeated for the same pairing in a")
    print("session'), on the related-topic nudge (eo/prerequisite_suggestions.py) half.")

    print("\n" + "=" * 70)
    print("MANUAL VERIFICATION (the two halves this script can't cover)")
    print("=" * 70)
    print("""\
  A. Post-generation cross-sell dedupe (WorkspaceDockContext.jsx's
     shownSuggestionPairs, step 3.8's other half):
       1. Open a workspace, generate flashcards from chat (or the picker,
          pre-step-2.9), accept or dismiss the "generate a quiz too?"
          suggestion — either way, note it appeared once.
       2. Generate flashcards again in the SAME chat session (regenerate,
          same panel_key). Confirm the "generate a quiz too?" suggestion
          does NOT appear a second time in this session.
       3. Refresh the page / open a new chat in the same workspace
          (a new dock key). Generate flashcards again — confirm the
          suggestion DOES appear again, since dedupe is scoped to a
          session's dock state, not persisted across sessions (matches
          the backend nudge's own per-session, in-memory scope tested
          above, and the guide's own open-questions wording: "never
          repeated for the same pairing in a session").
       4. Toggle "Proactive suggestions" off in SettingsTab.jsx, repeat
          step 1's scenario — confirm no suggestion message is appended
          at all (step 3.7's existing gate, unaffected by this change).

  B. Full end-to-end loop, real topics/generations (step 3.9's own ask):
       1. In a real workspace with at least two topics connected by a
          real "prerequisite-of" backlink edge (upload sources that
          naturally produce one, or confirm an existing pair via the
          Mind Map view), ask the chatbox a question that grounds on the
          DEPENDENT topic only.
       2. Confirm the assistant's answer is followed by a
          PrerequisiteSuggestions offer naming the correct prerequisite
          topic and the correct "for" topic.
       3. Ask a related follow-up question in the same chat that still
          grounds on the same dependent topic. Confirm the identical
          offer does NOT repeat (step 3.8, live).
       4. Trigger a real generation (e.g. study_flashcards) from chat,
          confirm the "generate a quiz too?" cross-sell appears once,
          accept it, confirm it dispatches the equivalent chat command
          (step 3.3's re-send path) rather than opening a second,
          separate execution path.
       5. Regenerate the same target again in the same session, confirm
          that cross-sell does NOT repeat (step 3.8, live, cross-sell
          half).
""")


if __name__ == "__main__":
    main()
