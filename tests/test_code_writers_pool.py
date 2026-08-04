"""
test_code_writers_pool.py — throwaway script to verify Stage 6 step 5's
ThreadPoolExecutor concurrency in agents/code_writers.py directly, without
going through routing, the frontend, or Pusher.

Seeds memory.bus with 5 fake module specs (one per worker slot), then
calls code_writers.run() exactly as its own __main__ block does. Since
_write_one_module()'s print() calls happen regardless of the relay, if
the 5 workers are genuinely running in parallel you'll see their
"[Code Writer:CEREBRAS_API_KEY_N] module 'X' trying model: ..." lines
interleave across different keys/modules in the terminal, not complete
one full module before the next one starts.

Run from your project root (same place you'd run `python -m agents.code_writers`
or similar) so the `agents`/`memory` package imports resolve:

    python test_code_writers_pool.py

Delete this file once you've confirmed concurrency — it's not part of
the app, just a manual verification harness.
"""
import os
import sys
import time
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, read, KEYS
from agents import code_writers

# 5 small, independent modules -- one per worker/key slot -- so all 5
# genuinely have work to do at the same time, instead of 1-2 modules
# finishing instantly and leaving idle workers that look "sequential"
# just because there was nothing to overlap.
FAKE_MODULES = {
    "modules": [
        {
            "name": "fizzbuzz",
            "description": "print fizzbuzz for numbers 1 to 30",
            "language": "python",
            "inputs": "none",
            "outputs": "printed fizzbuzz sequence",
            "edge_cases": [],
            "constraints": [],
        },
        {
            "name": "is_palindrome",
            "description": "check if a given string is a palindrome, ignoring case and spaces",
            "language": "python",
            "inputs": "a string",
            "outputs": "boolean",
            "edge_cases": ["empty string", "single character"],
            "constraints": [],
        },
        {
            "name": "word_frequency",
            "description": "count word frequency in a block of text and return the top 5 most common words",
            "language": "python",
            "inputs": "a string of text",
            "outputs": "list of (word, count) tuples",
            "edge_cases": ["empty text", "punctuation attached to words"],
            "constraints": [],
        },
        {
            "name": "temperature_converter",
            "description": "convert a temperature between celsius, fahrenheit, and kelvin",
            "language": "python",
            "inputs": "value, from_unit, to_unit",
            "outputs": "converted value",
            "edge_cases": ["invalid unit name", "absolute zero boundary"],
            "constraints": [],
        },
        {
            "name": "matrix_transpose",
            "description": "transpose a 2D matrix given as a list of lists",
            "language": "python",
            "inputs": "a rectangular list of lists",
            "outputs": "the transposed matrix",
            "edge_cases": ["empty matrix", "non-rectangular input"],
            "constraints": [],
        },
    ]
}


def main():
    write(KEYS["module_specs"], FAKE_MODULES)
    print(f"[harness] seeded {len(FAKE_MODULES['modules'])} module specs, starting pool...\n")

    # Fake session_id/tier -- not a real routed task, but this is what
    # makes llm_client.py's _log_usage() actually fire (it's a documented
    # no-op when session_id is None). tier=2 since this pool is tier 2's
    # "refactor" agent per the registry.
    fake_session_id = "sess_harness_test"
    fake_tier = 2

    started = time.monotonic()
    results = code_writers.run(session_id=fake_session_id, tier=fake_tier)
    duration = time.monotonic() - started

    print(f"\n[harness] pool finished in {duration:.2f}s total.")
    print(f"[harness] if this were truly sequential, expect ~5x a single call's "
          f"latency; if concurrent, expect roughly 1x (all 5 overlapping).")
    for name, code in results.items():
        status = "OK" if not code.startswith("# CODE WRITER FAILED") else "FAILED"
        print(f"  - {name}: {status} ({len(code)} chars)")

    # Read back today's usage:cerebras:CEREBRAS_API_KEY_N:<date> entries
    # directly from Upstash -- this is the same data _log_usage() writes
    # and the same data the dashboard's usage_update events are built
    # from, just checked here without needing the frontend/Pusher open.
    print(f"\n[harness] usage logged today, per key (from memory.bus):")
    today = date.today().isoformat()
    for i in range(1, 6):
        key_env = f"CEREBRAS_API_KEY_{i}"
        db_key = f"usage:cerebras:{key_env}:{today}"
        try:
            entry = read(db_key, default=None)
        except Exception as exc:
            entry = None
            print(f"  - {key_env}: (read failed: {exc})")
            continue
        if entry:
            print(f"  - {key_env}: {entry.get('requests', 0)} requests, "
                  f"{entry.get('tokens', 0)} tokens")
        else:
            print(f"  - {key_env}: no usage logged yet")


if __name__ == "__main__":
    main()