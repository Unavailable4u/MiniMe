"""
eo/loop_v4.py — Stage 4.2 of the roadmap (Part 10):

    "Add the Inspector, wired to always classify but forced to tier 3
    regardless of its own output — validates classification quality
    against real volume without affecting execution yet."

This is deliberately a THROWAWAY-SHAPED script for one purpose: run the
Inspector on every real task you throw at the system, log what it would
have decided, but always execute exactly what loop.py already does today.
Once Stage 4.3's fixture results (tests/test_eo_inspector.py) and enough
real `eo:task_classification` entries give you confidence, later stages
progressively let lower tiers actually take effect (Stage 4.4 onward) —
none of that logic lives here yet, on purpose.

loop.py itself is NOT imported-and-modified, only imported-and-called —
same file, same behavior, unconditionally, every time. That's the whole
point of this stage: prove the new layer observes without changing
anything, before it's trusted to change anything.

Usage (mirrors loop.py's own):
    python eo/loop_v4.py "a one-sentence idea for the app"
    python eo/loop_v4.py            (resumes an existing run, same as loop.py)

Manual override (Part 3) is a no-op right now — tier is ALWAYS forced to
3 at this stage regardless of --tier, since nothing below tier 3 exists
yet to route to. The flag is accepted (not rejected) so scripts/CI calling
it with --tier 3 today don't break when tier 0-2 execution lands later.
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo.inspector import classify
from eo.router import build_execution_graph
from memory.bus import write

FORCED_TIER = 3  # Stage 4.2: always 3, regardless of classify()'s output.


def _parse_task_arg(argv: list) -> tuple:
    """Strips a --tier N flag (accepted, currently ignored) out of argv,
    returns (task_text_or_None, remaining_argv_for_loop_py)."""
    args = list(argv)
    if "--tier" in args:
        i = args.index("--tier")
        # Currently ignored (see module docstring) — just consume it so
        # it doesn't get treated as part of the task text.
        del args[i:i + 2]
    task_text = " ".join(args) if args else None
    return task_text, args


def run_inspector_and_log(task_text: str) -> dict:
    """
    Classifies `task_text`, writes the Part 7 DB5 keys, and returns the
    classification dict. Never raises out to the caller on a classifier
    failure — a routing observation failing should not block the tier-3
    work it's only watching, at this stage. Logs the failure instead.
    """
    write("eo:original_task", task_text)

    try:
        decision = classify(task_text)
        write("eo:task_classification", decision)
        write("eo:routing_decision", {**decision, "forced_tier": FORCED_TIER})
        print(f"  [Inspector] would route: tier={decision['tier']} "
              f"directed_task_type={decision['directed_task_type']} "
              f"confidence={decision['confidence']:.2f} — {decision['reasoning']}")
    except Exception as exc:
        decision = None
        write("eo:task_classification", {"error": str(exc)})
        write("eo:routing_decision", {"error": str(exc), "forced_tier": FORCED_TIER})
        print(f"  [Inspector] classification failed ({exc.__class__.__name__}: {exc}) "
              f"— logged, proceeding with forced tier {FORCED_TIER} anyway.")
    if decision and decision["tier"] < FORCED_TIER:
        print(f"\n[EO] Inspector suggests tier {decision['tier']} "
              f"(confidence {decision['confidence']:.2f}: {decision['reasoning']}), "
              f"but this system is currently forced to tier {FORCED_TIER} "
              f"(~19 LLM calls, sandboxed testing, full commit cycle).")
        confirm = input("Proceed with the full tier-3 pipeline anyway? [y/N]: ").strip().lower()
        if confirm != "y":
            print("Aborted — nothing was run.")
            sys.exit(0)
    # Log what tier-3's execution graph looks like too, so eo:execution_graph
    # is populated from day one even though it's hardcoded right now.
    graph = build_execution_graph(tier=FORCED_TIER)
    write("eo:execution_graph", graph)

    return decision


def main():
    task_text, loop_argv = _parse_task_arg(sys.argv[1:])

    if task_text:
        print(f"[EO] Classifying task (forced tier {FORCED_TIER} regardless of result)...")
        run_inspector_and_log(task_text)
    else:
        print("[EO] No new task text — resuming an existing run, "
              "skipping classification (nothing new to classify).")

    print(f"[EO] Handing off to loop.py, tier {FORCED_TIER}, unmodified.\n")

    # Hand off to loop.py exactly as if it had been invoked directly:
    # same sys.argv shape it already expects, same process, same behavior.
    sys.argv = ["loop.py"] + loop_argv
    import loop
    loop.main()


if __name__ == "__main__":
    main()
