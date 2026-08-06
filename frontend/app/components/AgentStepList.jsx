"use client";
import { useState } from "react";
import Markdown from "./Markdown";
import { Check, Pencil, RotateCcw } from "lucide-react";
import { categorize } from "./agentRoleIcons";

// Each `step` is one agent_start/agent_done pair pushed by
// SessionContext.jsx's Pusher handler, in arrival order. Safe to render
// in array order and treat the last entry as "currently running" —
// see SessionContext.jsx's comment on why eo/executor.py's strictly
// sequential execution loop makes that safe.
//
// `onResume` — Part 2 §2.7: optional. Only ever passed for the LIVE
// steps list (WorkingPanel.jsx's `loading` section), never for a
// finished message's own snapshot — a past run can't be resumed.
// Called as onResume({action: "approve"|"edit"|"reject_redo", text?})
// and wired straight through to SessionContext.jsx's resumeRun(), which
// POSTs to /api/resume (§2.4). No structural change to this component
// otherwise — it already renders arbitrary step objects generically;
// a step whose status is "awaiting_approval" just renders these extra
// actions in place of (or alongside) its existing collapsible body.
// `manualPause` — NEW, CO3 patch 4. True when dock.state.pausedRun is
// set but no step here carries status:"awaiting_approval" — i.e. the
// run was paused by the on-demand pause_requested flag (WorkingPanel's
// PauseButton), not by hitting an approval_roles checkpoint. Confirmed
// by reading WorkspaceDockContext.jsx's handleDockEvent: the
// "awaiting_approval" SSE event only ever fires for the approval_roles
// path, so a manual pause previously left StepRow's own `isPaused`
// check permanently false and rendered no resume affordance at all in
// this panel — the chat bubble's Resume button (CO3 patch 3) was the
// only way to unblock it. Rendered once, after every step, since a
// manual pause isn't tied to any single role's output the way an
// approval-role pause is.
export default function AgentStepList({ steps, onResume, manualPause = false }) {
  if (!steps || steps.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {steps.map((step) => (
        <StepRow key={step.id} step={step} onResume={onResume} />
      ))}
      {manualPause && onResume && <ManualPauseActions onResume={onResume} />}
    </div>
  );
}

// Matches the exact suffix eo/executor.py's _summarize() appends when it
// still has to cut a result short (Migration Part 26 fix — the limit was
// raised 300 -> 9000 chars, so most results now arrive here whole; this
// only fires for the genuinely oversized minority, e.g. a full
// multi-module code submission).
const TRUNCATED_SUFFIX = /\.\.\. \[truncated, \d+ chars total\]$/;

// Small fixed palette, assigned deterministically per role name (hash ->
// index) so the same role always gets the same color across a session,
// without needing to hand-maintain a mapping for every possible role the
// Panel might hire (roles are dynamic — see eo/panel.py's staff_task()).
const ROLE_COLORS = [
  "text-sky-400", "text-violet-400", "text-emerald-400", "text-amber-400",
  "text-rose-400", "text-cyan-400", "text-fuchsia-400", "text-lime-400",
];
function roleColor(role) {
  if (!role) return "text-[var(--neutral-400)]";
  let hash = 0;
  for (let i = 0; i < role.length; i++) hash = (hash * 31 + role.charCodeAt(i)) >>> 0;
  return ROLE_COLORS[hash % ROLE_COLORS.length];
}

// NEW — Phase 8 step 8.2. Mirrors eo/structure.py's PATH_TO_TIER exactly
// (kept as a literal copy, not derived from anything at build time --
// this file has no route into the Python source, same tradeoff every
// other frontend constant that shadows a backend enum already makes,
// e.g. notebookCapabilities.js's `key` strings matching
// CAPABILITIES_MANIFEST by hand). `step.path` is constant for every
// step in one run (see SessionContext.jsx's step-8.2 comment on
// `agent_start` for why it's stored per-step anyway), so this is really
// a run-level badge repeated per row -- intentional, per the plan's own
// "tier per step" framing: the reader shouldn't have to scroll up to
// the routing card to know what tier the step they're looking at ran
// under, even though every step in view shares the same answer.
const TIER_LABELS = {
  instant: "Tier 0 · instant",
  direct: "Tier 1 · direct",
  fixed: "Tier 2 · fixed",
  adaptive: "Tier 3 · adaptive",
};

// NEW — Phase 8 step 8.4. The exact three reason codes
// eo/dispatcher.py's next_step() ever returns (confirmed by reading
// that function directly, not guessed) -- "plan" (the ordinary next
// role in the staffed sequence), "recheck" (routed back to an earlier
// role that already ran), "escalate" (a role asked for a NEW role not
// in the original plan, spliced in on the fly). A fourth internal value,
// "requested" (agents/*'s MissingDependencyError self-heal path), never
// reaches dispatch_event -- it's its own event type
// (agent_requested_role), not something this map needs to cover.
const REASON_LABELS = {
  plan: "next step",
  recheck: "sent back to recheck",
  escalate: "escalated to",
};

