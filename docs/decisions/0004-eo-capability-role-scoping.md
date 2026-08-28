# 0004 — Capability layer, role-scoping, and what Patch Series B closed out

**Status:** accepted
**Context:** `MiniMe-CLI-Internal-Architecture-Plan.md` §7/§8; Patch Series
B (`MiniMe-Patch-Series-B-Plan.md`), B0-B9.

## The question

Patch Series B touched a lot of ground — a new capability module, a
two-form redaction layer, role-scoped introspection, a tool-call
budget, and an ephemeral scratchpad — spread across nine patches
(B0-B8) landed independently, on the understanding that a few things
would only get decided once, not re-derived by whichever session picked
up the next patch. This doc is that one place, closing out the four
items the architecture plan's §7 already called "decided" and recording
what B9's integration pass found once all of B0-B8 were merged.

## Decisions

**Introspection scope: role-scoped via skill-entry tags, not fixed
directory paths.** `eo/registry.py` (B3) attaches `capability_tags` per
role; `eo/capabilities.py::capabilities_for_role()` is the single lookup
every role's read scope goes through. Rejected alternative: hard-coded
per-role path allowlists scattered through `eo/registry.py` — same
"second, parallel system" failure mode `0001-cli-skills-mcp-scope.md`
already warned against, just one layer down.

**Tool-call budget: chat tab only, standalone and reusable.**
`eo/tool_budget.py` (B6) is not chat-tab-specific in its own code — the
scoping happens where `eo/executor.py`/`eo/loop_controller.py` call it,
gated on a `tab` flag threaded from `TaskRequest`. `DEFAULT_TOOL_CALL_BUDGET`
is a named constant, explicitly a starting guess (see `DEFERRED.md`),
not derived from usage data — that's follow-up work, not this phase's.

**Attribution/logging: audit log and scratchpad stay separate stores,
never merged.** `eo/audit_log.py` keeps its existing job — a durable,
append-only record, extended by B2 to also log redaction-entry writes.
`eo/scratchpad.py` (B7) is the opposite on every axis that matters:
mutable, agent-deletable, wiped by end-of-task. Keeping them apart means
the audit trail never gets noisy with throwaway notes, and the
scratchpad never accidentally becomes permanent.

**Scope boundary for this phase: no formal boundary, just inline flags
plus `DEFERRED.md`.** Every patch that ran into a "not now" idea flagged
it inline and added a line to `DEFERRED.md` in the same commit, per
B0's seeding of that file. That log is the boundary — reviewed as part
of this patch (see below), not redrawn.

## What B9's integration pass found

Full backend suite (`pytest tests/unit tests/integration`, matching
this repo's own CI invocation) was run against the merged B0-B8 state.

- **One real cross-patch gap, fixed:** `eo/mcp_registry.py`'s
  `mcp_server_status()` is async (a live MCP status check), so
  `eo/capabilities.py`'s B0 wrapper is async too, and
  `tests/unit/test_eo_capabilities.py` already marks its two
  `mcp_server_status` tests `@pytest.mark.asyncio` — correctly. No
  patch in B0-B8 added the pytest plugin that mark depends on, so both
  tests failed to even collect under this repo's actual CI command.
  Fixed by adding `pytest-asyncio` to `backend/requirements.txt` — a
  dependency-manifest gap, not an application-code bug; no source
  changes needed since the tests were already written correctly against
  the async shape.
- **Two failures ruled environment-only, not fixed:**
  `tests/integration/test_pii_scrub_ingestors.py`'s two PDF/voice
  redaction tests fail in this sandbox because `presidio-analyzer`
  tries to download the `en_core_web_lg` spaCy model over the network at
  test time and this environment's egress allowlist doesn't include
  the model host. `eo/pii_scrub.py` predates and is outside Patch
  Series B's scope entirely (it's not touched by any of B0-B8); this is
  a pre-existing "model must be pre-provisioned" environment
  requirement, not a regression this series introduced.
- **`resume_graph()`'s scratchpad gap, confirmed still open, not
  fixed.** Flagged during B7 and re-confirmed here: a task that pauses
  and is later resumed to completion through `eo/executor.py`'s
  `resume_graph()` never hits `eo/loop_controller.py`'s
  `clear_scratchpad()` call, because `resume_graph()`'s finished-return
  paths duplicate `run_with_looping()`'s tail inline rather than calling
  back into it. Left as-is per B7's own scope call — fixing it means
  restructuring that duplicated tail, which is bigger than either B7's
  or B9's stated scope. See `DEFERRED.md` for both the original B7 entry
  and this pass's confirmation.
- Acceptance greps from B2, B5a, and B8 (`redaction_guard.py` has no
  import-time/runtime dependency on `skill_library`; no direct
  `eo.skill_library`/`eo.mcp_registry` imports remain in
  `dispatcher.py`/`executor.py`/`router.py`) were re-checked and still
  hold against the fully-merged tree.

## Still genuinely open (unchanged from the architecture plan's §7)

- **Drift-checking for the capability/security layer** — mechanism not
  designed; per the plan, worth deciding now that the layer has real
  content, but that design work is separate from this doc.
- **The write-access design from §4** (atomic writes, verify-then-
  promote) — named as a requirement, not designed.
- **The daemon** — unchanged, its own future pass.
- **`resume_graph()`'s scratchpad-on-completion gap**, per above —
  newly explicit as a standing item rather than only a B7-commit
  comment, but not newly decided; no design work done here.

## Why not the alternatives

- **Skip the decision doc and let `DEFERRED.md` alone stand as the
  record.** Rejected — `DEFERRED.md` is a running log of "not now"
  ideas flagged inline as they came up; it was never meant to also
  carry the handful of things that *were* decided (tags vs. paths,
  chat-tab-only budget, separate stores, no formal boundary). Mixing
  "decided" and "deferred" into one file makes both harder to find.
- **Fix the `resume_graph()` scratchpad gap as part of B9 since it was
  already found and understood.** Tempting, but B9's own acceptance
  criteria are "full test suite green" and "decision doc current" — an
  integration pass over B0-B8, not a new structural patch. Restructuring
  `resume_graph()`'s duplicated tail deserves its own patch, sized and
  reviewed on its own, not folded silently into a patch whose job is to
  close the series out.
