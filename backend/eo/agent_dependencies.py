"""
eo/agent_dependencies.py — static ROLE -> prerequisite-ROLE(s) graph.

Patch 7.1 (Phase 7, static dependency graph). Every entry below is mined
directly from an existing `raise MissingDependencyError(required_role=...)`
call site in agents/*.py — grep for `MissingDependencyError(` across
agents/ to confirm this is the full current list. Pure data, zero new
judgment calls: if an agent already asks eo/executor.py's reactive
self-heal branch (eo/errors.py) for a role by name, that same
(role -> required_role) edge belongs here too.

Keyed and valued by ROLE name (eo/registry.py's vocabulary — what
role_names[idx] holds), not resolved module name, same convention the
reactive self-heal branch already uses (`role_names.insert(idx,
needed_role)`).

Deliberately excludes agents/deploy_agent.py's three
MissingDependencyError raises:
  - write_deploy_config() -> "deploy_config_writer": real edge, but
    deploy_agent is dispatched directly from a UI-button API endpoint
    (see eo/registry.py's REAL_ACTION_ROLES comment on
    "deploy_config_writer"), never through the Panel-hire/executor.py
    dispatch loop this graph feeds -- there's no role_names[idx] slot for
    it to ever be looked up against.
  - trigger_live_deploy() / register_uptimerobot_monitor() -> "deploy_agent":
    self-referential ("call this module's other function first"), not an
    inter-agent ordering dependency at all.

Only ever consulted on the "adaptive" path (see eo/executor.py's
proactive-check call site and eo/errors.py's module docstring for why:
role_names is a Panel-decided, spliceable vocabulary ONLY there --
"instant"/"direct"/"fixed" graphs are already in a statically-correct
fixed order, so a missing edge on those paths is a real bug, not a
staffing gap this graph should paper over).
"""

ID_PREFIX = "agent_deps"

AGENT_DEPENDENCIES = {
    # agents/test_writer.py's run() — needs submitted_code.
    "test_writer": ["implementer"],

    # agents/sandbox_tester.py's run_sandbox_tester() — needs
    # fixed_code/submitted_code (falls back to fixer_pool's output, but
    # the ultimate producer if neither exists yet is still the Code
    # Writers -- same reasoning fixer_pool.py/report_writer.py use below).
    "sandbox_tester": ["implementer"],

    # agents/fixer_pool.py's run() — needs submitted_code.
    "fixer_pool": ["implementer"],

    # agents/report_writer.py's run_report_writer() — needs
    # fixed_code/submitted_code.
    "report_writer": ["implementer"],

    # agents/file_manager.py's run() — needs a file_plan.
    "file_manager": ["structure_architect"],
    # Same underlying run(), reached via two other role names in the
    # fixed "debug"/"add_tests" DIRECTED_TASK_MAP entries (eo/router.py)
    # — kept here too since it's the exact same call site, even though
    # neither of those paths is "adaptive" (see module docstring above).
    "file_manager_writeback": ["structure_architect"],
    "file_manager_test_writeback": ["structure_architect"],

    # agents/sandbox_tester.py's run_sandbox_tester_lean() — needs
    # tier1_fixed_code. Tier-1 "lean" pipeline, path == "direct" (see
    # module docstring — inert here today, kept for completeness/any
    # future adaptive use).
    "sandbox_tester_lean": ["reviewer_fixer_lean"],

    # agents/reviewer_fixer_lean.py's run() — needs tier1_code.
    "reviewer_fixer_lean": ["code_writer_lean"],

    # agents/code_writer_lean.py's run() — needs tier1_module_spec.
    "code_writer_lean": ["prompt_writer_lean"],

    # agents/source_quality_flagger.py's run() — needs the
    # academic_search_report's papers.
    "source_quality_flagger": ["academic_search"],

    # agents/extraction_table_builder.py's run() — same academic_search
    # report dependency as source_quality_flagger above.
    "extraction_table_builder": ["academic_search"],

    # agents/citation_graph_builder.py's run() — same academic_search
    # report dependency again.
    "citation_graph_builder": ["academic_search"],

    # agents/contradiction_prefilter.py's run() — needs the extraction
    # table's rows.
    "contradiction_prefilter": ["extraction_table_builder"],

    # agents/architecture_diagrammer.py — needs prd_writer's PRD context.
    "architecture_diagrammer": ["prd_writer"],

    # agents/schema_diagrammer.py — same prd_writer dependency.
    "schema_diagrammer": ["prd_writer"],

    # agents/hardware_speccer.py — same prd_writer dependency.
    "hardware_speccer": ["prd_writer"],

    # agents/handoff_packager.py — same prd_writer dependency.
    "handoff_packager": ["prd_writer"],
}
