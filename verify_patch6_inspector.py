"""
verify_patch6_inspector.py — run this from your repo root after applying
patch6_inspector.diff and filling in real GEMINI_API_KEY_10/_11 values in
your .env.

    cd MiniMe
    python verify_patch6_inspector.py

Same forced-failure shape as verify_patch4_structure_architect.py /
verify_patch5_documentation_agent.py: temporarily blanks
EO_INSPECTOR_GROQ_KEY_1 and _2 out of os.environ (in-process only — your
real .env file is never touched) so the first two CHAIN steps are
guaranteed to be skipped as "key not set", then calls the REAL
eo.inspector.classify() (not a raw generate_text() call) so this test
also exercises the JSON-shape validation classify() does on top of
whatever the model returns — a good deal more of the actual code path
than just confirming Gemini answered.

Unlike verify_patch5b_gemini_model_fix.py, no "import eo.registry first"
workaround is needed here: eo.inspector's own import chain (eo.structure,
relay.emitter, utils.llm_client) never touches agents.generic_worker, so
it doesn't hit that particular circular-import quirk. Confirmed by
importing eo.inspector standalone before writing this script.

Expected: prints the full classification dict (path/confidence/etc.) and
a confirmation line. Watch the "[Patch6Verification] ..." lines
generate_text() itself prints while running -- you should see both Groq
steps print "skipped — <key> not set", then either a Gemini step succeed
silently or (if you also want to test the true worst case) fail over to
EO_PANEL_GITHUB_PAT. If it raises instead, the fallback chain isn't
working -- recheck GEMINI_API_KEY_10/_11 are set and valid before
assuming the CHAIN edit itself is broken.
"""
import os
import sys
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

missing = [k for k in ("GEMINI_API_KEY_10", "GEMINI_API_KEY_11") if not os.getenv(k)]
if missing:
    raise SystemExit(
        "These Patch-6 keys are not set in your .env yet, fill in at least "
        f"one before running this: {', '.join(missing)}"
    )

import eo.inspector  # noqa: E402

# IMPORTANT: this pop happens AFTER the import above, not before.
# eo/inspector.py's own import chain pulls in memory/bus.py, which calls
# load_dotenv() itself at import time -- popping these first and
# importing second meant that import silently refilled them from .env
# (load_dotenv() only skips variables that are already set), so the
# "forced failure" never actually happened. Popping here, after that
# load_dotenv() call has already fired, is what actually simulates the
# outage. In-process only -- your real .env file is never touched.
os.environ.pop("EO_INSPECTOR_GROQ_KEY_1", None)
os.environ.pop("EO_INSPECTOR_GROQ_KEY_2", None)

print("--- CHAIN as eo/inspector.py will actually use it ---")
for step in eo.inspector.CHAIN:
    print(" ", step["provider"], step["key_env"])

print("\n--- calling eo.inspector.classify() (Groq forced unavailable) ---")
result = eo.inspector.classify(
    task_text=(
        "Build me a small Flask app with user auth, a Postgres-backed "
        "todo list, and a Docker setup."
    )
)

print("\n--- classify() result ---")
for k, v in result.items():
    print(f"  {k}: {v!r}")
print("------------------------")

assert result["path"] in {"instant", "direct", "fixed", "adaptive"}, (
    "classify() returned an invalid path -- see eo/inspector.py's own "
    "_validate() for what should have caught this before it got here."
)
print(
    "\nSchema looks valid. If that ran without raising, Patch 6's fallback "
    "worked -- the call had to fall through EO_INSPECTOR_GROQ_KEY_1 and _2 "
    "(both unset for this run) to reach GEMINI_API_KEY_10 or _11 (or, if "
    "those also failed, all the way to EO_PANEL_GITHUB_PAT)."
)
print(
    "Check your usage dashboard / Upstash for a fresh "
    "usage:gemini:GEMINI_API_KEY_10:<today> entry (or _11, if _10 also "
    "happened to fail) to confirm it was actually Gemini that answered on "
    "this run, not GitHub Models as the true last resort."
)
