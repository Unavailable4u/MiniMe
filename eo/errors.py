"""
eo/errors.py — MissingDependencyError.

A structured alternative to a bare `ValueError` for the one recurring
failure pattern across the real-action agents (agents/file_manager.py,
structure_architect.py, fixer_pool.py, test_writer.py, sandbox_tester.py,
report_writer.py, and the tier-1 lean pipeline): "the memory.bus key I
need doesn't exist yet, because the role that produces it hasn't run in
this plan." Before this, every one of those agents just raised
ValueError(f"No {thing} found in memory. Run {role} first.") and killed
the whole task -- correct as a safety check, but a dead end even when the
fix (run that role, then retry this one) is something the system could
plainly do for itself.

`required_role` must be a role name eo.registry.resolve_role() can
resolve -- either a REAL_ACTION_ROLES entry (its own dedicated module) or
any other name, which falls through to generic_worker(role=...). Either
way, eo/executor.py is the only thing that ever catches this.

Where this actually triggers auto-recovery: ONLY on the "adaptive" path
(path == "adaptive", Part 12+). That's the one execution mode where
`role_names` is a Panel-decided, independent-of-module-order vocabulary a
new role can be spliced into. On "instant"/"direct"/"fixed", the graph is
statically built by eo/router.py's build_execution_graph() in an already-
correct fixed order -- role_names there is just a copy of agent_names
(see eo/executor.py's own module docstring), so "insert missing role X"
has no sound meaning (X would just be a module name, not a staffable
role, and generic_worker(role=X) would be nonsensical for e.g.
"prompt_writer_lean"). Agents on those paths still raise this for a
consistent error shape, but executor.py re-raises it there as a real
failure -- hitting it on a statically-ordered graph means a genuine
internal ordering bug, not a staffing gap.
"""


class MissingDependencyError(Exception):
    """`required_role`: the role name (eo/registry.py's vocabulary) whose
    output this step needs but doesn't have yet. `reason`: optional
    human-readable detail; defaults to a generic message built from
    `required_role` if omitted."""

    def __init__(self, required_role: str, reason: str = None):
        self.required_role = required_role
        self.reason = reason or (
            f"missing prerequisite output — needs '{required_role}' to run first"
        )
        super().__init__(self.reason)