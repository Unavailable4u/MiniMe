"use client";
import { useState } from "react";
import Markdown from "./Markdown";
import { Check, Pencil, RotateCcw } from "lucide-react";

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
export default function AgentStepList({ steps, onResume }) {
  if (!steps || steps.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {steps.map((step) => (
        <StepRow key={step.id} step={step} onResume={onResume} />
      ))}
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
  if (!role) return "text-neutral-400";
  let hash = 0;
  for (let i = 0; i < role.length; i++) hash = (hash * 31 + role.charCodeAt(i)) >>> 0;
  return ROLE_COLORS[hash % ROLE_COLORS.length];
}

function StepRow({ step, onResume }) {
  // Part 2 §2.4/§2.7: a step paused for human approval auto-opens (the
  // whole point is to show the output for review, not make the user
  // discover it's hidden), and stays open while the approval card is
  // showing regardless of the collapsible toggle below.
  const isPaused = step.status === "awaiting_approval";
  const [open, setOpen] = useState(isPaused);
  const hasBody = Boolean(step.text || step.summary);
  const wasTruncated = !step.text && step.summary && TRUNCATED_SUFFIX.test(step.summary);
  const color = step.status === "error" ? "text-red-400" : roleColor(step.role);

  return (
    <div
      className={`rounded-lg border text-xs ${
        step.status === "error"
          ? "border-red-900 bg-red-950/30"
          : isPaused
          ? "border-amber-700 bg-amber-950/20"
          : step.status === "done"
          ? "border-neutral-800 bg-neutral-950/50"
          : "border-neutral-700 bg-neutral-900/50"
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
          {hasBody && <span className="text-neutral-600">{open ? "▾" : "▸"}</span>}
          {step.role}
        </span>
        <span className={step.status === "running" ? "animate-pulse text-neutral-500" : isPaused ? "text-amber-500" : "text-neutral-500"}>
          {isPaused ? "awaiting approval" : step.status}
          {step.durationMs != null ? ` · ${step.durationMs}ms` : ""}
        </span>
      </button>
      {open && hasBody && (
        <div className="border-t border-neutral-800 px-3 py-2">
          {step.status === "error" ? (
            <div className="text-red-400 whitespace-pre-wrap">{step.summary}</div>
          ) : (
            <>
              <div className="max-h-64 overflow-y-auto">
                <Markdown>{step.text || step.summary}</Markdown>
              </div>
              {wasTruncated && (
                <p className="mt-1 text-neutral-600 text-xs">
                  This output was too long to stream in full and was
                  truncated.
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
            value={text}
            onChange={(e) => setText(e.target.value)}
            rows={5}
            className="w-full resize-none bg-neutral-950 border border-neutral-800 rounded-md px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600 leading-relaxed"
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() => setEditing(false)}
              className="text-neutral-500 hover:text-neutral-300 px-2 py-1"
            >
              Cancel
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => act("edit", { text })}
              className="flex items-center gap-1.5 bg-neutral-100 text-neutral-900 rounded-lg px-3 py-1.5 font-medium"
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
            className="flex items-center gap-1.5 text-neutral-400 hover:text-neutral-200 px-2 py-1"
          >
            <RotateCcw size={12} />
            Reject & Redo
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setEditing(true)}
            className="flex items-center gap-1.5 text-neutral-400 hover:text-neutral-200 px-2 py-1"
          >
            <Pencil size={12} />
            Edit & Continue
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => act("approve")}
            className="flex items-center gap-1.5 bg-neutral-100 text-neutral-900 rounded-lg px-3 py-1.5 font-medium"
          >
            <Check size={12} />
            Approve
          </button>
        </div>
      )}
    </div>
  );
}