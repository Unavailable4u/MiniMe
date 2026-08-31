# Self-Modification Experiment — Running Log

Format per guide §9: what it was asked to look at, what it reported, what
was decided, what actually happened — plus a confidence category on every
finding (hard fact / cross-referenced signal / single-source guess), so
this log can later answer "was the diagnosis actually trustworthy" by
category, not with one undifferentiated verdict.

---

## 2026-08-30 — Session 1: Environment setup + Phase 1 tool debugging

### Environment & infrastructure (context, not a self-mod finding)

- WSL2 environment, venv, VS Code Remote-WSL set up and verified working
  end-to-end (backend boots, `/docs` reachable from Windows browser).
- Data layer isolated: dedicated Upstash Redis (`minime-experiment`) and
  Vector (`minime-experiment-vector`) instances, isolation verified via a
  direct write/delete test against both dashboards.
- Iteration cap (`eo/iteration_cap.py`) built and verified: 8/session,
  resets at local midnight, state file lives outside the repo.
- Branching workflow established: `self-mod/experiment`, kept separate
  from `main` and from the Windows-folder clone's own independent work.
- Rollback drill completed successfully — both an uncommitted-edit
  recovery (`git checkout --`) and a committed-bad-edit recovery
  (`git reset --hard HEAD~1`) were deliberately triggered and recovered.

### Target audited: agents/code_writer_lean.py
(used mainly to shake out self_audit.py's own bugs, not yet a from-scratch
diagnosis run for its own sake)

**Attempt 1 — tool non-functional (path bug)**
- Asked: run self_audit against `agents/code_writer_lean.py`.
- Reported: REFUSED, "Not a file" — caused by a redundant `backend/`
  prefix in both the invocation and `AUDIT_ALLOWED_ROOTS`.
- Category: N/A — tool bug, not a target finding.
- Decided/outcome: fixed the double-prefix in `AUDIT_ALLOWED_ROOTS`,
  corrected invocation convention (always run from `backend/`, no
  `backend/` prefix in the path argument). Re-verified.

**Attempt 2 — first real report, three self-referential bugs found**
- `"utils.llm_client.generate_text ... no matching file found"` —
  **false positive**. Category: hard fact once traced (bug in the
  resolver, not a real finding about the target).
- `"No other file imports this module"` — plausible, later confirmed
  consistent with the file's own docstring once the import graph was
  fixed. Category: single-source, medium confidence.
- `test_summary.ran: false, "no matching test file found"` — **false
  negative**. A real test file exists at
  `tests/unit/test_agent_code_writer_lean.py`; the glob had a doubled
  `backend/` prefix. Category: hard fact.
- `churn.ran: false, "not a git repo"` — **false**. Bug: checked
  `REPO_ROOT / ".git"` instead of `REPO_ROOT.parent / ".git"`. Category:
  hard fact.
- Decided/outcome: fixed all three. Re-verified — `test_summary` now
  shows `ran: true` (20 passed), `churn` now shows real history
  (9 commits).

**Attempt 3 — import-resolution bug persisted after the above fixes**
- Reported: still flagged `utils.llm_client.generate_text` as
  unresolved, despite the file existing.
- Root cause (confirmed by direct code read): the resolver treated every
  dotted segment of `from module import symbol` as a path component
  (looked for `utils/llm_client/generate_text.py` instead of
  `utils/llm_client.py`). Separately, the "is this worth checking" gate
  only recognized `agents`/`utils`, silently skipping imports from `eo`,
  `memory`, `relay`, and others.
- Category: hard fact.
- Decided/outcome: added a parent-path fallback to the resolver; split
  the single allowlist into two — `AUDIT_ALLOWED_ROOTS` (gates audit
  targets) and a new `LOCAL_PACKAGE_NAMES` (gates which imports are
  checked for resolution). Re-verified — `unresolved_imports_in_target`
  correctly empty.

**Attempt 4 — hand-edit introduced a fresh bug, caught before running**
- While applying attempt 3's fix by hand, a stale duplicate line
  silently discarded the `SCAN_SKIP_DIRS` filter, and the new fallback
  logic ended up indented outside the `for` loop (would have only
  checked the last import in the file, once, after the loop finished).
- Category: hard fact, caught via review *before* execution — a good
  example of catching a mistake before it produced a misleading report.
- Decided/outcome: replaced the whole block cleanly. Verified via
  `py_compile` and a real run.

**Known limitation — logged, deliberately not fixed this session**
- `symbols[].reference_count_elsewhere` is name-only, not
  fully-qualified. Confirmed by hand: `run` in `code_writer_lean.py`
  showed 234 "references," most traced to unrelated `.run(...)` calls on
  unrelated objects in unrelated scripts. A real instance of the
  name-collision limitation the original guide's §6a anticipated.
- Category: single-source guess, low confidence.
- Decision: do not trust `reference_count_elsewhere` on common names
  (`run`, `get`, `process`, etc.) until confidence tiers are added to
  the `symbols` section — currently only `findings` carries a confidence
  field. Deliberately deferred, not fixed.

### Decisions made, not yet executed

- **Multi-LLM review layer** — agreed scope expansion: 2–3 independent
  reviewer calls across genuinely different providers (not the same
  model 3x), modeled on `agents/reviewer.py`'s parallel-worker pattern,
  to get real intelligence-based review beyond static/mocked-test
  analysis. Outputs to be kept separate per reviewer, not merged/voted.
  Not yet built.
- **Real execution-based profiling** — identified as the fix for Phase
  1's biggest blind spot (mocked LLM/network calls make real latency
  invisible to the current profiler), modeled on
  `agents/performance_reviewer.py`'s LLM-writes-a-harness-then-executes
  pattern. Explicitly flagged as a genuine execution-risk boundary
  crossing requiring a conscious, separate opt-in — not started.

