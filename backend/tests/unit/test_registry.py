"""
tests/unit/test_registry.py — Part 5 checklist item 1, rebuilt around
the current eo/registry.py. resolve() maps an agent (module) NAME --
not a role name -- to a real callable, and raises KeyError with a clear
message for anything not in REGISTRY. resolve_role() (see
test_resolve_role.py) is the separate role-name -> module-name step
upstream of this.
"""
import pytest

from eo.registry import REAL_ACTION_ROLES, REGISTRY, resolve, resolve_role


def test_every_real_action_role_target_resolves_to_a_real_callable():
    for role_name, module_name in REAL_ACTION_ROLES.items():
        fn = resolve(module_name)
        assert callable(fn), f"{role_name} -> {module_name} did not resolve to a callable"


def test_generic_worker_resolves_to_a_real_callable():
    fn = resolve("generic_worker")
    assert callable(fn)


def test_role_to_callable_end_to_end():
    # resolve_role() then resolve() -- the two-step path every real
    # caller (eo/executor.py) actually takes.
    for role_name, expected_module in [
        ("implementer", "code_writers"),
        ("verifier", "reviewer"),
        ("fixer", "fixer_pool"),
        ("security_reviewer", "security_scanner"),
    ]:
        module_name = resolve_role(role_name)
        assert module_name == expected_module
        assert callable(resolve(module_name))


def test_unknown_agent_name_raises_keyerror_not_silent_none():
    with pytest.raises(KeyError):
        resolve("not_a_real_agent")


def test_registry_has_no_none_callables():
    for name, entry in REGISTRY.items():
        assert entry["callable"] is not None, f"{name} has a None callable"
        assert callable(entry["callable"]), f"{name}'s callable isn't callable"