function StepRow({ step, onResume }) {
  // Part 2 §2.4/§2.7: a step paused for human approval auto-opens (the
  // whole point is to show the output for review, not make the user
  // discover it's hidden), and stays open while the approval card is
  // showing regardless of the collapsible toggle below.
  const isPaused = step.status === "awaiting_approval";
  const [open, setOpen] = useState(isPaused);
  const hasGiven = Boolean(step.givenRoles?.length);
  const hasCalledOutTo = Boolean(step.calledOutTo?.destination);
  const hasBody = Boolean(step.text || step.summary || step.image || hasGiven || hasCalledOutTo);
  const wasTruncated = !step.text && step.summary && TRUNCATED_SUFFIX.test(step.summary);
  const color = step.status === "error" ? "text-red-400" : roleColor(step.role);
  const category = categorize(step.role);

  return (
    <div
      className={`rounded-lg border text-xs ${
        step.status === "error"
          ? "border-red-900 bg-red-950/30"
          : isPaused
          ? "border-amber-700 bg-amber-950/20"
          : step.status === "done"
          ? "border-[var(--neutral-800)] bg-[var(--neutral-950-a50)]"
          : "border-[var(--neutral-700)] bg-[var(--neutral-900-a50)]"
      }`}
    >
      <button
        type="button"
        onClick={() => hasBody && setOpen((o) => !o)}
        className={`w-full flex items-center justify-between px-3 py-2 text-left ${
          hasBody ? "cursor-pointer" : "cursor-default"
        }`}
      >
        <span className={`flex items-center gap-1.5 font-medium ${color}`}>
          {hasBody && <span className="text-[var(--neutral-600)]">{open ? "▾" : "▸"}</span>}
          <span style={{ color: category.color }} aria-hidden="true">{category.icon}</span>
          {step.role}
          {/* NEW — Phase 8 step 8.2: undefined for any step captured
              before this patch (persisted snapshots have no `path` on
              them yet) -- TIER_LABELS[undefined] is undefined, so this
              silently renders nothing for old data instead of "Tier
              undefined". */}
          {TIER_LABELS[step.path] && (
            <span className="font-normal text-[10px] text-[var(--neutral-600)]">
              {TIER_LABELS[step.path]}
            </span>
          )}
        </span>
        <span className={step.status === "running" ? "animate-pulse text-[var(--neutral-500)]" : isPaused ? "text-amber-500" : "text-[var(--neutral-500)]"}>
          {isPaused ? "awaiting approval" : step.status}
          {step.durationMs != null ? ` · ${step.durationMs}ms` : ""}
        </span>
      </button>
      {open && hasBody && (
        <div className="border-t border-[var(--neutral-800)] px-3 py-2">
          {/* NEW — Phase 8 step 8.3: "what it was given" — the roles
              already staffed/finished before this one started, i.e.
              what this step had on the memory bus to draw on. Rendered
              ahead of the step's own output, not mixed into it, since
              it describes the INPUT side of the step rather than being
              part of the result. Only ever empty for the very first
              step of a run (nothing ran before it yet) or an
              instant/direct-path entrypoint that never had a
              role_names[:idx] to report — hasGiven already gates this
              out for those, no empty "Given:" line renders. */}
          {hasGiven && (
            <p className="text-[var(--neutral-500)] mb-2">
              Given: {step.givenRoles.join(", ")}
            </p>
          )}
          {step.status === "error" ? (
            <div className="text-red-400 whitespace-pre-wrap">{step.summary}</div>
          ) : (
            <>
              {step.image && (
                // e.g. agents/citation_graph_builder.py's SVG data URI —
                // a data: URI, not a fetched URL, so no next/image;
                // sized/framed by Markdown.jsx's own img: override for
                // visual consistency, reused here directly.
                <img
                  src={step.image}
                  alt={`${step.role} visualization`}
                  loading="lazy"
                  className="max-w-full h-auto rounded-lg border border-[var(--neutral-800)] mb-2"
                />
              )}
              <div className="max-h-64 overflow-y-auto">
                {(step.text || step.summary) && <Markdown>{step.text || step.summary}</Markdown>}
              </div>
              {wasTruncated && (
                <p className="mt-1 text-[var(--neutral-600)] text-xs">
                  This output was too long to stream in full and was
                  truncated.
                </p>
              )}
              {/* NEW — Phase 8 step 8.4: "called out to" — what this
                  step's completion chained to next, and why. Placed
                  after the step's own output rather than up with
                  "Given:" above, since this describes what happens
                  AFTER the step, not what it started with. Absent for
                  a run's last step (eo/dispatcher.py's _log_route()
                  never fires when destination is None) and for any
                  error step (the executor loop re-raises instead of
                  calling next_step() on an exception) -- both expected,
                  not missing data. */}
              {hasCalledOutTo && (
                <p className="mt-1 text-[var(--neutral-500)]">
                  → {REASON_LABELS[step.calledOutTo.reason] || "routed to"} <span className="font-medium">{step.calledOutTo.destination}</span>
                </p>
              )}
            </>
          )}
          {isPaused && onResume && (
            <ApprovalActions step={step} onResume={onResume} />
          )}
        </div>
      )}
    </div>
  );
}

