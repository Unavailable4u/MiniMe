# Usage ledger: LLM call-site inventory (S01)

Every real LLM call writes one row to `usage_ledger` (migration `0012`) from the
hook in `utils/llm_client.py`, next to `_record_call_log()`. **Who the row is
billed to** is not known there; it comes from `eo/usage_context.py`, which only
knows what an entry point told it. A caller reached from an entry point that
opens no scope is stored as `surface='system'`, `user_id=NULL`.

This is the inventory the S01 work package requires: 54 call sites of
`generate_text()` / `stream_completion()` in 41 files (found by AST scan, so
docstring mentions don't count). `tests/static/test_usage_ledger_call_sites.py`
pins it, so a new caller fails CI until someone classifies it.

## How attribution reaches a call

| Entry point | Mechanism |
|---|---|
| `run_task`, `preview_task`, `confirm_task`, `run_task_from_template` (`api/task_runner.py`) | `@usage_context.attributed("web")` reads `owner_id` / `session_id` from the call; `attach_session()` binds ids minted inside |
| `POST /api/resume` (`api/routes/tasks.py`) | `usage_context.scope(owner_id, "web", session_id)` around `run_in_agent_pool` (which copies the context to the worker) |
| Notebook generators: `notebooks_generate`, `notebooks_podcast`, `notebooks_video_overview`, `topic_workflow_endpoint`, `build_table_endpoint` (`api/routes/notebooks.py`) | `@usage_context.attributed("web")` |
| Raw `ThreadPoolExecutor` workers (~20 modules) | contextvars do **not** cross into pool threads, so: calls carrying a `session_id` are matched to the live scope by that id; `run_gated()` (`eo/concurrency_gate.py`) re-enters the caller's scope; `run_panel`'s two voter threads use `usage_context.bind()` |

If two different users have live scopes on the same `session_id` (two partners
running tasks at once in one shared chat), a pool-thread call that can only be
matched by `session_id` is attributed to `system` rather than guessed.

## Carry an owner (attributed to the account, surface `web`)

27 agents run under a task's scope: `architecture_diagrammer`, `code_writer_lean`,
`code_writers`, `content_adapter_pool`, `dataset_analyst`, `dependency_mapper`,
`deploy_config_writer`, `documentation_agent`, `extraction_table_builder`,
`fixer_pool`, `generic_worker`, `hardware_speccer`, `idea_planner`,
`mech_primitive_pool`, `mech_section_pool`, `mech_subsection_pool`,
`output_organizer` (incl. `stream_completion`), `performance_reviewer`,
`prompt_writer`, `prompt_writer_lean`, `report_writer`, `responder`, `reviewer`,
`reviewer_fixer_lean`, `schema_diagrammer`, `security_scanner`,
`structure_architect`, `test_writer`.

Plus `eo/conversation_memory.py`, `eo/fact_summarizer.py`, `eo/inspector.py`,
`eo/panel.py`, `eo/sga.py`, and `agents/note_table_builder.py` (notebook route).

## Attributed to `system`

| File | Why | Kind |
|---|---|---|
| `eo/skill_library.py` | Platform self-improvement research on its own pool (`_SKILL_SELF_IMPROVE_POOL`), not a user action | deliberate |
| `evals/promptfoo/providers/role_provider.py` | Offline eval harness | deliberate |
| `agents/source_manager.py` | Upload routes (`/api/notes/clip`, `video`, `import`, `pdf`, `voice`) authenticate via `dependencies=[Depends(require_auth)]`, so no owner id reaches the handler | **known gap** |
| `agents/part_price_finder.py` | Attributed when reached through the task path (`hardware_speccer`), but the price-refresh route in `workspace_data.py` has no owner in scope | **known gap** |

Closing the two gaps means giving those handlers an `owner_id` parameter
(`owner_id: str = Depends(require_auth)`) and decorating them with
`@usage_context.attributed("web")`, then moving the files to `NOTEBOOK`/`TASK`
in the test.

## Defined but never called

`eo/mech_mass.py`, `eo/mech_motion.py`, `eo/mech_thermal.py` have LLM fallbacks
(`estimate_mass`, `estimate_motion`, `estimate_thermal_and_vibration`) with no
caller today. They will meter correctly under a task scope if wired up.

## Not covered by this work package

`stream_completion()` is wired (hook in the `"done"` branch) but its only
caller, `output_organizer.organize_final_answer_stream`, is not used by any
route today. Calls that bypass `generate_text` / `stream_completion` (embeddings
via `embed_text`) are not metered by this ledger.
