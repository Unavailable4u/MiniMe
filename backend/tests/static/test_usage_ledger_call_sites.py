"""S01 call-site inventory: every caller of generate_text() / stream_completion()
and how its usage is attributed in the shared ledger.

WHY THIS TEST EXISTS. The ledger hook in utils/llm_client.py meters every real
call, but WHO the call is billed to comes from eo/usage_context.py, which only
knows what an entry point told it. A new LLM caller reached from a new,
un-scoped entry point silently meters as 'system' -- usage nobody is charged
for. This test pins the full list so adding a caller forces a decision here.

If it fails because you added or moved a caller: add the file below with the
right category, and if the caller is reachable from a route or job that does
not open a usage scope, either open one (usage_context.attributed / scope) or
mark it SYSTEM on purpose. Keep docs/usage-ledger-call-sites.md in step.

Categories:
  TASK      reached only through api/task_runner.py's run_task / preview_task /
            confirm_task / run_task_from_template (decorated with
            usage_context.attributed) or POST /api/resume (scoped in the route)
            -> attributed to the task's owner, surface 'web'.
  NOTEBOOK  reached through the notebook routes that take owner_id and are
            decorated with usage_context.attributed -> owner, surface 'web'.
  SYSTEM    attributed to surface 'system' (user_id NULL). Either deliberate
            (platform-initiated work) or a KNOWN GAP (a user-initiated route
            that has no owner in scope yet) -- see the notes.
  UNUSED    defined but with no caller in the codebase today.
"""
import ast
import pathlib
from collections import Counter

BACKEND = pathlib.Path(__file__).resolve().parents[2]
TARGETS = {"generate_text", "stream_completion"}

TASK, NOTEBOOK, SYSTEM, UNUSED = "TASK", "NOTEBOOK", "SYSTEM", "UNUSED"

# file -> (number of call sites, category)
EXPECTED = {
    # --- agents dispatched by the executor under a task's scope ----------
    "agents/architecture_diagrammer.py": (1, TASK),
    "agents/code_writer_lean.py": (1, TASK),
    "agents/code_writers.py": (2, TASK),
    "agents/content_adapter_pool.py": (2, TASK),
    "agents/dataset_analyst.py": (1, TASK),
    "agents/dependency_mapper.py": (1, TASK),
    "agents/deploy_config_writer.py": (1, TASK),
    "agents/documentation_agent.py": (1, TASK),
    "agents/extraction_table_builder.py": (1, TASK),
    "agents/fixer_pool.py": (1, TASK),
    "agents/generic_worker.py": (3, TASK),
    "agents/hardware_speccer.py": (4, TASK),
    "agents/idea_planner.py": (1, TASK),
    "agents/mech_primitive_pool.py": (1, TASK),
    "agents/mech_section_pool.py": (1, TASK),
    "agents/mech_subsection_pool.py": (1, TASK),
    "agents/output_organizer.py": (2, TASK),
    "agents/performance_reviewer.py": (1, TASK),
    "agents/prompt_writer.py": (1, TASK),
    "agents/prompt_writer_lean.py": (1, TASK),
    "agents/report_writer.py": (1, TASK),
    "agents/responder.py": (1, TASK),
    "agents/reviewer.py": (1, TASK),
    "agents/reviewer_fixer_lean.py": (1, TASK),
    "agents/schema_diagrammer.py": (1, TASK),
    "agents/security_scanner.py": (1, TASK),
    "agents/structure_architect.py": (2, TASK),
    "agents/test_writer.py": (1, TASK),
    # part_price_finder: TASK via hardware_speccer's run_gated pool (scope is
    # carried by eo/concurrency_gate.py), but ALSO called directly by
    # api/routes/workspace_data.py's price-refresh route, which has no owner in
    # scope -> that path meters as 'system'. KNOWN GAP.
    "agents/part_price_finder.py": (4, SYSTEM),
    # --- eo/ helpers called from the task path ----------------------------
    "eo/conversation_memory.py": (1, TASK),
    "eo/fact_summarizer.py": (1, TASK),
    "eo/inspector.py": (1, TASK),
    "eo/panel.py": (2, TASK),            # run_panel's two voter threads use usage_context.bind
    "eo/sga.py": (1, TASK),
    # --- notebook routes (decorated: notebooks_generate, _podcast, ...) ---
    "agents/note_table_builder.py": (1, NOTEBOOK),
    # --- not attributed to an account -------------------------------------
    # DELIBERATE: platform-initiated "self-improvement" research that runs on
    # its own pool, not a user action (agents/generic_worker.py's
    # _SKILL_SELF_IMPROVE_POOL).
    "eo/skill_library.py": (1, SYSTEM),
    # DELIBERATE: offline eval harness, never serves a user.
    "evals/promptfoo/providers/role_provider.py": (1, SYSTEM),
    # KNOWN GAP: upload routes (/api/notes/clip|video|import|pdf|voice) are
    # authenticated with dependencies=[Depends(require_auth)], so no owner id
    # reaches the handler to open a scope. Extracting it is a follow-up.
    "agents/source_manager.py": (1, SYSTEM),
    # --- LLM fallbacks with no caller today --------------------------------
    "eo/mech_mass.py": (1, UNUSED),
    "eo/mech_motion.py": (1, UNUSED),
    "eo/mech_thermal.py": (1, UNUSED),
}


