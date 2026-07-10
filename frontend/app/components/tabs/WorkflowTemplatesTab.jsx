"use client";
import { useEffect, useState } from "react";
import { useSession } from "../../context/SessionContext";
import { categorize, DEFAULT_CATEGORY } from "../agentRoleIcons";
import { Trash2, Plus, Play, X } from "lucide-react";

// Part 2 §2.7 — Workflow Template builder. Covers both write paths
// §2.3's design calls for against a single GET/POST/DELETE
// /api/workflow-templates surface:
//   1. Build from scratch — the form below, a plain comma-separated
//      role list (the identical flat-list-of-strings shape
//      STRUCTURE_TEMPLATES entries already use).
//   2. Save from a finished run — not wired up here (there's no
//      "save this run" affordance on a message yet); the backend
//      (save_workflow_template()) already accepts a raw execution_order
//      list either way, so that's a small follow-up on ChatTab/
//      WorkingPanel, not a gap in this endpoint.
//
// "Start a new task from a saved template" (Definition of Done #2) is
// the small inline runner on each template card below — a task-text
// box + Run button hitting POST /api/task/from-template directly. This
// is deliberately NOT wired into ChatSidebar's "new chat" flow yet —
// per §2.7, that's an optional convenience to layer on once the Role
// Library and Workflow Template panels alone make the underlying data
// reachable.
export default function WorkflowTemplatesTab() {
  const { API_URL } = useSession();
  const API_KEY = process.env.NEXT_PUBLIC_API_KEY || null;

  const [templates, setTemplates] = useState(null);
  const [error, setError] = useState(null);
  const [showBuilder, setShowBuilder] = useState(false);

  async function load() {
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/workflow-templates`, {
        headers: API_KEY ? { "x-api-key": API_KEY } : {},
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setTemplates(await res.json());
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [API_URL]);

  async function deleteTemplate(templateId) {
    const res = await fetch(`${API_URL}/api/workflow-templates/${templateId}`, {
      method: "DELETE",
      headers: API_KEY ? { "x-api-key": API_KEY } : {},
    });
    if (res.ok) setTemplates((prev) => (prev || []).filter((t) => t.template_id !== templateId));
  }

  async function saveTemplate(payload) {
    const res = await fetch(`${API_URL}/api/workflow-templates`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(API_KEY ? { "x-api-key": API_KEY } : {}),
      },
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const saved = await res.json();
    setTemplates((prev) => [saved, ...(prev || [])]);
    setShowBuilder(false);
  }

  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-3xl mx-auto space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium text-neutral-200">Workflow Templates</h2>
          <p className="text-xs text-neutral-500 mt-1">
            Saved role pipelines you can start a task from directly,
            skipping automatic classification.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setShowBuilder((s) => !s)}
          className="flex items-center gap-1.5 text-xs bg-neutral-100 text-neutral-900 rounded-lg px-3 py-1.5 font-medium shrink-0"
        >
          <Plus size={13} />
          New template
        </button>
      </div>

      {showBuilder && (
        <TemplateBuilder onSave={saveTemplate} onCancel={() => setShowBuilder(false)} />
      )}

      {error && (
        <p className="text-xs text-red-400">
          Couldn't load templates: {error}. Check that{" "}
          <code className="font-mono">GET /api/workflow-templates</code> is reachable.
        </p>
      )}
      {!error && templates === null && <p className="text-xs text-neutral-500">Loading…</p>}
      {!error && templates !== null && templates.length === 0 && !showBuilder && (
        <p className="text-xs text-neutral-500">
          No saved templates yet. Build one from the Role Library, or
          click "New template" above.
        </p>
      )}

      <div className="space-y-2">
        {(templates || []).map((t) => (
          <TemplateCard key={t.template_id} template={t} apiUrl={API_URL} apiKey={API_KEY} onDelete={deleteTemplate} />
        ))}
      </div>
    </div>
  );
}

function TemplateBuilder({ onSave, onCancel }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [rolesText, setRolesText] = useState("");
  const [approvalText, setApprovalText] = useState("");
  const [domainHint, setDomainHint] = useState("");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  async function submit() {
    const roles = rolesText.split(",").map((r) => r.trim()).filter(Boolean);
    if (!name.trim() || roles.length === 0) {
      setErr("A name and at least one role are required.");
      return;
    }
    const approval_roles = approvalText.split(",").map((r) => r.trim()).filter(Boolean);
    setSaving(true);
    setErr(null);
    try {
      await onSave({
        name: name.trim(),
        description: description.trim(),
        roles,
        approval_roles,
        domain_hint: domainHint.trim() || null,
      });
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-950/50 p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-neutral-200">Build a template</h3>
        <button type="button" onClick={onCancel} className="text-neutral-500 hover:text-neutral-300">
          <X size={14} />
        </button>
      </div>

      <label className="block text-xs text-neutral-500">
        Name
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Blog post pipeline"
          className="mt-1 w-full bg-neutral-950 border border-neutral-800 rounded-md px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600"
        />
      </label>

      <label className="block text-xs text-neutral-500">
        Description (optional)
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          className="mt-1 w-full bg-neutral-950 border border-neutral-800 rounded-md px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600"
        />
      </label>

      <label className="block text-xs text-neutral-500">
        Roles, in order (comma-separated role names — see the Role Library for known names)
        <input
          value={rolesText}
          onChange={(e) => setRolesText(e.target.value)}
          placeholder="e.g. brainstormer, outliner, writer, editor"
          className="mt-1 w-full bg-neutral-950 border border-neutral-800 rounded-md px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600 font-mono"
        />
      </label>

      <label className="block text-xs text-neutral-500">
        Require approval after these roles (optional, comma-separated)
        <input
          value={approvalText}
          onChange={(e) => setApprovalText(e.target.value)}
          className="mt-1 w-full bg-neutral-950 border border-neutral-800 rounded-md px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600 font-mono"
        />
      </label>

      <label className="block text-xs text-neutral-500">
        Domain hint (optional)
        <input
          value={domainHint}
          onChange={(e) => setDomainHint(e.target.value)}
          placeholder="e.g. creative_writing"
          className="mt-1 w-full bg-neutral-950 border border-neutral-800 rounded-md px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600"
        />
      </label>

      {err && <p className="text-xs text-red-400">{err}</p>}

      <div className="flex justify-end gap-2">
        <button type="button" onClick={onCancel} className="text-xs text-neutral-400 hover:text-neutral-200 px-3 py-1.5">
          Cancel
        </button>
        <button
          type="button"
          disabled={saving}
          onClick={submit}
          className="text-xs bg-neutral-100 text-neutral-900 rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save template"}
        </button>
      </div>
    </div>
  );
}

function TemplateCard({ template, apiUrl, apiKey, onDelete }) {
  const [taskText, setTaskText] = useState("");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);

  async function run() {
    if (!taskText.trim()) return;
    setRunning(true);
    setResult(null);
    try {
      const res = await fetch(`${apiUrl}/api/task/from-template`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(apiKey ? { "x-api-key": apiKey } : {}),
        },
        body: JSON.stringify({ template_id: template.template_id, task_text: taskText }),
      });
      const data = await res.json();
      setResult(data);
    } catch (err) {
      setResult({ status: "error", message: String(err) });
    } finally {
      setRunning(false);
    }
  }

  return (
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/50 p-3 space-y-2">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium text-neutral-200">{template.name}</div>
          {template.description && (
            <p className="text-xs text-neutral-500 mt-0.5">{template.description}</p>
          )}
        </div>
        <button
          type="button"
          onClick={() => onDelete(template.template_id)}
          className="shrink-0 text-neutral-600 hover:text-red-400"
          title="Delete template"
        >
          <Trash2 size={13} />
        </button>
      </div>

      <div className="flex flex-wrap gap-1.5">
        {(template.roles || []).map((r, i) => {
          const group = Array.isArray(r);
          const roleNames = group ? r : [r];
          return roleNames.map((roleName) => {
            const category = categorize(roleName) || DEFAULT_CATEGORY;
            return (
              <span
                key={`${i}-${roleName}`}
                className="flex items-center gap-1 text-[11px] rounded border border-neutral-800 px-1.5 py-0.5"
                style={{ color: category.color }}
              >
                {category.icon} {roleName}
                {template.approval_roles?.includes(roleName) && (
                  <span className="text-amber-500" title="requires approval">✋</span>
                )}
              </span>
            );
          });
        })}
      </div>

      <div className="flex gap-2 pt-1">
        <input
          value={taskText}
          onChange={(e) => setTaskText(e.target.value)}
          placeholder="Task text to run through this template…"
          className="flex-1 bg-neutral-950 border border-neutral-800 rounded-md px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600"
        />
        <button
          type="button"
          disabled={running || !taskText.trim()}
          onClick={run}
          className="flex items-center gap-1.5 text-xs bg-neutral-100 text-neutral-900 rounded-lg px-3 py-1.5 font-medium disabled:opacity-50 shrink-0"
        >
          <Play size={12} />
          {running ? "Running…" : "Run"}
        </button>
      </div>

      {result && (
        <div className={`text-xs rounded-md border px-2.5 py-1.5 ${result.status === "error" ? "border-red-900 text-red-400" : "border-neutral-800 text-neutral-400"}`}>
          {result.status === "error"
            ? `Error: ${result.message}`
            : result.status === "paused"
            ? `Paused for approval at role "${result.result?.paused_at_role}" — open Chat to review it.`
            : `Status: ${result.status}. Session: ${result.session_id || "—"}. Open Chat to see the full trace.`}
        </div>
      )}
    </div>
  );
}
