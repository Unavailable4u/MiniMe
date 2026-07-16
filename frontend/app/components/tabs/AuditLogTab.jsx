"use client";
import { useEffect, useState } from "react";
import { useSession } from "../../context/SessionContext";
import { ScrollText, User, ShieldAlert, Loader2 } from "lucide-react";

const SUB_TABS = [
  { id: "workspace", label: "Workspace activity" },
  { id: "mine", label: "My activity" },
];

function timeAgo(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return ""; }
}

// Turns "role_change" -> "Role change", leaves already-readable strings alone.
function formatAction(action) {
  if (!action) return "";
  return action.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

function AuditRow({ entry }) {
  const hasDetail = entry.detail && Object.keys(entry.detail).length > 0;
  return (
    <div className="flex items-start gap-3 px-3 py-2 rounded-lg border border-[var(--neutral-800)]">
      <div className="mt-0.5 shrink-0 rounded-full bg-[var(--neutral-900)] p-1.5">
        <ScrollText size={12} className="text-[var(--neutral-500)]" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-[var(--neutral-200)] font-medium">
            {formatAction(entry.action)}
          </span>
          <span className="text-[10px] text-[var(--neutral-600)] shrink-0">
            {timeAgo(entry.created_at)}
          </span>
        </div>
        <p className="text-[10px] text-[var(--neutral-600)] mt-0.5">
          {entry.target_type} · {entry.target_id}
          {entry.user_id ? ` · by ${entry.user_id}` : ""}
        </p>
        {hasDetail && (
          <pre className="text-[10px] text-[var(--neutral-500)] mt-1 whitespace-pre-wrap break-all">
            {JSON.stringify(entry.detail)}
          </pre>
        )}
      </div>
    </div>
  );
}

export default function AuditLogTab() {
  const { workspaces, fetchWorkspaceAudit, fetchMyAudit } = useSession();

  const [selectedId, setSelectedId] = useState(null);
  const [subTab, setSubTab] = useState("workspace");
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!selectedId && workspaces.length > 0) setSelectedId(workspaces[0].id);
  }, [workspaces, selectedId]);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const data =
          subTab === "mine"
            ? await fetchMyAudit(100)
            : selectedId
            ? await fetchWorkspaceAudit(selectedId, 100)
            : [];
        if (!cancelled) setEntries(data);
      } catch (e) {
        if (!cancelled) {
          setEntries([]);
          setError(e.message);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    }
    load();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [subTab, selectedId]);

  return (
    <div className="p-3 space-y-4 text-sm">
      <div className="flex items-center justify-end gap-2">
        {subTab === "workspace" && workspaces.length > 0 && (
          <select
            value={selectedId || ""}
            onChange={(e) => setSelectedId(e.target.value)}
            className="text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1 text-[var(--neutral-300)]"
          >
            {workspaces.map((ws) => (
              <option key={ws.id} value={ws.id}>{ws.name}</option>
            ))}
          </select>
        )}
      </div>

      <div className="flex shrink-0 rounded-lg border border-[var(--neutral-800)] p-0.5 gap-0.5 w-fit">
        {SUB_TABS.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => setSubTab(t.id)}
            className={`text-xs rounded-md px-2.5 py-1 transition-colors flex items-center gap-1.5 ${
              subTab === t.id
                ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
            }`}
          >
            {t.id === "mine" ? <User size={11} /> : <ScrollText size={11} />}
            {t.label}
          </button>
        ))}
      </div>

      {loading && (
        <div className="flex items-center gap-2 text-xs text-[var(--neutral-600)] py-6 justify-center">
          <Loader2 size={14} className="animate-spin" /> Loading…
        </div>
      )}

      {!loading && error && (
        <div className="flex items-start gap-2 text-xs text-amber-500 border border-amber-900/40 rounded-lg px-3 py-2">
          <ShieldAlert size={14} className="shrink-0 mt-0.5" />
          <span>{error}</span>
        </div>
      )}

      {!loading && !error && entries.length === 0 && (
        <p className="text-xs text-[var(--neutral-600)]">
          {subTab === "mine"
            ? "No activity recorded for your account yet."
            : "No activity recorded for this workspace yet."}
        </p>
      )}

      {!loading && !error && entries.length > 0 && (
        <div className="space-y-1.5">
          {entries.map((entry) => (
            <AuditRow key={entry.id} entry={entry} />
          ))}
        </div>
      )}
    </div>
  );
}
