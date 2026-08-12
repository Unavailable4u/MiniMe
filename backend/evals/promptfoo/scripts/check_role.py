"""
evals/promptfoo/scripts/check_role.py — D2 patch 7.

Wraps the manual check tests/_template.yaml's header comment has
documented, since patch 4, as a copy-paste `python3 -c "..."` one-liner
to run before picking a role_name for a new case file. Same check,
same two conditions, same reasoning for why REAL_ACTION_ROLES is the
wrong dict to test against (see role_provider.py's module docstring,
_reject_real_action_role()) -- just exposed as a real command instead
of something to retype/adapt by hand under time pressure each time.

_template.yaml's manual snippet is left in place, not deleted -- it's
still correct, and still useful if this script itself is ever
unavailable (no node/npm on hand, quick copy-paste from a GitHub view,
etc). This script is the easier path, not the only path.

Usage:
    npm run check-role -- <role_name>
    # or directly, from backend/evals/promptfoo/:
    python3 scripts/check_role.py <role_name>

Exit code 0 if role_name is testable by role_provider.py (and prints
how to proceed); exit code 1 if not (and prints why, matching what
_reject_real_action_role() would say at actual eval time -- the point
is finding this out BEFORE writing a whole case file and running
`npm run eval`, not after).
"""
import os
import sys

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))  # .../backend -- same depth as providers/role_provider.py,
                                     # so `eo`/`agents` import the same way it does


def check(role_name: str) -> bool:
    # Import eo.registry before agents.generic_worker (not the other way
    # around) -- eo/registry.py imports every REAL_ACTION_ROLES module at
    # module load time (including agents/generic_worker.py itself), so
    # importing generic_worker first can hit a circular-import ordering
    # error depending on what else has already been imported in this
    # process. role_provider.py's own call_api() never hits this because
    # promptfoo always imports eo.registry (via _reject_real_action_role)
    # before agents.generic_worker (via _build_live_chain) in the same
    # order used here.
    import eo.registry
    from eo.registry import REGISTRY, ROLE_PROMPTS_SEED
    from agents.generic_worker import run as generic_worker_run

    in_seed = role_name in ROLE_PROMPTS_SEED
    entry = REGISTRY.get(role_name)
    routes_to_generic_worker = entry is None or entry["callable"] is generic_worker_run

    print(f"role_name: {role_name!r}")
    print(f"  in ROLE_PROMPTS_SEED: {in_seed}")
    if entry is None:
        print("  in REGISTRY: no (absent entries fall through to generic_worker by default)")
    else:
        module_name = entry["callable"].__module__.rsplit(".", 1)[-1]
        tag = "generic_worker" if routes_to_generic_worker else "DEDICATED MODULE"
        print(f"  in REGISTRY: yes -> {module_name}.{entry['callable'].__name__} ({tag})")
    print(f"  routes to generic_worker: {routes_to_generic_worker}")

    ok = in_seed and routes_to_generic_worker
    if ok:
        print(f"\n\u2713 '{role_name}' is testable by role_provider.py.")
        print(f"  Next: cp tests/_template.yaml tests/{role_name}.yaml, fill it in, "
              f"add it to promptfooconfig.yaml's tests: list, then `npm run eval`.")
    else:
        print(f"\n\u2717 '{role_name}' is NOT testable by role_provider.py.")
        if not in_seed:
            print("  - not present in eo.registry.ROLE_PROMPTS_SEED -- get_role_prompt() "
                  "has no seed brief to fall back to for it.")
        if not routes_to_generic_worker:
            print("  - resolves to a dedicated module with its own hardcoded prompt/chain, "
                  "not something get_role_prompt() can fetch. role_provider.py's "
                  "_reject_real_action_role() will bounce any case file pointed at it -- "
                  "see that function's own docstring for why REAL_ACTION_ROLES isn't the "
                  "right dict to check this against by hand.")
    return ok


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: npm run check-role -- <role_name>", file=sys.stderr)
        return 2
    return 0 if check(sys.argv[1]) else 1


if __name__ == "__main__":
    sys.exit(main())
