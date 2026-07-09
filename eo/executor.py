"""
eo/executor.py — Stage 4 steps 4-5 of the roadmap: the piece that
actually RUNS an execution graph from eo/router.py.

Migration Part 12 §3.3: this is the TRUE FINAL execute_graph() —
supersedes every prior version (Parts 2, 5, 8, 9, 10, 11) for real, not
just in the manifest's bookkeeping sense.

Migration Part 12 §3.1/§3.2: execution now navigates by ROLE, not by
resolved module name. This became necessary the moment generic_worker
started running for many different roles in the same plan (Part 10
§2.1's REAL_ACTION_ROLES split) — a "next_destination: <role>" value has
no way to disambiguate which generic_worker SLOT in agent_names it means,
since multiple slots are all literally the string "generic_worker".
eo/dispatcher.py's next_step() now returns an INDEX into role_names, not
a destination string; this module resolves agent_names[idx] separately
at the moment of actually calling a function.

Migration Part 12 §3.3 executor bug fix (found implementing this against
the real graph, not just the guide's illustrative snippet): when the
Dispatcher escalates to a genuinely new role (appends it to role_names),
agent_names has to grow in lockstep, or the next loop iteration indexes
past its end. Both agent_names and role_names are now local mutable
copies, and an escalation resolves the new role's module name via
eo.registry.resolve_role() before appending.

Backward compatibility: the "instant"/"direct"/"fixed" paths' static
graphs (build_execution_graph(), untouched since Part 2) have no separate
role concept -- role IS the module name there. role_names defaults to a
copy of agent_names in that case, so those callers are unaffected.
Hires-driven ("adaptive" path / Panel) calls always pass role_names
explicitly (eo.router.build_execution_graph_from_hires()).

Two agent names need special handling because they're entry points that
need the raw task text passed in directly the first time:
  - "responder"          (path "instant" — the only agent in its graph)
  - "prompt_writer_lean" (path "direct" — the first agent in its graph)
Every other agent name reads its input from memory.bus, since the agent
before it in the same graph already wrote it there.

Stage 6 step 4: each step fires agent_start/agent_done (and error, on
failure) through relay/emitter.py, per Part 6.3's schema. session_id
defaults to None, which makes emit_event() a documented no-op, so every
existing caller that doesn't pass a session_id keeps working with zero
behavior change.

Migration Part 5 §2.2: key_overrides maps a ROLE name (Part 11 §0 fixed
this to be role-keyed uniformly, not module-name-keyed) to the specific
key_env(s) the Panel hired for it, via
eo.router.build_execution_graph_from_hires(). Defaults to {}, so every
existing caller keeps today's exact behavior — each agent module falls
back to its own internal default key selection.

Migration Part 8 §8.3 / Part 9 fix: execute_graph() forwards
project_unique_name to file_manager.py's three callables (file_manager,
file_manager_writeback, file_manager_test_writeback) via a dedicated
dispatch case, instead of letting it silently fall through the generic
`else: fn()` branch and get dropped.

Migration Part 12 §8.4: `tier` (int) is renamed to `path` (str) throughout
this module's signature, emit_event() calls, and every fn(...) dispatch
call -- same positions, same meaning, new name only. One spot deliberately
NOT touched: the `__main__` block's `build_execution_graph(tier=0)` call
still passes the old numeric kwarg, since eo/router.py (not available at
the time of this rewrite) hasn't been confirmed to accept `path=` yet --
flagging rather than guessing at that module's signature.
"""
import os
import sys
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from eo.registry import resolve, resolve_role, list_known_roles
from eo.structure import PATH_TO_TIER
from eo.errors import MissingDependencyError
from relay.emitter import emit_event

TASK_TEXT_ENTRYPOINTS = {"responder", "prompt_writer_lean"}

