"use client";
import { useEffect, useMemo, useState } from "react";
import { useSession } from "../../context/SessionContext";
import { categorize, DEFAULT_CATEGORY } from "../agentRoleIcons";
import { Pencil, Check, X, RotateCcw } from "lucide-react";

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

function RoleCard({ entry, onSave }) {
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
    <div className="rounded-lg border border-neutral-800 bg-neutral-900/50 p-3 space-y-2">
      <div className="flex items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-sm font-medium" style={{ color: category.color }}>
          <span>{category.icon}</span>
          {entry.role}
        </span>
        <div className="flex items-center gap-1.5">
          <SourceBadge source={entry.source} />
          {entry.times_hired > 0 && (
            <span className="text-[10px] text-neutral-500">hired {entry.times_hired}×</span>
          )}
        </div>
      </div>

      {editing ? (
        <>
          <textarea
            value={brief}
            onChange={(e) => setBrief(e.target.value)}
            rows={4}
            className="w-full resize-none bg-neutral-950 border border-neutral-800 rounded-md px-2.5 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600 leading-relaxed"
          />
          <div className="flex justify-end gap-2 text-xs">
            {isDirty && (
              <button
                type="button"
                onClick={() => setBrief(entry.brief || "")}
                className="flex items-center gap-1 text-neutral-500 hover:text-neutral-300 px-2 py-1"
              >
                <RotateCcw size={11} />
                Revert
              </button>
            )}
            <button
              type="button"
              onClick={() => { setEditing(false); setBrief(entry.brief || ""); }}
              className="flex items-center gap-1 text-neutral-500 hover:text-neutral-300 px-2 py-1"
            >
              <X size={11} />
              Cancel
            </button>
            <button
              type="button"
              disabled={saving || !isDirty}
              onClick={save}
              className="flex items-center gap-1 bg-neutral-100 text-neutral-900 rounded-lg px-2.5 py-1 font-medium disabled:opacity-50"
            >
              <Check size={11} />
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </>
      ) : (
        <div className="flex items-start justify-between gap-2">
          <p className="text-xs text-neutral-400 leading-relaxed">{entry.brief}</p>
          <button
            type="button"
            onClick={() => setEditing(true)}
            className="shrink-0 flex items-center gap-1 text-[11px] text-neutral-500 hover:text-neutral-300 px-2 py-1"
          >
            <Pencil size={11} />
            Edit
          </button>
        </div>
      )}

      {entry.updated_at && (
        <p className="text-[10px] text-neutral-600">
          last updated {new Date(entry.updated_at).toLocaleString()}
        </p>
      )}
    </div>
  );
}

export default function RoleLibraryTab() {
  const { API_URL } = useSession();
  const [roles, setRoles] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState("");

  const API_KEY = process.env.NEXT_PUBLIC_API_KEY || null;

  async function load() {
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/roles`, {
        headers: API_KEY ? { "x-api-key": API_KEY } : {},
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
      headers: {
        "Content-Type": "application/json",
        ...(API_KEY ? { "x-api-key": API_KEY } : {}),
      },
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

  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-3xl mx-auto space-y-4">
      <div>
        <h2 className="text-sm font-medium text-neutral-200">Role Library</h2>
        <p className="text-xs text-neutral-500 mt-1">
          Every role the system has ever briefed — a role's brief is
          reused as-is the next time it's hired, unless it's edited here.
        </p>
      </div>

      <input
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
        placeholder="Filter by role name…"
        className="w-full bg-neutral-950 border border-neutral-800 rounded-lg px-3 py-1.5 text-xs text-neutral-300 outline-none focus:border-neutral-600"
      />

      {error && (
        <p className="text-xs text-red-400">
          Couldn't load the Role Library: {error}. Check that{" "}
          <code className="font-mono">GET /api/roles</code> is reachable.
        </p>
      )}
      {!error && roles === null && <p className="text-xs text-neutral-500">Loading…</p>}
      {!error && roles !== null && grouped.length === 0 && (
        <p className="text-xs text-neutral-500">No roles match that filter.</p>
      )}

      <div className="space-y-5">
        {grouped.map(({ category, entries }) => (
          <section key={category.key} className="space-y-2">
            <h3 className="text-xs font-medium text-neutral-400 flex items-center gap-1.5">
              <span>{category.icon}</span>
              {category.key}
              <span className="text-neutral-600">({entries.length})</span>
            </h3>
            <div className="grid gap-2 sm:grid-cols-2">
              {entries.map((entry) => (
                <RoleCard key={entry.role} entry={entry} onSave={saveRole} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
