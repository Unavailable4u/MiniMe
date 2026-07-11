"use client";
import { useState } from "react";
import { categorize } from "./agentRoleIcons";
import { Check, X, RotateCcw } from "lucide-react";

// Part 2 §2.5 — Manual role editing before dispatch.
//
// Presentational only: takes the `hires` list straight from a
// POST /api/task/preview response (result.hires — {role, agent_key,
// brief} triples) and renders one editable card per hire. Wiring is
// deliberately left to the caller (SessionContext.jsx's sendTask()) —
// this component doesn't know about /api/task/preview or
// /api/task/confirm itself, it just turns a hires array into an edited
// hires array plus a confirm/cancel decision.
//
// Each hire card tracks its own `brief` edit and `update_library`
// choice locally; onConfirm() is called with the full hires array,
// each entry widened to {role, agent_key, brief, update_library} —
// exactly the shape ConfirmTaskRequest.hires (api/server.py) expects.
// A hire nobody touched is passed through unchanged with
// update_library: false ("just this once" is the default — most
// reviews are a quick skim, not an edit).
export default function HireReviewScreen({ hires, onConfirm, onCancel }) {
  const [edited, setEdited] = useState(() =>
    hires.map((h) => ({ ...h, update_library: false }))
  );

  function updateBrief(index, brief) {
    setEdited((prev) => prev.map((h, i) => (i === index ? { ...h, brief } : h)));
  }

  function setScope(index, updateLibrary) {
    setEdited((prev) => prev.map((h, i) => (i === index ? { ...h, update_library: updateLibrary } : h)));
  }

  function resetBrief(index) {
    setEdited((prev) =>
      prev.map((h, i) => (i === index ? { ...h, brief: hires[i].brief, update_library: false } : h))
    );
  }

  return (
    <div className="rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-950-a50)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <div>
          <h3 className="text-sm font-medium text-[var(--neutral-200)]">Review hires before dispatch</h3>
          <p className="text-xs text-[var(--neutral-500)] mt-0.5">
            {edited.length} role{edited.length === 1 ? "" : "s"} staffed for this task. Edit any
            brief below, or confirm to run as-is.
          </p>
        </div>
      </div>

      <div className="space-y-2">
        {edited.map((hire, i) => {
          const original = hires[i];
          const category = categorize(hire.role);
          const isDirty = hire.brief !== original.brief;
          return (
            <div key={`${hire.role}-${i}`} className="rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-900-a50)] p-3">
              <div className="flex items-center justify-between gap-2 mb-2">
                <span className="flex items-center gap-1.5 text-sm font-medium" style={{ color: category.color }}>
                  <span>{category.icon}</span>
                  {hire.role}
                </span>
                <span className="text-[11px] text-[var(--neutral-500)] font-mono">{hire.agent_key}</span>
              </div>

              <textarea
                value={hire.brief}
                onChange={(e) => updateBrief(i, e.target.value)}
                rows={3}
                className="w-full resize-none bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)] leading-relaxed"
              />

              <div className="flex items-center justify-between mt-2">
                <div className={`flex items-center gap-3 text-[11px] ${isDirty ? "text-[var(--neutral-300)]" : "text-[var(--neutral-600)]"}`}>
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="radio"
                      name={`scope-${i}`}
                      checked={!hire.update_library}
                      onChange={() => setScope(i, false)}
                      disabled={!isDirty}
                    />
                    Just this once
                  </label>
                  <label className="flex items-center gap-1.5 cursor-pointer">
                    <input
                      type="radio"
                      name={`scope-${i}`}
                      checked={hire.update_library}
                      onChange={() => setScope(i, true)}
                      disabled={!isDirty}
                    />
                    Update the library
                  </label>
                </div>
                {isDirty && (
                  <button
                    type="button"
                    onClick={() => resetBrief(i)}
                    className="flex items-center gap-1 text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                    title="Revert to the original brief"
                  >
                    <RotateCcw size={11} />
                    Revert
                  </button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      <div className="flex justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onCancel}
          className="flex items-center gap-1.5 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] px-3 py-1.5"
        >
          <X size={13} />
          Cancel
        </button>
        <button
          type="button"
          onClick={() => onConfirm(edited)}
          className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium"
        >
          <Check size={13} />
          Confirm & Run
        </button>
      </div>
    </div>
  );
}