# Bug fix / new capability: an agent can now raise MissingDependencyError
# instead of hard-failing the whole task when a prerequisite role's output
# isn't in memory yet (see eo/errors.py's module docstring for the full
# reasoning and the "adaptive path only" scoping). MAX_AUTO_INSERTS_PER_STEP
# guards against a genuinely unsatisfiable or circular dependency looping
# forever -- e.g. a role that requests itself, or two roles that each
# request the other. Keyed per (role, requested_role) pair rather than
# globally, so one step's retries don't eat another step's budget.
MAX_AUTO_INSERTS_PER_STEP = 2

# Migration Part 26 §5: these six agents are NOT special-cased below, so
# every prior call into them fell into the generic `else: fn()` branch --
# called with ZERO arguments. Their own run()/run_X() signatures default
# session_id/tier to None, so this never crashed, but it did mean every
# one of their generate_text()/usage-log calls was silently unscoped
# (session_id=None) even mid-session, and none of their calls were
# labeled with a real tier either. Per §1's own table, all six already do
# the right thing INTERNALLY with a `tier` int ("tier=tier where
# applicable -> correct") -- the gap was purely here, at the boundary,
# never wiring session_id/tier through to them at all.
#
# These six take `tier` (int), not `path` (str) -- unlike the boundary-A
# group above, none of them were part of the tier->path migration, so
# this dispatch case translates `path` back to `tier` via the same
# PATH_TO_TIER table eo/panel.py and eo/loop_v4.py use (now shared via
# eo/structure.py, Part 26 §4c) rather than asking these six to also
# migrate to `path` for no reason -- they never emit_event()'d with a
# path label to begin with (see §1's table: "not called with tier/path,
# or not called at all").
UNSCOPED_TIER_AGENTS = {
    "dependency_mapper", "documentation_agent", "duplication_checker",
    "memory_search",
}
# Migration Part 27: "final_qa" removed from this set -- its dedicated
# agents/final_qa.py module was deleted (reasoning-only, no live caller;
# see eo/registry.py's REGISTRY comment). The role name still resolves
# via generic_worker if the Panel hires it.
#
# Bug fix: "structure_architect" moved OUT of the set above into its own
# dispatch case below. Its new no-code planning path (structure_architect.py)
# needs task_text -- the original task description -- to plan a folder/file
# scaffold when there's no fixed_code/submitted_code to organize. The other
# four agents in UNSCOPED_TIER_AGENTS have no such need, so they're
# unaffected.


def _summarize(result, limit: int = 9000) -> str:
    """Best-effort human-readable summary for an agent_done payload.
    Results vary in shape (str, dict, ...) across the agent roster —
    eo/result_render.py's render_agent_result() is the one place that
    knows every shape (generic_worker's {"text"}, reviewer's {"issues"},
    fixer_pool's {"fixed_code"}, code_writers'/test_writer's flat
    {module: code} map, ...) and turns each into markdown instead of a
    raw Python dict repr.

    limit defaults to 9000, not the old 300 -- Pusher enforces a hard
    ~10KB payload limit per event (true on every plan, not just free
    tier), and the event envelope around this string (type, session_id,
    agent, path, timestamp) costs a few hundred bytes, so 9000 is the
    most we can send while leaving headroom. This is "full output" for
    the large majority of real agent results; only genuinely oversized
    ones (e.g. a full multi-module code submission) still get cut, and
    now say so explicitly instead of a bare '...'."""
    from eo.result_render import render_agent_result
    return render_agent_result(result, limit=limit)


