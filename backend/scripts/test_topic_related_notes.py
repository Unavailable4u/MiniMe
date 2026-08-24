"""
Notebooks Chat-First refinement, Phase 6 step 6.11.e.

"Small helper: filter existing notes down to ones tied to that topic's
covered node ids -- test against a topic with real notes."

Unlike scripts/test_topic_covered_sources.py (step 6.11.d), this CANNOT
be a fully seeded/offline unit test: eo/note_candidates.py:
get_topic_related_notes() calls eo/knowledge_graph.py:search_nodes(),
which does a real embedding call (utils/embedding.py:embed_text(),
needs HUGGINGFACE_API_KEY) and a real Upstash Vector query
(memory/bus.py:vector_index(), needs UPSTASH_VECTOR_REST_URL /
UPSTASH_VECTOR_REST_TOKEN) -- there's no local fake for either. This is
a manual integration check against one of your real workspaces, same
category as scripts/test_tool_calling.py needing a live GROQ_API_KEY,
not a repeatable CI-style unit test.

You need a workspace_id that already has:
  - at least one topic in eo/secondary_data.py's store (Source
    Manager/Backlink Detector create these on ingest)
  - at least one accepted note (a graph node with node_type="note" --
    i.e. something that's been through
    eo/note_candidates.py:accept_candidate(), not just proposed)
for this to have anything real to find.

Usage (bash):
    export HUGGINGFACE_API_KEY=your_key_here
    export UPSTASH_VECTOR_REST_URL=your_url_here
    export UPSTASH_VECTOR_REST_TOKEN=your_token_here
    python scripts/test_topic_related_notes.py <workspace_id> <topic_id>

Usage (PowerShell):
    $env:HUGGINGFACE_API_KEY = "your_key_here"
    $env:UPSTASH_VECTOR_REST_URL = "your_url_here"
    $env:UPSTASH_VECTOR_REST_TOKEN = "your_token_here"
    python scripts/test_topic_related_notes.py <workspace_id> <topic_id>

Run with no arguments to just list available (workspace_id, topic_id)
pairs from your local secondary-data store, so you don't have to go
digging for one by hand.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _list_available_topics():
    from eo.secondary_data import _read  # deferred, see note_candidates.py's
    # own precedent for late-importing store internals from a script
    doc = _read()
    if not doc:
        print("No workspaces found in local secondary-data store.")
        return
    print("Available (workspace_id, topic_id) pairs:\n")
    for ws_id, ws_doc in doc.items():
        topics = ws_doc.get("topics", {})
        if not topics:
            continue
        print(f"  workspace_id={ws_id!r}")
        for tid, t in topics.items():
            print(f"    topic_id={tid!r}  name={t.get('name')!r}  "
                  f"covers={len(t.get('source_section_ids') or [])} section(s)")
        print()


def main() -> None:
    if len(sys.argv) != 3:
        print("Usage: python scripts/test_topic_related_notes.py <workspace_id> <topic_id>\n")
        _list_available_topics()
        sys.exit(0)

    workspace_id, topic_id = sys.argv[1], sys.argv[2]

    from eo.note_candidates import get_topic_related_notes

    print(f"Querying notes related to topic_id={topic_id!r} in workspace_id={workspace_id!r}...\n")
    try:
        notes = get_topic_related_notes(workspace_id, topic_id, top_k=10)
    except KeyError:
        print(f"topic_id {topic_id!r} not found in workspace {workspace_id!r}'s scope. "
              f"Run with no arguments to list valid pairs.")
        sys.exit(1)
    except ValueError as exc:
        print(f"Bad input: {exc}")
        sys.exit(1)

    if not notes:
        print("No related notes found. This can mean:\n"
              "  - the topic genuinely has no accepted notes yet (valid, not a bug)\n"
              "  - the topic has no name/summary to query with (get_topic_related_notes\n"
              "    returns [] in that case rather than erroring)\n"
              "  - search_nodes() degraded to [] after a Vector/embed hiccup (see its\n"
              "    own printed error above, if any)")
        return

    print(f"Found {len(notes)} related note(s), most similar first:\n")
    for i, n in enumerate(notes, 1):
        title = n.get("title", "(untitled)")
        score = n.get("score")
        content = (n.get("content") or "")[:120].replace("\n", " ")
        print(f"  {i}. [{score:.4f}] {title!r}  node_id={n.get('node_id')!r}" if score is not None
              else f"  {i}. {title!r}  node_id={n.get('node_id')!r}")
        print(f"       {content}{'...' if len(n.get('content') or '') > 120 else ''}")


if __name__ == "__main__":
    main()
