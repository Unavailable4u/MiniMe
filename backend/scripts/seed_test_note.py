"""
Notebooks Chat-First refinement, Phase 6 step 6.11.e — test data seeder.

get_topic_related_notes() (eo/note_candidates.py) matches notes to a
topic by semantic similarity (search_nodes() against the topic's
name+summary), not a stored edge -- see that function's own docstring.
There is no manual "create a note for topic X" UI in this app yet: the
only path to an accepted note is agents/note_taker.py's
scan_conversation(), which proposes a note only when it judges the
recent chat contains a decision/action item worth keeping. Asking it to
"write a note about topic X" doesn't reliably trigger that (confirmed
2026-08-01: it returned NONE for exactly that prompt).

This script skips the LLM judgement entirely and calls
eo/note_candidates.py's propose_note() + accept_candidate() directly --
the same two calls scan_conversation()'s result would eventually
trigger via the UI's Accept button, just without waiting on an LLM to
decide something is worth saving. This is legitimate for testing
get_topic_related_notes()/search_nodes() specifically: that helper
only cares that a real node_type="note" node exists and is embedded,
not how it got there.

Usage (PowerShell):
    python scripts/seed_test_note.py <workspace_id> <topic_title> <topic_summary>

Example, matching the Salient Pole Rotor topic already in
ws_0d72d2f8cc:
    python scripts/seed_test_note.py ws_0d72d2f8cc `
      "Salient Pole Rotor" `
      "Used in low-to-medium speed alternators (<=1200 RPM); features a large diameter, short axial length, laminated projecting poles, and is unsuitable for high-speed operation due to mechanical stresses."

The note's title/content are built FROM the topic's own title+summary
(paraphrased, not copy-pasted verbatim) specifically so it's
semantically close enough for search_nodes() to actually surface it --
a generic placeholder note ("test note 123") would legitimately come
back with a near-zero similarity score and defeat the point of this
seed.

Prints the new node_id on success. Run
scripts/test_topic_related_notes.py right after with the SAME
workspace_id and that topic's topic_id (not the title/summary you
passed here) to confirm it's retrievable.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    if len(sys.argv) != 4:
        print("Usage: python scripts/seed_test_note.py <workspace_id> <topic_title> <topic_summary>")
        sys.exit(1)

    workspace_id, topic_title, topic_summary = sys.argv[1], sys.argv[2], sys.argv[3]

    from eo.note_candidates import accept_candidate, propose_note

    title = f"Notes: {topic_title}"
    content = (
        f"{topic_summary}\n\n"
        f"(Seeded test note for {topic_title} -- Phase 6 step 6.11.e "
        f"integration test, not a real study note.)"
    )

    print(f"Proposing a note for workspace_id={workspace_id!r}...")
    candidate = propose_note(
        workspace_id=workspace_id,
        title=title,
        content=content,
        tags=["test-seed", "6.11.e"],
        proposed_by="seed_test_note.py",
    )
    print(f"  proposed candidate_id={candidate['candidate_id']!r}")

    print("Accepting it into the real graph...")
    node_id = accept_candidate(workspace_id, candidate["candidate_id"])
    if node_id is None:
        print(
            "  accept_candidate() returned None -- the candidate was removed "
            "from the pending list, but write_node()'s embed/upsert itself "
            "failed. Check for a printed error above from write_node()."
        )
        sys.exit(1)

    print(f"  accepted -> node_id={node_id!r}")
    print(
        "\nNow run:\n"
        f"  python scripts/test_topic_related_notes.py {workspace_id} <topic_id>\n"
        "using the topic_id whose title/summary you passed in above."
    )


if __name__ == "__main__":
    main()