def execute_graph(agent_names: list, role_names: list = None, task_text: str = None,
                   cycle_num: int = None, session_id: str = None, path: str = None,
                   mode: str = None, key_overrides: dict = None,
                   project_unique_name: str = None) -> dict:
    from eo.dispatcher import next_step

    # Migration Part 3 step 4 / Part 12 §3.3: reserve-account worker
    # pools only activate under Expert/Beast mode. Kept `mode=None` as
    # the default (not the guide snippet's literal "auto") so existing
    # callers that never pass mode keep working -- `(mode or "auto")`
    # gives the same "auto" semantics the guide specifies without
    # crashing on a bare .lower() call against None.
    expanded = (mode or "auto").lower() in ("expert", "beast")

    key_overrides = key_overrides or {}

    # Migration Part 12 §3.1/§3.2: role_names is now the Dispatcher's
    # real unit of navigation, not just a display label. Local mutable
    # copies -- role_names may get appended to on escalation (a
    # genuinely new role named via next_destination), and agent_names
    # must grow in lockstep right after, so neither can be the caller's
    # original list object.
    role_names = list(role_names) if role_names is not None else list(agent_names)
    agent_names = list(agent_names)

    results = {}
    idx = 0
    # Bug fix / new capability: how many times we've auto-inserted a given
    # (role, requested_role) pair to satisfy a MissingDependencyError.
    # Keyed per-pair, not globally -- see MAX_AUTO_INSERTS_PER_STEP's
    # comment above for why.
    auto_inserted = {}

    while idx is not None and idx < len(agent_names):
        current_name = agent_names[idx]
        role = role_names[idx]
        fn = resolve(current_name)
        # Migration Part 11 §0: key_overrides is always keyed by ROLE
        # name, not resolved agent/module name.
        override = key_overrides.get(role)

        print(f"  [Executor] running: {current_name} (role={role})")
        emit_event("agent_start", session_id=session_id, agent=current_name, path=path,
                    payload={"label": role})
        started = time.monotonic()
        try:
            if current_name == "prompt_writer_lean" and task_text:
                result = fn(task_text, session_id=session_id, path=path)
            elif current_name in TASK_TEXT_ENTRYPOINTS and task_text:
                # Part 23 fix: was `fn(task_text, key_override=override)` —
                # session_id/path never reached responder.py at all, so its
                # own Part 23 conversation-memory wiring (get_full_context())
                # had no session_id to work with even after that agent was
                # fixed to accept one. (prompt_writer_lean is also in
                # TASK_TEXT_ENTRYPOINTS but never reaches this branch — it's
                # caught by the dedicated `if` above, which already passed
                # both through.)
                result = fn(task_text, key_override=override, session_id=session_id, path=path)
            elif current_name == "code_writer_lean":
                result = fn(session_id=session_id, path=path)
            elif current_name == "reviewer_fixer_lean":
                result = fn(session_id=session_id, path=path)
            elif current_name == "code_writers":
                # NEW — bug fix: code_writers needs task_text as a
                # fallback seed for its own module_specs synthesis when
                # it's hired without "prompt_writer" ahead of it in the
                # plan (see agents/code_writers.py's
                # _derive_specs_from_task_text() docstring). Split out of
                # the shared branch below since "reviewer"/
                # "security_scanner" don't take task_text.
                result = fn(session_id=session_id, path=path, expanded=expanded,
                            key_override=override, task_text=task_text)
            elif current_name in ("reviewer", "security_scanner"):
                result = fn(session_id=session_id, path=path, expanded=expanded,
                            key_override=override)
            elif current_name == "fixer_pool":
                result = fn(session_id=session_id, path=path, key_override=override)
            elif current_name == "sandbox_tester_lean":
                result = fn(session_id=session_id, path=path)
            elif current_name == "structure_architect":
                # Bug fix: needs task_text now too (see UNSCOPED_TIER_AGENTS
                # comment above) -- its no-code planning path uses it to plan
                # a folder/file scaffold when there's no code to organize.
                result = fn(session_id=session_id, tier=PATH_TO_TIER.get(path), task_text=task_text)
            elif current_name in ("file_manager", "file_manager_writeback", "file_manager_test_writeback"):
                # Migration Part 8 §8.3 fix, preserved: kept the
                # three-name case rather than Part 12 §3.3's illustrative
                # single-name ("file_manager" only) snippet -- dropping
                # back to one name would silently reintroduce the exact
                # project_unique_name bug Part 8 fixed for the two
                # writeback callables.
                result = fn(project_unique_name=project_unique_name)
            elif current_name == "generic_worker":
                # Migration Part 10 §4 / Part 12 §3.2: `role` identifies
                # WHICH reasoning-only role this step is. input_keys is
                # "every role earlier than this one in the (possibly
                # runtime-escalated) plan" -- role_names[:idx] is a plain
                # slice since role_names is already in resolved execution
                # order.
                result = fn(role=role, task_text=task_text,
                            input_keys=role_names[:idx], session_id=session_id,
                            key_override=override)
            elif current_name in UNSCOPED_TIER_AGENTS:
                # Migration Part 26 §5 fix: was falling into the generic
                # `else: fn()` branch below (zero args). tier=None if path
                # itself is None/unrecognized -- these agents already
                # treat a None tier as "unscoped" today (that's the exact
                # gap being fixed, not a new failure mode), so this is
                # strictly additive: a real tier whenever one is known,
                # same silent-None fallback as before whenever it isn't.
                result = fn(session_id=session_id, tier=PATH_TO_TIER.get(path))
            else:
                result = fn()
        except MissingDependencyError as dep_exc:
            # New capability: an agent asked for a specific prerequisite
            # role instead of just hard-failing the task. Only attempt to
            # self-heal on the "adaptive" path -- that's the only mode
            # where role_names is a Panel-decided vocabulary a new role
            # can be spliced into; on instant/direct/fixed's statically-
            # built graphs (see eo/errors.py's docstring) this is a real
            # ordering bug, not a staffing gap, so it's re-raised as-is.
            needed_role = dep_exc.required_role
            pair = (role, needed_role)
            already_ran = needed_role in role_names[:idx]
            over_budget = auto_inserted.get(pair, 0) >= MAX_AUTO_INSERTS_PER_STEP
            if path != "adaptive" or already_ran or over_budget:
                emit_event("error", session_id=session_id, agent=current_name, path=path,
                            payload={"message": f"{dep_exc.__class__.__name__}: {dep_exc}"})
                raise
            auto_inserted[pair] = auto_inserted.get(pair, 0) + 1
            print(f"  [Executor] {current_name} (role={role}) requested prerequisite "
                  f"role '{needed_role}' — inserting it and retrying.")
            emit_event("agent_requested_role", session_id=session_id, agent=current_name, path=path,
                        payload={"label": f"{role} needs '{needed_role}' first — adding it to the plan",
                                 "requested_role": needed_role})
            role_names.insert(idx, needed_role)
            agent_names.insert(idx, resolve_role(needed_role))
            continue   # re-enter the loop at the same idx, now pointing at
                       # the newly inserted prerequisite step instead of
                       # the one that raised (which got shifted to idx+1).
        except Exception as exc:
            emit_event("error", session_id=session_id, agent=current_name, path=path,
                        payload={"message": f"{exc.__class__.__name__}: {exc}"})
            raise
        duration_ms = int((time.monotonic() - started) * 1000)
        # Migration Part 12 §3.3: results is now keyed by ROLE, not
        # module name -- results["generic_worker"] would otherwise
        # silently overwrite itself across multiple generic_worker hires
        # in the same plan (the same root cause as the dispatch
        # disambiguation fix above).
        results[role] = result
        print(f"  [Executor] done: {current_name}")
        emit_event("agent_done", session_id=session_id, agent=current_name, path=path,
                    payload={"summary": _summarize(result), "duration_ms": duration_ms})

        next_idx, reason = next_step(
            result if isinstance(result, dict) else {},
            role_names, idx, session_id=session_id,
            known_roles=set(list_known_roles()) | set(role_names),
        )

        # Migration Part 12 §3.3 executor bug fix: next_step() may have
        # appended a genuinely new role to role_names (escalation to a
        # role that wasn't in the original plan at all). agent_names
        # must grow in lockstep right here, or the `idx < len(agent_names)`
        # check above would pass on borrowed role_names length while
        # agent_names[idx] indexes past its own end next iteration.
        if next_idx is not None and next_idx >= len(agent_names):
            agent_names.append(resolve_role(role_names[next_idx]))

        idx = next_idx

    return results


if __name__ == "__main__":
    from eo.router import build_execution_graph
    graph = build_execution_graph(tier=0)
    print(execute_graph(graph, task_text="What is the capital of France?"))