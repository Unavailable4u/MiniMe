# diag_facts.py — run from backend/ with your venv active
import sys
import json
sys.path.append(".")
from memory.bus import read

ws_id = "ws_1f95b84dd4"   # from your log line
facts = read(f"workspace_facts:{ws_id}", default={})
blob = json.dumps(facts)
print("total chars:", len(blob), "~tokens:", len(blob)//4)

for section, bucket in (facts.get("sections") or {}).items():
    entries = (bucket or {}).get("entries", {}) if isinstance(bucket, dict) else {}
    for key, entry in entries.items():
        t = (entry or {}).get("text") or ""
        if len(t) > 500:
            print(f"[{section}] {key}: {len(t)} chars")