"use client";
import { useState } from "react";
import { Bookmark, Check, X } from "lucide-react";
import { authHeaders } from "../context/SessionContext";

// Part 2 §2.3 — the "save from a finished run" write path. A run's own
// `execution_order` (already returned by the Panel/Inspector's
// classification, and identical to a workflow template's `roles` shape)
// is sitting right there on the message — this just names it and POSTs
// to /api/workflow-templates, reusing eo/structure.py's
// save_workflow_template() with zero reshaping, exactly as designed.
//
// Presentational + its own fetch, same pattern RoleLibraryTab.jsx and
// WorkflowTemplatesTab.jsx already use, rather than routing through
// SessionContext — this is a one-off action on a single finished
// message, not shared session state.
export default function SaveRunAsTemplate({ apiUrl, roles, domainHint }) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [err, setErr] = useState(null);

  if (!roles || roles.length === 0) return null;

  async function save() {
    if (!name.trim()) {
      setErr("Name is required.");
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      const res = await fetch(`${apiUrl}/api/workflow-templates`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({
          name: name.trim(),
          description: description.trim(),
          roles,
          domain_hint: domainHint || null,
        }),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setSaved(true);
      setOpen(false);
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  if (saved) {
    return (
      <span className="flex items-center gap-1 text-[11px] text-emerald-400">
        <Check size={11} />
        Saved as template
      </span>
    );
  }

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="flex items-center gap-1 text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
        title="Save this run's role pipeline as a reusable workflow template"
      >
        <Bookmark size={11} />
        Save as template
      </button>
    );
  }

  return (
    <div className="rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-950-a50)] p-2.5 space-y-1.5 text-xs">
      <div className="flex items-center justify-between">
        <span className="text-[var(--neutral-400)]">
          Save this run's {roles.length}-role pipeline as a template
        </span>
        <button type="button" onClick={() => setOpen(false)} className="text-[var(--neutral-600)] hover:text-[var(--neutral-300)]">
          <X size={12} />
        </button>
      </div>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Template name"
        className="w-full bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2 py-1 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)]"
      />
      <input
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="Description (optional)"
        className="w-full bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2 py-1 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)]"
      />
      {err && <p className="text-red-400">{err}</p>}
      <div className="flex justify-end gap-2">
        <button
          type="button"
          disabled={saving}
          onClick={save}
          className="flex items-center gap-1 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-2.5 py-1 font-medium disabled:opacity-50"
        >
          <Check size={11} />
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </div>
  );
}