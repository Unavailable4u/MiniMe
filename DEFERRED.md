# Deferred ideas

Running log of things explicitly flagged as "not now" during the
CLI-as-Internal-Interface work (see
`MiniMe-CLI-Internal-Architecture-Plan.md` and its patch breakdown,
`MiniMe-Patch-Series-B-Plan.md`), kept in one place so reviewing
everything deferred is one glance instead of grepping the whole
codebase.

Convention: when a patch flags something inline as out of scope, add a
one-line entry here in the same commit, pointing back to the inline
comment (`file:line`) that flagged it.

## Deferred at the start of Patch Series B (architecture plan §3.6)

- **The daemon (external project access).** Separate feature, separate
  risk profile — touches a person's own filesystem outside this
  system, not just internal capability access. Comes later, with its
  own hard ceiling on what it can run.
- **Write/execute capability on the backend's own code.** Deferred
  until the atomicity/isolation problem in the architecture plan's §4
  (live bind-mounted `./backend:/app`, interrupted-write risk) has a
  real design answer — atomic writes (temp file + validate + swap) and
  probably a verify-then-promote working copy, neither designed yet.
- **Anything beyond architecture plan §3.1–§3.5.** No formal scope
  boundary drawn yet for letting agents write, call MCP tools
  directly, or manage their own skill library. Flag each such idea
  inline as it comes up during the build; this file is the rollup.
- **Drift-check mechanism for the capability/security layer.** Not
  designed yet — a periodic check that the skill-entry capability/
  redaction layer (Patch B1/B2) still matches reality. Worth deciding
  once that layer exists and has real content, not before.

- **Redaction coverage beyond `redaction_guard.py`'s seeded
  ALLOWED_ROOTS/SECRET_NAME_PATTERNS is a code change, not a config
  change.** (`backend/eo/redaction_guard.py`) The redaction *entries*
  in `eo/capability_entries.py` are human-editable without a deploy;
  what `redaction_guard.is_readable()` actually enforces is not, by
  design — that's the property Patch B2 depends on. Worth revisiting
  only alongside the drift-check mechanism already deferred above,
  once there's a real process for someone to notice "the enforcement
  list and the documentation list have drifted apart" rather than
  relying on a person happening to re-read both.

<!-- New entries go below this line, oldest first. -->

## Deferred during Patch B5a

- **Per-role capability entries.** `_is_known_capability()`
  (`eo/dispatcher.py`) and `capability_registration_gaps()`
  (`eo/router.py`) both check a hallucinated/referenced role name
  against `eo/capabilities.py::list_capabilities()`, but Patch B1's
  `CAPABILITY_SEED` is entry-id granularity (`agent_roster`,
  `mcp_capabilities`, ...), not per-role — so in practice these checks
  report "not a known capability" for essentially every real role name
  today. Worth revisiting once B3's role-scoping (`capability_tags` per
  role) gives the capability layer that finer granularity to check
  against.
- **`generic_worker.run()` itself receiving capability context.**
  B5a only threads `eo/capabilities.py` into the three orchestration
  modules' own observability/validation surface (`agent_start`'s
  payload, the hallucinated-role event, router's coverage check) — it
  does not change what any dispatched agent function (including
  `agents/generic_worker.py`) is actually given to reason with. That
  module's own direct `eo.skill_library` import (`get_relevant_skill`/
  `ensure_skill_for_task`) is untouched; those functions aren't wrapped
  by `eo/capabilities.py` at all yet. A real "agent checks the
  capability layer before falling back to a broader read" (architecture
  plan §3.2) needs generic_worker.py's own call shape to change — out
  of scope for a patch that's supposed to be behavior-safe for the
  three named orchestration files only.

## Deferred during Patch B6

- **No explicit "tab" concept anywhere else in the backend.** `tab` is
  a brand-new, bare `str | None` threaded from `TaskRequest`/
  `ConfirmTaskRequest` all the way to `eo/executor.py`'s `_run_loop()`
  (see `eo/executor.py::execute_graph()`'s own docstring) purely so
  this patch's budget check can gate itself to `tab == "chat"`. No
  other backend code validates, enumerates, or otherwise cares what
  values are legal — the frontend is trusted to send the right string.
  A real enum/constant (`CHAT`, `PROJECTS`, `NOTEBOOKS`, ...) shared
  between frontend and backend, and validation at the `TaskRequest`
  boundary, would close that gap; out of scope for a patch that's just
  supposed to add the budget mechanism.
- **`DEFAULT_TOOL_CALL_BUDGET`'s value (40) is a guess, not data.**
  `eo/tool_budget.py`'s own docstring already flags this — the plan
  (§3.4) explicitly calls it a starting default meant to be tuned once
  real chat-tab usage numbers exist. No telemetry pass to actually
  gather those numbers is in scope here.
- **`_run_tier2()`'s direct `execute_graph()` call never receives
  `tab`.** Only the tier-3 hires-driven path (`_run_tier3_hires()` ->
  `run_with_looping()`) is threaded through — tier 2's fixed/hires
  graphs are typically short, code-editing dispatches, not the
  open-ended adaptive runs the budget is meant to catch. Worth
  revisiting if a long-running tier-2 chat-tab task turns out to need
  the same backstop.

## Deferred during Patch B7

- **`eo/executor.py`'s `resume_graph()` doesn't clear the scratchpad on
  completion.** (`backend/eo/scratchpad.py`, `backend/eo/executor.py`)
  `resume_graph()` duplicates `run_with_looping()`'s own finished-return
  tail inline for the resumed-macro-loop case, rather than calling back
  into `eo/loop_controller.py` — so this patch's `clear_scratchpad()`
  hook, wired only into `run_with_looping()`'s real completion point,
  never fires for a run that pauses and is later resumed to completion.
  Fixing it properly means resume_graph()'s duplicated tail should stop
  being a separate copy of that logic, which is a bigger structural
  change than this patch's scope (add the scratchpad mechanism).

## Removed during Patch C0

- **The human-facing CLI (`cli/minime_cli/`).** Built before this
  direction was clear; nothing in Series C is a human-typed command,
  and Track 2's `run_data_command()` is in-process only, never HTTP.
  Deleted rather than deferred — see `MiniMe-Patch-Series-C-Plan.md`,
  Track 1, and `docs/decisions/0002-cli-auth-strategy.md` /
  `0003-cli-attach-daemon-dir.md` (marked superseded, not deleted, as
  the record of why A6/A7 were built the way they were).

## Reviewed during Patch B9

- **`resume_graph()`'s scratchpad gap (flagged during Patch B7, above)
  is still open.** Confirmed still present during B9's integration pass
  (`backend/eo/executor.py`'s `resume_graph()` still has two finished-
  return points — the plain-resume case and the macro-loop-continuation
  tail — neither calling `clear_scratchpad()`). Not fixed here: B9 is an
  integration pass over B0-B8's own scope, not a rewrite of
  `resume_graph()`'s duplicated tail. Worth its own patch once that
  structural change is prioritized.
