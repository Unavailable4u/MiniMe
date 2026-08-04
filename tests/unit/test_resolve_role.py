"""
tests/unit/test_resolve_role.py — rebuilt around the current
eo/registry.py, whose resolve_role() no longer raises KeyError for an
unmapped role (Migration Part 10 §2.1 replaced the old ROLE_TO_AGENT /
KeyError design entirely). Every real-action role listed in
REAL_ACTION_ROLES resolves to its dedicated module name; every other
role name -- known or brand new -- resolves to the literal string
"generic_worker".
"""
from eo.registry import resolve_role, REAL_ACTION_ROLES


def test_unmapped_role_falls_back_to_generic_worker():
    unmapped_role = "some_role_nobody_ever_mapped_xyz"
    assert unmapped_role not in REAL_ACTION_ROLES, (
        "test role accidentally collides with a real mapping"
    )
    assert resolve_role(unmapped_role) == "generic_worker"


def test_real_action_role_resolves_to_its_dedicated_module():
    assert resolve_role("implementer") == "code_writers"
    assert resolve_role("verifier") == "reviewer"
    assert resolve_role("fixer") == "fixer_pool"


def test_every_real_action_role_resolves_to_its_own_mapping():
    for role_name, expected_module in REAL_ACTION_ROLES.items():
        assert resolve_role(role_name) == expected_module


def test_retired_dedicated_modules_now_fall_back_to_generic_worker():
    # Migration Part 27: changelog_writer, final_qa, and gatekeeper's
    # dedicated agent modules were retired. Their role names are still
    # valid for the Panel to hire, but -- not being in
    # REAL_ACTION_ROLES -- they now resolve straight to generic_worker
    # instead of a module that no longer exists.
    for role_name in ("changelog_writer", "final_qa", "gatekeeper"):
        assert role_name not in REAL_ACTION_ROLES
        assert resolve_role(role_name) == "generic_worker"
