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

function StepRow({ step }) {
  const [open, setOpen] = useState(false);
  const hasBody = Boolean(step.text || step.summary);

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
        <span className="text-neutral-400 flex items-center gap-1.5">
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
              {!step.text && step.summary && (
                <p className="mt-1 text-neutral-600 text-xs">
                  Only a short summary is available for this step — the
                  full output isn't streamed to the frontend for every
                  agent yet (see Part 18 guide §1).
                </p>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