def _scan() -> Counter:
    found = Counter()
    for path in sorted(BACKEND.rglob("*.py")):
        rel = path.relative_to(BACKEND).as_posix()
        if rel.startswith(("tests/", "scripts/")):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        aliases = set(TARGETS)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("llm_client"):
                aliases |= {a.asname for a in node.names if a.name in TARGETS and a.asname}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
            if name in aliases or (isinstance(f, ast.Attribute) and f.attr in TARGETS):
                found[rel] += 1
    # llm_client's own internals are the implementation, not callers
    found.pop("utils/llm_client.py", None)
    return found


def test_every_llm_caller_is_in_the_inventory():
    found = _scan()
    expected = {k: v[0] for k, v in EXPECTED.items()}
    new = sorted(set(found) - set(expected))
    gone = sorted(set(expected) - set(found))
    assert not new, (f"New generate_text/stream_completion caller(s) {new}: classify them in "
                     f"EXPECTED (see this file's docstring) so their usage is not silently 'system'.")
    assert not gone, f"Inventory lists files with no LLM call any more: {gone}"
    assert {k: found[k] for k in expected} == expected, "call-site count changed in a listed file"


def test_the_system_set_is_exactly_the_reviewed_one():
    """Moving a file into SYSTEM (or out of it) is a billing-relevant change
    and should be a conscious edit, not a side effect."""
    assert sorted(k for k, (_, c) in EXPECTED.items() if c == SYSTEM) == [
        "agents/part_price_finder.py", "agents/source_manager.py",
        "eo/skill_library.py", "evals/promptfoo/providers/role_provider.py",
    ]


def test_task_entry_points_open_a_usage_scope():
    """The TASK category rests on these decorators; if someone removes one,
    every LLM call under that entry point silently becomes 'system'."""
    text = (BACKEND / "api/task_runner.py").read_text(encoding="utf-8")
    for fn in ("run_task", "preview_task", "confirm_task", "run_task_from_template"):
        assert f'@usage_context.attributed("web")   # NEW — S01\ndef {fn}(' in text, fn
    assert 'usage_context.scope(owner_id, "web", req.session_id)' in (
        BACKEND / "api/routes/tasks.py").read_text(encoding="utf-8")
    nb = (BACKEND / "api/routes/notebooks.py").read_text(encoding="utf-8")
    for fn in ("notebooks_generate", "notebooks_podcast", "notebooks_video_overview",
               "topic_workflow_endpoint", "build_table_endpoint"):
        assert f'@usage_context.attributed("web")   # NEW — S01: meter this route\'s LLM calls to its owner\ndef {fn}(' in nb, fn
