"use client";
import { useEffect, useMemo, useState } from "react";
import { useSession, authHeaders } from "../../context/SessionContext";
import { categorize, DEFAULT_CATEGORY } from "../agentRoleIcons";
import { Pencil, Check, X, RotateCcw, ListChecks, Copy, LayoutTemplate } from "lucide-react";

// Part 2 §2.7 — Role Library panel. Lists every role the system has
// ever briefed (eo/registry.py's list_known_roles() + get_role_metadata(),
// surfaced through GET /api/roles), grouped by the same categorize()
// buckets agentRoleIcons.js already defines for RoutingTraceGraph.jsx —
// so a role's color/icon here is the exact same one a user has already
// seen on the graph, not a second mapping to keep in sync.
//
// Each entry is editable inline, saving through PUT /api/roles/{role}
// (eo/registry.py's update_role_prompt(), source="user_edited") —
// directly surfacing the risk Part 1 flagged: an unreviewed cold-start
// brief silently becoming permanent. A role whose source is still
// "panel_brief_writer" (nobody has reviewed it) gets a visible flag so
// it's easy to find and check.
//
// NEW — multi-select mode + sticky action bar. There's no bulk-delete
// or bulk-edit endpoint on the backend (registry.py only exposes
// per-role update_role_prompt()), so the two actions here are
// deliberately both client-side/no-backend: copying names to the
// clipboard, and handing a role list off to WorkflowTemplatesTab to
// pre-fill a new template (via AppShell's onStartTemplate — see
// AppShell.jsx's pendingTemplateRoles).

const SOURCE_LABEL = {
  seed: { text: "seed", className: "text-sky-400 border-sky-900 bg-sky-950/40" },
  panel_brief_writer: { text: "unreviewed — auto-generated", className: "text-amber-400 border-amber-900 bg-amber-950/40" },
  user_edited: { text: "user edited", className: "text-emerald-400 border-emerald-900 bg-emerald-950/40" },
};

function SourceBadge({ source }) {
  const s = SOURCE_LABEL[source] || SOURCE_LABEL.panel_brief_writer;
  return (
    <span className={`text-[10px] rounded border px-1.5 py-0.5 ${s.className}`}>
      {s.text}
    </span>
  );
}

