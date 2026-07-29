"""
diagnose_mode_a_raw.py — drop this next to diagnose_mode_a.py in your
project root and run:

    python diagnose_mode_a_raw.py <workspace_id> <node_id>

Unlike diagnose_mode_a.py's step 5, this prints the ACTUAL raw text the
LLM returned before any parsing happens, plus which provider/model
answered, plus a line-by-line explanation of why _parse_mode_a_topics()
did or didn't find topics in it. This is the missing piece: step 5's
"SUCCESS — got 0 ops, 0 topic_ids" means parsing ran without crashing,
not that the LLM's answer was usable.
"""
import os
import sys
import json
import re

from dotenv import load_dotenv; load_dotenv()
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import agents.source_manager as sm
from eo.knowledge_graph import get_node
from eo.registry import get_role_prompt, add_role_prompt

_JSON_BLOCK_RE = sm._JSON_BLOCK_RE


def main(workspace_id, node_id):
    sm._ensure_role_registered()

    node = get_node(workspace_id, node_id)
    if not node:
        print("Could not find that node_id in this workspace.")
        return

    artifact = {"title": node.get("title", "Untitled"),
                "sections": [{"content": node.get("content", "")}]}
    pairs = sm._zipped_sections(artifact, [node_id])
    if not pairs:
        print("_zipped_sections returned nothing — node content/shape mismatch.")
        return

    context, id_map = sm._build_context(pairs, artifact["title"])
    print(f"Context length sent to the LLM: {len(context)} chars")
    print(f"Section ids the model is allowed to cite: {list(id_map.keys())}\n")

    # ---- call the LLM directly, same as _run_sequential_pass, but keep the raw text ----
    from agents.generic_worker import run as run_role
    result = run_role(
        role="source_manager", task_text=(
            "Extract this newly-ingested source's topic tree, per your "
            "instructions.\n\n" + context
        ),
        input_keys=[], session_id=None, include_conversation_context=False,
        domain="notes",
    )
    raw = result.get("text") or ""

    print("=" * 60)
    print("RAW LLM RESPONSE (this is what actually came back)")
    print("=" * 60)
    print(raw)
    print("=" * 60)
    print(f"\nRaw response length: {len(raw)} chars")

    # ---- walk _parse_mode_a_topics()'s own logic, explaining each gate ----
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        print("\n[DIAGNOSIS] No ```json fenced block found in the response.")
        print("  -> The regex is: ```json\\s*(.*?)\\s*```  (requires the literal")
        print("     'json' language tag right after the opening fence).")
        if "```" in raw:
            print("  -> The response DOES contain a ``` fence somewhere, just not")
            print("     tagged 'json' — this is almost certainly your bug: the")
            print("     model that answered this call didn't follow the exact")
            print("     fence format the parser requires.")
        else:
            print("  -> The response contains no fence at all. The model likely")
            print("     answered in prose instead of the required JSON format.")
        return

    print("\n[DIAGNOSIS] Found a ```json fenced block. Contents:")
    print(match.group(1))
    try:
        parsed = json.loads(match.group(1))
    except (json.JSONDecodeError, TypeError) as exc:
        print(f"\n[DIAGNOSIS] json.loads() FAILED: {exc}")
        print("  -> The fence exists but the JSON inside is malformed.")
        return

    if not isinstance(parsed, dict):
        print(f"\n[DIAGNOSIS] Parsed JSON is a {type(parsed).__name__}, not a dict.")
        return

    raw_topics = parsed.get("topics")
    if not isinstance(raw_topics, list):
        print(f"\n[DIAGNOSIS] parsed JSON has no usable 'topics' list. "
              f"Keys present: {list(parsed.keys())}")
        return

    print(f"\n[DIAGNOSIS] Found {len(raw_topics)} raw topic entries before "
          f"per-item validation (name/section-id checks).")
    if len(raw_topics) == 0:
        print("  -> The model itself decided this excerpt has no extractable "
              "topics. If the source content looks substantial to you, this "
              "means either the excerpt sent to the model was too thin/"
              "generic, or this particular model under-delivered on the task.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python diagnose_mode_a_raw.py <workspace_id> <node_id>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