// Part 2 §2.4/§2.7 — the three decisions resume_graph() understands.
// "Edit & Continue" opens a textarea seeded with this role's own output
// (the same text/summary already rendered above) so the human edits the
// ACTUAL text that gets written back to stage_output:{session_id}:{role}
// — not a blank box. "Reject & Redo" needs no extra input; the backend
// resets idx back to this role's position and re-enters the loop.
function ApprovalActions({ step, onResume }) {
  const [editing, setEditing] = useState(false);
  const [text, setText] = useState(step.text || step.summary || "");
  const [busy, setBusy] = useState(false);

  async function act(action, payload) {
    setBusy(true);
    try {
      await onResume({ action, ...(payload || {}) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-2 border-t border-amber-900/60 pt-2 space-y-2">
      <p className="text-amber-500/90">
        This role requires approval before the run continues.
      </p>
      {editing ? (
        <>
          <textarea
            id={`approval-edit-${step.id}`}
            name={`approval-edit-${step.id}`}
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={5}
            className="w-full resize-none bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)] leading-relaxed"
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => setEditing(false)}
              className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] px-2 py-1"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => act("edit", { text })}
              className="flex items-center gap-1.5 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium"
            >
              <Check size={12} />
              Save & Continue
            </button>
          </div>
        </>
      ) : (
        <div className="flex justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => act("reject_redo")}
            className="flex items-center gap-1.5 text-[var(--neutral-400)] hover:text-[var(--neutral-200)] px-2 py-1"
          >
            <RotateCcw size={12} />
            Reject & Redo
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setEditing(true)}
            className="flex items-center gap-1.5 text-[var(--neutral-400)] hover:text-[var(--neutral-200)] px-2 py-1"
          >
            <Pencil size={12} />
            Edit & Continue
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => act("approve")}
            className="flex items-center gap-1.5 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium"
          >
            <Check size={12} />
            Approve
          </button>
        </div>
      )}
    </div>
  );
}

// NEW — CO3 patch 4. Sibling to ApprovalActions above, for the manual
// on-demand pause case (see AgentStepList's own comment on
// `manualPause`). Deliberately its own component rather than reusing
// ApprovalActions with `step` made optional: there's no role output to
// seed the textarea with here (starts blank — this is a redirect for
// what happens NEXT, not an edit of something a role already
// produced), and "Reject & Redo" has no sensible target — resume_graph()
// resets idx back to a specific role's position, and a manual pause
// isn't attached to one. Only "approve" (resume as-is) and "edit"
// (resume with new steering text) apply.
function ManualPauseActions({ onResume }) {
  const [redirecting, setRedirecting] = useState(false);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  async function act(action, payload) {
    setBusy(true);
    try {
      await onResume({ action, ...(payload || {}) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="rounded-lg border border-amber-700 bg-amber-950/20 text-xs px-3 py-2 space-y-2">
      <p className="text-amber-500/90">
        Run paused. Resume as-is, or redirect it with new instructions first.
      </p>
      {redirecting ? (
        <>
          <textarea
            id="manual-pause-redirect"
            name="manual-pause-redirect"
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={4}
            placeholder="New instructions to steer the run before it continues…"
            className="w-full resize-none bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)] leading-relaxed"
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => setRedirecting(false)}
              className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)] px-2 py-1"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={busy || !text.trim()}
              onClick={() => act("edit", { text })}
              className="flex items-center gap-1.5 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-60"
            >
              <Check size={12} />
              Redirect & Continue
            </button>
          </div>
        </>
      ) : (
        <div className="flex justify-end gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => setRedirecting(true)}
            className="flex items-center gap-1.5 text-[var(--neutral-400)] hover:text-[var(--neutral-200)] px-2 py-1"
          >
            <Pencil size={12} />
            Redirect
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => act("approve")}
            className="flex items-center gap-1.5 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium"
          >
            <Check size={12} />
            Resume
          </button>
        </div>
      )}
    </div>
  );
}