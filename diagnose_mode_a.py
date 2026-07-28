"""
diagnose_mode_a.py — run this from your MiniMe project root, inside your
activated venv:

    python diagnose_mode_a.py <workspace_id>

Get <workspace_id> from your browser: open DevTools > Network tab, click
into the "llll" notebook, and look at any /api/... request — the
workspace_id (or notebook id used as workspace_id) is in the URL or
request payload.

What this checks, in order, printing the REAL exception at every step
instead of swallowing it:
  1. Required env vars are present
  2. HuggingFace embedding call works (what write_node() needs)
  3. Today's quota/cooldown status for the 5 Groq keys tagged
     "source_manager" (what Mode A topic extraction needs)
  4. Every "source" node currently in the Knowledge Graph for this
     workspace, vs. every topic currently in Secondary Data for it —
     this is the real number behind "32 sources, ~1 shows in Backlinks"
  5. Optionally, re-run Mode A on ONE specific source with no
     try/except around it, so you see the actual traceback live.
"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def section(title):
    print(f"\n{'='*60}\n{title}\n{'='*60}")


def check_env():
    section("1. Environment variables")
    required = [
        "HUGGINGFACE_API_KEY",
        "GROQ_API_KEY_6", "GROQ_API_KEY_7", "GROQ_API_KEY_8",
        "GROQ_RESERVE_1", "GROQ_RESERVE_2",
        "UPSTASH_VECTOR_REST_URL", "UPSTASH_VECTOR_REST_TOKEN",
    ]
    missing = []
    for key in required:
        val = os.getenv(key)
        status = "SET" if val else "MISSING"
        if not val:
            missing.append(key)
        print(f"  {key:30s} {status}")
    if missing:
        print(f"\n  -> MISSING: {missing}")
        print("  These are the exact 5 keys Mode A's parallel-chunk path is "
              "restricted to (source_manager tag in eo/registry.py). If any "
              "are missing, that account simply never gets picked.")
    return missing


def check_embedding():
    section("2. HuggingFace embedding (write_node dependency)")
    try:
        from utils.embedding import embed_text
        vec = embed_text("diagnostic test string")
        print(f"  OK — got a {len(vec)}-dim vector back.")
    except Exception as exc:
        print(f"  FAILED: {exc!r}")
        print("  Every node write (write_node in eo/knowledge_graph.py) "
              "needs this to succeed. If this fails, sources ingest but "
              "NOTHING becomes searchable/linkable, regardless of Mode A.")


def check_quota():
    section("3. Groq quota/cooldown for source_manager-tagged keys")
    try:
        from eo.quota_sentinel import get_quota_snapshot
        snap = get_quota_snapshot()
    except Exception as exc:
        print(f"  Could not read quota snapshot: {exc!r}")
        return
    source_manager_keys = [
        "GROQ_API_KEY_6", "GROQ_API_KEY_7", "GROQ_API_KEY_8",
        "GROQ_RESERVE_1", "GROQ_RESERVE_2",
    ]
    all_exhausted = True
    for key in source_manager_keys:
        info = snap.get(key)
        if not info:
            print(f"  {key:20s} not found in snapshot (not in AGENT_CAPABILITIES as loaded)")
            continue
        pct = info["pct"]
        pct_str = f"{pct*100:.1f}%" if pct is not None else "n/a"
        cooling = " [COOLING DOWN]" if info["cooling_down"] else ""
        print(f"  {key:20s} used={info['used']:<8} quota={info['quota']} pct={pct_str}{cooling}")
        if (pct is not None and pct < 0.95) and not info["cooling_down"]:
            all_exhausted = False
    if all_exhausted:
        print("\n  -> ALL 5 source_manager-tagged Groq keys are near-exhausted "
              "or cooling down. This alone explains Mode A silently failing "
              "on every source uploaded after the first few.")
    else:
        print("\n  -> At least one key has headroom, so quota alone probably "
              "isn't the whole story — check step 5's live traceback too.")


def check_nodes_vs_topics(workspace_id):
    section(f"4. Source nodes vs. extracted topics for workspace_id={workspace_id!r}")
    try:
        from eo.knowledge_graph import list_nodes
        sources = list_nodes(workspace_id, node_type="source")
        print(f"  Source nodes in Knowledge Graph: {len(sources)}")
        for s in sources:
            print(f"    - {s.get('title', 'untitled')[:60]!r}  (node_id={s.get('node_id')})")
    except Exception as exc:
        print(f"  FAILED to list nodes: {exc!r}")
        sources = []

    try:
        from eo.secondary_data import get_secondary_data
        data = get_secondary_data(workspace_id)
        topics = data.get("topics", {})
        print(f"\n  Topics in Secondary Data (this is what Backlinks/Mind Map read): {len(topics)}")
    except Exception as exc:
        print(f"  FAILED to read secondary data: {exc!r}")
        topics = {}

    print(f"\n  -> {len(sources)} sources ingested, {len(topics)} topics extracted total.")
    if sources and len(topics) <= 3:
        print("     This confirms: ingestion (Sources tab) is working fine; "
              "Mode A topic extraction (feeds Backlinks/Mind Map) is the "
              "part failing for most of them.")


def retry_one_source(workspace_id, node_id):
    section(f"5. Live retry of Mode A on node_id={node_id!r} (no error swallowing)")
    try:
        from eo.knowledge_graph import get_node
        node = get_node(workspace_id, node_id)
        if not node:
            print("  Could not find that node_id in this workspace.")
            return
        print(f"  Re-running topic extraction against: {node.get('title')!r}")
        # Mirrors what _run_mode_a_topic_extraction does internally, but
        # WITHOUT the try/except, so any real exception prints its full
        # traceback here instead of a one-line [Source Manager] log.
        import agents.source_manager as sm
        artifact = {"title": node.get("title", "Untitled"),
                    "sections": [{"content": node.get("content", "")}]}
        pairs = sm._zipped_sections(artifact, [node_id])
        if not pairs:
            print("  _zipped_sections returned nothing — node content/shape mismatch.")
            return
        context, id_map = sm._build_context(pairs, artifact["title"])
        ops, topic_ids = sm._run_sequential_pass(context, id_map)
        print(f"  SUCCESS — got {len(ops)} ops, {len(topic_ids)} topic_ids.")
    except Exception:
        import traceback
        print("  REAL EXCEPTION (this is what step 3/4's print() lines hide):")
        traceback.print_exc()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python diagnose_mode_a.py <workspace_id> [node_id_to_retry]")
        sys.exit(1)
    workspace_id = sys.argv[1]

    check_env()
    check_embedding()
    check_quota()
    check_nodes_vs_topics(workspace_id)

    if len(sys.argv) > 2:
        retry_one_source(workspace_id, sys.argv[2])
    else:
        print("\n(Pass a node_id as a 2nd argument to see a live traceback "
              "for one specific failed source, e.g.:\n"
              f"  python diagnose_mode_a.py {workspace_id} <node_id from step 4>)")
