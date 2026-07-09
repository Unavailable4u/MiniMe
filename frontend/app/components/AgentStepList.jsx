"use client";
import { useState } from "react";
import Markdown from "./Markdown";

// Each `step` is one agent_start/agent_done pair pushed by
// SessionContext.jsx's Pusher handler, in arrival order. Safe to render
// in array order and treat the last entry as "currently running" —
// see SessionContext.jsx's comment on why eo/executor.py's strictly
// sequential execution loop makes that safe.
export default function AgentStepList({ steps }) {
  if (!steps || steps.length === 0) return null;
  return (
    <div className="space-y-1.5">
      {steps.map((step) => (
        <StepRow key={step.id} step={step} />
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

function StepRow({ step }) {
  const [open, setOpen] = useState(false);
  const hasBody = Boolean(step.text || step.summary);
  const wasTruncated = !step.text && step.summary && TRUNCATED_SUFFIX.test(step.summary);
  const color = step.status === "error" ? "text-red-400" : roleColor(step.role);

  return (
    <div
      className={`rounded-lg border text-xs ${
        step.status === "error"
          ? "border-red-900 bg-red-950/30"
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
        <span className={step.status === "running" ? "animate-pulse text-neutral-500" : "text-neutral-500"}>
          {step.status}
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
        </div>
      )}
    </div>
  );
}