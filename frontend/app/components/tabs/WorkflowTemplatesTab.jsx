"use client";
import { useEffect, useState } from "react";
import { useSession, authHeaders } from "../../context/SessionContext";
import { categorize, DEFAULT_CATEGORY } from "../agentRoleIcons";
import RolePickerOverlay from "../RolePickerOverlay";
import { Trash2, Plus, Play, X, Pencil } from "lucide-react";

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
export default function WorkflowTemplatesTab({ onOpenChat, initialTemplateRoles, onConsumeInitialTemplateRoles }) {
  const { API_URL } = useSession();

  const [templates, setTemplates] = useState(null);
  const [error, setError] = useState(null);
  const [showBuilder, setShowBuilder] = useState(false);
  const [builderInitialRoles, setBuilderInitialRoles] = useState([]);
  const [editingTemplateId, setEditingTemplateId] = useState(null);

  async function load() {
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/workflow-templates`, {
        headers: await authHeaders(),
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

  // NEW — Role Library's sticky selection bar hands off a role list via
  // AppShell (see AppShell.jsx's pendingTemplateRoles). Snapshot it into
  // local state for TemplateBuilder's initial mount, then immediately
  // tell AppShell to clear it — otherwise switching tabs away and back,
  // or clicking "New template" again later, would keep re-opening the
  // builder with the same stale roles.
  useEffect(() => {
    if (initialTemplateRoles && initialTemplateRoles.length > 0) {
      setBuilderInitialRoles(initialTemplateRoles);
      setShowBuilder(true);
      onConsumeInitialTemplateRoles?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialTemplateRoles]);

  async function deleteTemplate(templateId) {
    const res = await fetch(`${API_URL}/api/workflow-templates/${templateId}`, {
      method: "DELETE",
      headers: await authHeaders(),
    });
    if (res.ok) setTemplates((prev) => (prev || []).filter((t) => t.template_id !== templateId));
  }

  async function saveTemplate(payload) {
    const res = await fetch(`${API_URL}/api/workflow-templates`, {
      method: "POST",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const saved = await res.json();
    setTemplates((prev) => [saved, ...(prev || [])]);
    setShowBuilder(false);
    setBuilderInitialRoles([]);
  }

  // Template editing — POST/DELETE existed already; this is the missing
  // update path onto PUT /api/workflow-templates/{id} (previously dead:
  // no UI ever called it, so the endpoint was unreachable).
  async function updateTemplate(templateId, payload) {
    const res = await fetch(`${API_URL}/api/workflow-templates/${templateId}`, {
      method: "PUT",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify(payload),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const updated = await res.json();
    setTemplates((prev) => (prev || []).map((t) => (t.template_id === templateId ? updated : t)));
    setEditingTemplateId(null);
  }

  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-3xl mx-auto space-y-4">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-medium text-[var(--neutral-200)]">Workflow Templates</h2>
          <p className="text-xs text-[var(--neutral-500)] mt-1">
            Saved role pipelines you can start a task from directly,
            skipping automatic classification.
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            setBuilderInitialRoles([]);
            setEditingTemplateId(null);
            setShowBuilder((s) => !s);
          }}
          className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium shrink-0"
        >
          <Plus size={13} />
          New template
        </button>
      </div>

      {showBuilder && (
        <TemplateBuilder
          onSave={saveTemplate}
          onCancel={() => { setShowBuilder(false); setBuilderInitialRoles([]); }}
          apiUrl={API_URL}
          initialRoles={builderInitialRoles}
        />
      )}

      {error && (
        <p className="text-xs text-red-400">
          Couldn't load templates: {error}. Check that{" "}
          <code className="font-mono">GET /api/workflow-templates</code> is reachable.
        </p>
      )}
      {!error && templates === null && <p className="text-xs text-[var(--neutral-500)]">Loading…</p>}
      {!error && templates !== null && templates.length === 0 && !showBuilder && (
        <p className="text-xs text-[var(--neutral-500)]">
          No saved templates yet. Build one from the Role Library, or
          click "New template" above.
        </p>
      )}

      <div className="space-y-2">
        {(templates || []).map((t) => (
          <TemplateCard
            key={t.template_id}
            template={t}
            apiUrl={API_URL}
            onDelete={deleteTemplate}
            onOpenChat={onOpenChat}
            isEditing={editingTemplateId === t.template_id}
            onStartEdit={() => { setShowBuilder(false); setEditingTemplateId(t.template_id); }}
            onCancelEdit={() => setEditingTemplateId(null)}
            onUpdate={updateTemplate}
          />
        ))}
      </div>
    </div>
  );
}

function TemplateBuilder({ onSave, onCancel, apiUrl, initialRoles, initialValues, heading = "Build a template", submitLabel = "Save template", savingLabel = "Saving…" }) {
  const [name, setName] = useState(initialValues?.name || "");
  const [description, setDescription] = useState(initialValues?.description || "");
  const [roles, setRoles] = useState(() => initialValues?.roles || initialRoles || []);
  const [approvalRoles, setApprovalRoles] = useState(initialValues?.approval_roles || []);
  const [domainHint, setDomainHint] = useState(initialValues?.domain_hint || "");
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState(null);

  async function submit() {
    if (!name.trim() || roles.length === 0) {
      setErr("A name and at least one role are required.");
      return;
    }
    setSaving(true);
    setErr(null);
    try {
      await onSave({
        name: name.trim(),
        description: description.trim(),
        roles,
        approval_roles: approvalRoles,
        domain_hint: domainHint.trim() || null,
      });
    } catch (e) {
      setErr(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-950-a50)] p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium text-[var(--neutral-200)]">{heading}</h3>
        <button type="button" onClick={onCancel} className="text-[var(--neutral-500)] hover:text-[var(--neutral-300)]">
          <X size={14} />
        </button>
      </div>

      <label className="block text-xs text-[var(--neutral-500)]">
        Name
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Blog post pipeline"
          className="mt-1 w-full bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)]"
        />
      </label>

      <label className="block text-xs text-[var(--neutral-500)]">
        Description (optional)
        <input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          className="mt-1 w-full bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)]"
        />
      </label>

      {/* Combobox overlay — picks known roles (fetched from the Role
          Library) or lets you type a new one, keeps them in pipeline
          order, and folds the old separate "approval roles" text field
          into a per-chip ✋ toggle so it can only ever reference a role
          that's actually selected. */}
      <RolePickerOverlay
        apiUrl={apiUrl}
        roles={roles}
        onRolesChange={setRoles}
        approvalRoles={approvalRoles}
        onApprovalRolesChange={setApprovalRoles}
      />

      <label className="block text-xs text-[var(--neutral-500)]">
        Domain hint (optional)
        <input
          value={domainHint}
          onChange={(e) => setDomainHint(e.target.value)}
          placeholder="e.g. creative_writing"
          className="mt-1 w-full bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)]"
        />
      </label>

      {err && <p className="text-xs text-red-400">{err}</p>}

      <div className="flex justify-end gap-2">
        <button type="button" onClick={onCancel} className="text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] px-3 py-1.5">
          Cancel
        </button>
        <button
          type="button"
          disabled={saving}
          onClick={submit}
          className="text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
        >
          {saving ? savingLabel : submitLabel}
        </button>
      </div>
    </div>
  );
}

function TemplateCard({ template, apiUrl, onDelete, onOpenChat, isEditing, onStartEdit, onCancelEdit, onUpdate }) {
  // NEW — running/result now live in SessionContext's `templateRuns`,
  // keyed by template_id, instead of local useState here. AppShell
  // fully unmounts this component whenever the person switches tabs
  // (`<Active />` swaps component identity), which was silently
  // discarding local `running`/`result` state — the run kept going on
  // the backend the whole time, the UI just had no memory of it once
  // you came back. See SessionContext.jsx's templateRuns/runTemplate
  // for the actual dispatch + chat-persistence logic.
  const { runTemplate, templateRuns } = useSession();
  const [taskText, setTaskText] = useState("");
  const runState = templateRuns[template.template_id] || { running: false, result: null, chatId: null };

  function run() {
    if (!taskText.trim() || runState.running) return;
    runTemplate(template.template_id, taskText);
  }

  // Template editing (previously dead PUT endpoint) — reuses the same
  // TemplateBuilder form the "New template" flow uses, just prefilled
  // and wired to onUpdate() instead of onSave(), inline in place of the
  // card's normal display.
  if (isEditing) {
    return (
      <TemplateBuilder
        apiUrl={apiUrl}
        heading={`Edit "${template.name}"`}
        submitLabel="Save changes"
        savingLabel="Saving…"
        initialValues={{
          name: template.name,
          description: template.description || "",
          roles: template.roles || [],
          approval_roles: template.approval_roles || [],
          domain_hint: template.domain_hint || "",
        }}
        onSave={(payload) => onUpdate(template.template_id, payload)}
        onCancel={onCancelEdit}
      />
    );
  }

  return (
    <div className="rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-900-a50)] p-[var(--density-card-padding)] space-y-[var(--density-card-gap)]">
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium text-[var(--neutral-200)]">{template.name}</div>
          {template.description && (
            <p className="text-xs text-[var(--neutral-500)] mt-0.5">{template.description}</p>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button
            type="button"
            onClick={onStartEdit}
            className="text-[var(--neutral-600)] hover:text-[var(--neutral-300)]"
            title="Edit template"
          >
            <Pencil size={13} />
          </button>
          <button
            type="button"
            onClick={() => onDelete(template.template_id)}
            className="text-[var(--neutral-600)] hover:text-red-400"
            title="Delete template"
          >
            <Trash2 size={13} />
          </button>
        </div>
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
                className="flex items-center gap-1 text-[11px] rounded border border-[var(--neutral-800)] px-1.5 py-0.5"
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
          className="flex-1 bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)]"
        />
        <button
          type="button"
          disabled={runState.running || !taskText.trim()}
          onClick={run}
          className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50 shrink-0"
        >
          <Play size={12} />
          {runState.running ? "Running…" : "Run"}
        </button>
      </div>

      {runState.running && (
        <div className="text-xs rounded-md border border-[var(--neutral-800)] px-2.5 py-1.5 text-[var(--neutral-500)] animate-pulse">
          Running — you can switch tabs, this keeps going in the background.
        </div>
      )}

      {!runState.running && runState.result && (
        <div className={`flex items-center justify-between gap-2 text-xs rounded-md border px-2.5 py-1.5 ${runState.result.status === "error" ? "border-red-900 text-red-400" : "border-[var(--neutral-800)] text-[var(--neutral-400)]"}`}>
          <span>
            {runState.result.status === "error"
              ? `Error: ${runState.result.message}`
              : runState.result.status === "paused"
              ? `Paused for approval at role "${runState.result.result?.paused_at_role}".`
              : `Status: ${runState.result.status}.`}
          </span>
          {runState.chatId && (
            <button
              type="button"
              onClick={() => onOpenChat?.(runState.chatId)}
              className="shrink-0 underline text-[var(--neutral-300)] hover:text-[var(--neutral-100)]"
            >
              Open chat →
            </button>
          )}
        </div>
      )}
    </div>
  );
}