"""
tests/unit/test_eo_agent_dependencies.py — Patch 7e-S5.

eo/agent_dependencies.py had zero test coverage before this. It's a
pure static data module (a ROLE -> prerequisite-ROLE(s) dict, no
functions of its own) consumed by eo/executor.py's reactive self-heal
branch, so there's no business logic to exercise -- what's actually
worth pinning down is the data's own shape contract, since executor.py
trusts every value here to already be a list of role-name strings
without re-validating it at the call site (see executor.py's own
`for needed_role in AGENT_DEPENDENCIES.get(role, ())` — a malformed
entry here (a bare string instead of a list, an empty list, a
non-string role name) would either raise or silently no-op deep inside
executor.py's dispatch loop instead of surfacing as an obviously-wrong
value in this module.

Also pins down the specific entries the module's own docstring commits
to (mined from `MissingDependencyError(required_role=...)` call sites)
and the three deploy_agent.py exclusions the docstring explicitly
calls out as deliberate, so a future edit doesn't quietly re-add or
drop one of them without the change being visible in a diff here.
"""
from eo import agent_dependencies

# ---------------------------------------------------------------------
# Shape contract — every entry must already be safely iterable by
# executor.py's dispatch loop without further validation
# ---------------------------------------------------------------------

def test_agent_dependencies_is_a_dict():
    assert isinstance(agent_dependencies.AGENT_DEPENDENCIES, dict)


def test_agent_dependencies_is_non_empty():
    assert len(agent_dependencies.AGENT_DEPENDENCIES) > 0


def test_every_key_is_a_non_empty_string():
    for role in agent_dependencies.AGENT_DEPENDENCIES:
        assert isinstance(role, str)
        assert role.strip() != ""


def test_every_value_is_a_non_empty_list():
    for role, deps in agent_dependencies.AGENT_DEPENDENCIES.items():
        assert isinstance(deps, list), f"{role!r}'s deps must be a list, got {type(deps)}"
        assert len(deps) > 0, f"{role!r} has an empty dependency list"


def test_every_dependency_entry_is_a_non_empty_string():
    for role, deps in agent_dependencies.AGENT_DEPENDENCIES.items():
        for dep in deps:
            assert isinstance(dep, str), f"{role!r} has a non-string dependency: {dep!r}"
            assert dep.strip() != ""


def test_no_role_lists_itself_as_its_own_prerequisite():
    """A direct self-dependency would put executor.py's self-heal branch
    into an infinite insert loop."""
    for role, deps in agent_dependencies.AGENT_DEPENDENCIES.items():
        assert role not in deps, f"{role!r} lists itself as its own dependency"


def test_id_prefix_is_a_non_empty_string():
    assert isinstance(agent_dependencies.ID_PREFIX, str)
    assert agent_dependencies.ID_PREFIX.strip() != ""


# ---------------------------------------------------------------------
# Specific documented entries — mined from real
# MissingDependencyError(required_role=...) call sites per the
# module's own docstring
# ---------------------------------------------------------------------

def test_test_writer_depends_on_implementer():
    assert agent_dependencies.AGENT_DEPENDENCIES["test_writer"] == ["implementer"]


def test_sandbox_tester_depends_on_implementer():
    assert agent_dependencies.AGENT_DEPENDENCIES["sandbox_tester"] == ["implementer"]


def test_fixer_pool_depends_on_implementer():
    assert agent_dependencies.AGENT_DEPENDENCIES["fixer_pool"] == ["implementer"]


def test_report_writer_depends_on_implementer():
    assert agent_dependencies.AGENT_DEPENDENCIES["report_writer"] == ["implementer"]


def test_file_manager_family_depends_on_structure_architect():
    for role in ("file_manager", "file_manager_writeback", "file_manager_test_writeback"):
        assert agent_dependencies.AGENT_DEPENDENCIES[role] == ["structure_architect"]


def test_tier1_lean_pipeline_chain():
    deps = agent_dependencies.AGENT_DEPENDENCIES
    assert deps["sandbox_tester_lean"] == ["reviewer_fixer_lean"]
    assert deps["reviewer_fixer_lean"] == ["code_writer_lean"]
    assert deps["code_writer_lean"] == ["prompt_writer_lean"]


def test_academic_search_dependents():
    deps = agent_dependencies.AGENT_DEPENDENCIES
    assert deps["source_quality_flagger"] == ["academic_search"]
    assert deps["extraction_table_builder"] == ["academic_search"]
    assert deps["citation_graph_builder"] == ["academic_search"]


def test_contradiction_prefilter_depends_on_extraction_table_builder():
    assert agent_dependencies.AGENT_DEPENDENCIES["contradiction_prefilter"] == ["extraction_table_builder"]


def test_prd_writer_dependents():
    deps = agent_dependencies.AGENT_DEPENDENCIES
    for role in ("architecture_diagrammer", "schema_diagrammer", "hardware_speccer", "handoff_packager"):
        assert deps[role] == ["prd_writer"]


# ---------------------------------------------------------------------
# Deliberate exclusions, per the module's own docstring
# ---------------------------------------------------------------------

def test_deploy_config_writer_edge_is_deliberately_excluded():
    """deploy_agent is dispatched directly from a UI-button endpoint,
    never through the Panel-hire/executor.py loop this graph feeds --
    there's no role_names[idx] slot for it to ever be looked up
    against."""
    assert "deploy_config_writer" not in agent_dependencies.AGENT_DEPENDENCIES


def test_deploy_agent_self_referential_edge_is_deliberately_excluded():
    """trigger_live_deploy()/register_uptimerobot_monitor()'s
    MissingDependencyError is self-referential ("call this module's
    other function first"), not a real inter-agent ordering dependency."""
    assert "deploy_agent" not in agent_dependencies.AGENT_DEPENDENCIES