function RoleCard({ entry, onSave, selectable, selected, onToggleSelect }) {
  const [editing, setEditing] = useState(false);
  const [brief, setBrief] = useState(entry.brief || "");
  const [saving, setSaving] = useState(false);
  const category = categorize(entry.role) || DEFAULT_CATEGORY;
  const isDirty = brief !== (entry.brief || "");

  async function save() {
    setSaving(true);
    try {
      await onSave(entry.role, brief);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className={`rounded-lg border p-[var(--density-card-padding)] space-y-[var(--density-card-gap)] transition-colors ${
        selectable && selected
          ? "border-[var(--neutral-500)] bg-[var(--neutral-800-a70)]"
          : "border-[var(--neutral-800)] bg-[var(--neutral-900-a50)]"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-sm font-medium min-w-0" style={{ color: category.color }}>
          {selectable && (
            <input
              type="checkbox"
              checked={selected}
              onChange={() => onToggleSelect(entry.role)}
              className="shrink-0 accent-[var(--neutral-300)]"
              aria-label={`Select ${entry.role}`}
            />
          )}
          <span className="truncate">
            <span>{category.icon}</span> {entry.role}
          </span>
        </span>
        <div className="flex items-center gap-1.5 shrink-0">
          <SourceBadge source={entry.source} />
          {entry.times_hired > 0 && (
            <span className="text-[10px] text-[var(--neutral-500)]">hired {entry.times_hired}×</span>
          )}
        </div>
      </div>

      {editing ? (
        <>
          <textarea
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            rows={4}
            className="w-full resize-none bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-md px-2.5 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)] leading-[var(--density-line-height)]"
          />
          <div className="flex justify-end gap-2 text-xs">
            {isDirty && (
              <button
                type="button"
                onClick={() => setBrief(entry.brief || "")}
                className="flex items-center gap-1 text-[var(--neutral-500)] hover:text-[var(--neutral-300)] px-2 py-1"
              >
                <RotateCcw size={11} />
                Revert
              </button>
            )}
            <button
              type="button"
              onClick={() => { setEditing(false); setBrief(entry.brief || ""); }}
              className="flex items-center gap-1 text-[var(--neutral-500)] hover:text-[var(--neutral-300)] px-2 py-1"
            >
              <X size={11} />
              Cancel
            </button>
            <button
              type="button"
              disabled={saving || !isDirty}
              onClick={save}
              className="flex items-center gap-1 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-2.5 py-1 font-medium disabled:opacity-50"
            >
              <Check size={11} />
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </>
      ) : (
        <div className="flex items-start justify-between gap-2">
          <p className="text-xs text-[var(--neutral-400)] leading-[var(--density-line-height)]">{entry.brief}</p>
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="shrink-0 flex items-center gap-1 text-[11px] text-[var(--neutral-500)] hover:text-[var(--neutral-300)] px-2 py-1"
          >
            <Pencil size={11} />
            Edit
          </button>
        </div>
      )}

      {entry.updated_at && (
        <p className="text-[10px] text-[var(--neutral-600)]">
          last updated {new Date(entry.updated_at).toLocaleString()}
        </p>
      )}
    </div>
  );
}

export default function RoleLibraryTab({ onStartTemplate }) {
  const { API_URL } = useSession();
  const [roles, setRoles] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("");
  const [selectMode, setSelectMode] = useState(false);
  const [selected, setSelected] = useState([]); // array (not Set) so selection order survives into "use in new template"
  const [copyFeedback, setCopyFeedback] = useState(false);

  async function load() {
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/roles`, {
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      setRoles(await res.json());
    } catch (err) {
      setError(err.message);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [API_URL]);

  async function saveRole(role, brief) {
    const res = await fetch(`${API_URL}/api/roles/${encodeURIComponent(role)}`, {
      method: "PUT",
      headers: await authHeaders({ json: true }),
      body: JSON.stringify({ brief }),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const updated = await res.json();
    setRoles((prev) => (prev || []).map((r) => (r.role === role ? updated : r)));
  }

  const grouped = useMemo(() => {
    const filtered = (roles || []).filter((r) =>
      !filter || r.role.toLowerCase().includes(filter.toLowerCase())
    );
    const groups = {};
    for (const entry of filtered) {
      const category = categorize(entry.role) || DEFAULT_CATEGORY;
      (groups[category.key] ||= { category, entries: [] }).entries.push(entry);
    }
    return Object.values(groups).sort((a, b) => a.category.key.localeCompare(b.category.key));
  }, [roles, filter]);

  const visibleRoleNames = useMemo(
    () => grouped.flatMap((g) => g.entries.map((e) => e.role)),
    [grouped]
  );

  function toggleSelectMode() {
    setSelectMode((prev) => {
      if (prev) setSelected([]); // turning it off always clears the selection
      return !prev;
    });
  }

  function toggleRoleSelected(role) {
    setSelected((prev) => (prev.includes(role) ? prev.filter((r) => r !== role) : [...prev, role]));
  }

  function selectAllVisible() {
    setSelected((prev) => Array.from(new Set([...prev, ...visibleRoleNames])));
  }

  function clearSelection() {
    setSelected([]);
  }

  async function copySelectedNames() {
    try {
      await navigator.clipboard.writeText(selected.join(", "));
      setCopyFeedback(true);
      setTimeout(() => setCopyFeedback(false), 1500);
    } catch {
      // Clipboard API can be unavailable (permissions, non-secure
      // context) — fail quietly rather than throw in the UI.
    }
  }

  function useSelectedInNewTemplate() {
    onStartTemplate?.(selected);
    setSelected([]);
    setSelectMode(false);
  }

  return (
    <div className="h-full flex flex-col">
      <div className="flex-1 overflow-y-auto px-4 py-6 max-w-3xl mx-auto w-full space-y-4">
        <div className="flex items-start justify-between gap-2">
          <div>
            <h2 className="text-sm font-medium text-[var(--neutral-200)]">Role Library</h2>
            <p className="text-xs text-[var(--neutral-500)] mt-1">
              Every role the system has ever briefed — a role's brief is
              reused as-is the next time it's hired, unless it's edited here.
            </p>
          </div>
          <button
            type="button"
            onClick={toggleSelectMode}
            className={`flex items-center gap-1.5 text-xs rounded-lg px-3 py-1.5 font-medium shrink-0 border transition-colors ${
              selectMode
                ? "border-[var(--neutral-500)] text-[var(--neutral-200)] bg-[var(--neutral-800-a70)]"
                : "border-[var(--neutral-800)] text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            <ListChecks size={13} />
            {selectMode ? "Done" : "Select"}
          </button>
        </div>

        <input
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          placeholder="Filter by role name…"
          className="w-full bg-[var(--neutral-950)] border border-[var(--neutral-800)] rounded-lg px-3 py-1.5 text-xs text-[var(--neutral-300)] outline-none focus:border-[var(--neutral-600)]"
        />

        {error && (
          <p className="text-xs text-red-400">
            Couldn't load the Role Library: {error}. Check that{" "}
            <code className="font-mono">GET /api/roles</code> is reachable.
          </p>
        )}
        {!error && roles === null && <p className="text-xs text-[var(--neutral-500)]">Loading…</p>}
        {!error && roles !== null && grouped.length === 0 && (
          <p className="text-xs text-[var(--neutral-500)]">No roles match that filter.</p>
        )}

        <div className="space-y-5">
          {grouped.map(({ category, entries }) => (
            <section key={category.key} className="space-y-2">
              <h3 className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
                <span>{category.icon}</span>
                {category.key}
                <span className="text-[var(--neutral-600)]">({entries.length})</span>
              </h3>
              <div className="grid gap-2 sm:grid-cols-2">
                {entries.map((entry) => (
                  <RoleCard
                    key={entry.role}
                    entry={entry}
                    onSave={saveRole}
                    selectable={selectMode}
                    selected={selected.includes(entry.role)}
                    onToggleSelect={toggleRoleSelected}
                  />
                ))}
              </div>
            </section>
          ))}
        </div>
      </div>

      {/* Sticky action bar — docked below the scroll area (not
          CSS-`sticky` inside it) so it's always visible the moment
          there's a selection, regardless of scroll position. */}
      {selectMode && selected.length > 0 && (
        <div className="shrink-0 border-t border-[var(--neutral-800)] bg-[var(--neutral-900)] px-4 py-2.5">
          <div className="max-w-3xl mx-auto flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-3">
              <span className="text-xs text-[var(--neutral-300)] font-medium">
                {selected.length} selected
              </span>
              <button
                type="button"
                onClick={selectAllVisible}
                className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
              >
                Select all visible
              </button>
              <button
                type="button"
                onClick={clearSelection}
                className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
              >
                Clear
              </button>
            </div>
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={copySelectedNames}
                className="flex items-center gap-1.5 text-xs text-[var(--neutral-400)] hover:text-[var(--neutral-200)] border border-[var(--neutral-800)] rounded-lg px-2.5 py-1.5"
              >
                <Copy size={12} />
                {copyFeedback ? "Copied!" : "Copy names"}
              </button>
              <button
                type="button"
                onClick={useSelectedInNewTemplate}
                className="flex items-center gap-1.5 text-xs bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-2.5 py-1.5 font-medium"
                title="Open Workflow Templates with these roles pre-filled, in selection order"
              >
                <LayoutTemplate size={12} />
                Use in new template
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}