### Open items carried forward

- Scan cap: appears implemented (`scan_truncated`/`files_scanned` fields
  present in reports) but the exact `MAX_SCAN_FILES` value has not been
  confirmed/logged yet.
- Phase 1's first genuine from-scratch diagnosis (as opposed to
  tool-debugging) hasn't happened yet — worth a clean re-run once the
  multi-LLM review layer exists.

---

## 2026-09-01 — Session 2: Multi-LLM review layer — token cap bug found and fixed

### Target audited: the multi-LLM review layer itself
(`backend/eo/self_audit.py`, `backend/utils/llm_client.py`,
`backend/utils/llm_errors.py` — tool-debugging, not a target-file finding)

**Attempt 1 — groq crashed outright ("can never fit" tpm-ceiling failure)**
- Reported: groq's whole-file review branch had no output-token cap at
  all, so `_max_tokens_for()`'s flat model-family default plus the
  file's real input size exceeded groq's entire 8000-tpm per-minute
  budget on its own — unwinnable regardless of retries.
- Category: hard fact.
- Decided/outcome: capped output tokens at a flat
  `_REVIEW_CHUNK_OUTPUT_TOKENS = 1500` on both the whole-file and
  chunked branches. Fixed groq's crash. Committed as `9b896ff`.

**Attempt 2 — the fix for Attempt 1 introduced a new bug (flat cap starved mistral)**
- Reported: after Attempt 1's fix, a real run against
  `agents/code_writer_lean.py` showed groq *and* mistral both hitting
  `finish_reason=length` mid-review. Root cause: the flat `1500` cap
  was sized for groq's worst case (8000 tpm) but applied identically to
  mistral, which has 25000 tpm — 3x the real headroom — so mistral's
  reviews were truncated despite having plenty of room to spare.
- Category: hard fact, confirmed by comparing each provider's real tpm
  figure in `QUOTA_CONFIG` against the flat constant.
- Decided/outcome: replaced the flat cap with a per-call, per-provider
  adaptive budget — `_review_output_budget()` — sized from that call's
  actual estimated input and that provider's real tpm, floored at 1500
  and ceilinged at 4096. First patch for this arrived mis-targeted (see
  below), regenerated correctly, applied, and re-verified against a
  real run: groq and mistral both returned full, untruncated,
  well-formed reviews with no crash and no truncation.

**Process note — a patch was generated against the wrong base and had to be regenerated**
- The first attempt at Attempt 2's patch failed to `git apply` with
  three "patch does not apply" errors (`self_audit.py:1236`,
  `llm_client.py:135`, `llm_errors.py:35`). Diagnosis: not a corrupted
  patch file — it was built assuming a stale base. Two of the three
  files (`llm_client.py`, `llm_errors.py`) already had their target
  state committed at HEAD (an unrelated zero-quota fix from the same
  `9b896ff` commit), so the patch's search context for those no longer
  existed. The third (`self_audit.py`) was the opposite problem — the
  patch assumed the *original, pre-Attempt-1* code, but HEAD already
  had Attempt 1's flat cap applied to both branches, a state the patch
  never accounted for.
- Category: hard fact, confirmed by cloning the actual `self-mod/
  experiment` branch and reproducing the failure directly rather than
  guessing from the error text alone.
- Decided/outcome: regenerated the patch against the real current HEAD
  (touching only `self_audit.py`, since the other two needed no further
  change), verified `git apply --check` passes clean on a fresh clone,
  and confirmed the patched file compiles before handing it over.
  Applied and confirmed working via a real `--ai-review` run. **Not yet
  committed** — do that next, e.g. `fix: replace flat output-token cap
  with per-call adaptive budget in multi-LLM review layer`.

**Known limitation — logged, not fixed this session: gemini truncation**
- The same real run also showed gemini truncating
  (`finish_reason=length` on its last chain step, only 620 chars of
  partial output returned). Confirmed this is unrelated to the bug just
  fixed: gemini is gated by RPM/RPD in `QUOTA_CONFIG`, not TPM, so
  `rate_ledger._tpm_limit_for("gemini", ...)` returns `None` and
  `_review_output_budget()` correctly no-ops for it (same "don't
  fabricate a number" posture the rest of this code already follows) —
  meaning gemini's code path is untouched by tonight's fix, for better
  or worse. Why it truncates at only 620 chars when its flat default
  (`DEFAULT_MAX_TOKENS = 8192`) should be generous remains
  unexplained — something else (a provider-side ceiling on
  `gemini-3.6-flash`, or the continuation/retry logic exhausting its
  budget) is the likely suspect, not yet traced.
- Category: single-source guess (the "why" is still open) sitting on
  top of a hard fact (that it happens, reproducibly).
- Decision: not yet made whether to fix now or carry forward like
  `reference_count_elsewhere` — open item below.

### Open items carried forward

- **Gemini truncation** (see above) — decide: investigate the real
  cause now, or log as a known limitation and move on.
- Phase 1's first genuine from-scratch diagnosis (as opposed to
  tool-debugging) still hasn't happened — groq and mistral are now
  trustworthy enough post-fix that this is unblocked.
- Tonight's `self_audit.py` fix (Attempt 2 above) needs an actual
  commit on `self-mod/experiment` — currently applied but uncommitted.

### Open items resolved this session

- **Scan cap** — confirmed: `MAX_SCAN_FILES = 4000` in
  `backend/eo/self_audit.py`. Carried as unconfirmed since Session 1;
  closing it out.
