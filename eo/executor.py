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
from eo.registry import resolve, resolve_role
from relay.emitter import emit_event

TASK_TEXT_ENTRYPOINTS = {"responder", "prompt_writer_lean"}


def _summarize(result, limit: int = 300) -> str:
    """Best-effort human-readable summary for an agent_done payload.
    Results vary in shape (str, dict, ...) across the agent roster, so
    this is deliberately forgiving rather than assuming a schema."""
    if isinstance(result, str):
        text = result
    elif isinstance(result, dict):
        text = result.get("code") or result.get("answer") or str(result)
    else:
        text = str(result)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "..."


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
                result = fn(task_text, key_override=override)
            elif current_name == "code_writer_lean":
                result = fn(session_id=session_id, path=path)
            elif current_name == "reviewer_fixer_lean":
                result = fn(session_id=session_id, path=path)
            elif current_name in ("code_writers", "reviewer", "security_scanner"):
                result = fn(session_id=session_id, path=path, expanded=expanded,
                            key_override=override)
            elif current_name == "fixer_pool":
                result = fn(session_id=session_id, path=path, key_override=override)
            elif current_name == "sandbox_tester_lean":
                result = fn(session_id=session_id, path=path)
            elif current_name == "gatekeeper":
                result = fn(cycle_num, session_id=session_id, path=path)
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
            else:
                result = fn()
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