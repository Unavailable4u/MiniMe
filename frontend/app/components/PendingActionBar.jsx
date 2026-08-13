"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import { authHeaders } from "../context/SessionContext";
import { getPusherClient } from "../lib/pusherClient";
import { Check, X, FileEdit, Trash2, Terminal as TerminalIcon, AlertTriangle } from "lucide-react";

/**
 * F2 Part 7 — the confirm/deny half of Part 4's propose/confirm/execute
 * flow, surfaced as real UI for the first time. Until now, POST
 * .../local/propose|confirm|deny existed and worked (Part 4), and every
 * call through them logged onto CO4's timeline (Part 5), but nothing in
 * the frontend ever actually called `propose` for a write/delete/
 * execute_command, or showed a person a pending one to act on -- an
 * agent proposing a mutating local action had no human-in-the-loop
 * surface to land on. This component is that surface, same
 * propose-then-go-live shape as the Deploy Agent, same pause/resume
 * *affordance* (a row of controls attached to the thing waiting on a
 * decision) as CO3's `ApprovalActions` in AgentStepList.jsx -- though
 * the two aren't the same mechanism: CO3 resumes a paused agent graph,
 * this confirms/denies one local-workspace tool call. Deliberately its
 * own small component rather than folding into AgentStepList: a
 * pending local action isn't a graph step and has no role, and this
 * needs to render inside LocalWorkspaceTab (no dock/steps context)
 * as well as, potentially, inline in chat later -- keeping it
 * self-contained (it only needs workspaceId) keeps both possible.
 *
 * Subscribes directly to the workspace's Pusher channel
 * (`workspace-${workspaceId}`) for local_tool_proposed/confirmed/
 * denied events, same pattern PlanTab.jsx already uses for
 * panel_content_updated -- so a pending action shows up here the
 * moment ANY caller (an agent's tool call, another browser tab)
 * proposes one, not just ones proposed from this exact tab.
 *
 * Place this file at: frontend/app/components/PendingActionBar.jsx
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const TOOL_META = {
  write_file: { icon: FileEdit, label: "Write file", tone: "text-sky-500" },
  delete: { icon: Trash2, label: "Delete", tone: "text-rose-500" },
  execute_command: { icon: TerminalIcon, label: "Run command", tone: "text-amber-500" },
};

function describeAction(action) {
  const { tool, params } = action;
  if (tool === "write_file") {
    const bytes = typeof params.content === "string" ? new Blob([params.content]).size : null;
    return `${params.path}${bytes != null ? ` (${bytes} bytes)` : ""}`;
  }
  if (tool === "delete") return params.path;
  if (tool === "execute_command") return params.command;
  return JSON.stringify(params);
}

async function apiPost(path, body) {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: await authHeaders({ json: true }),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `Request failed (${res.status})`);
  }
  return res.json();
}

/**
 * `onConfirmed(action, result)` — called after a successful confirm,
 * so a parent (e.g. LocalWorkspaceTab's Terminal view) can react, such
 * as switching to the Terminal sub-view the moment an execute_command
 * gets confirmed, or refreshing the file tree after a write/delete.
 */
function PendingActionBar({ workspaceId, onConfirmed }) {
  const [actions, setActions] = useState([]); // [{action_id, tool, params, expires_in_seconds}]
  const [busyId, setBusyId] = useState(null);
  const [errorById, setErrorById] = useState({});
  const onConfirmedRef = useRef(onConfirmed);
  onConfirmedRef.current = onConfirmed;

  // Live updates: an agent (or another tab) proposing/confirming/
  // denying an action anywhere shows up here immediately. This is the
  // only way a *proposed-elsewhere* action ever appears in this list --
  // there's no polling fallback, same trade-off PlanTab.jsx's own
  // workspace-channel subscription already accepts (Pusher not
  // configured -> live updates just don't happen, and the docstring
  // there says so plainly rather than silently degrading to a poll).
  useEffect(() => {
    if (!workspaceId) return undefined;
    const pusher = getPusherClient();
    if (!pusher) return undefined;

    const channelName = `workspace-${workspaceId.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
    const channel = pusher.subscribe(channelName);

    const handler = (eventType, data) => {
      const payload = data?.payload || {};
      if (eventType === "local_tool_proposed") {
        const { action_id, tool, path, command } = payload;
        if (!action_id || !tool) return;
        setActions((prev) => {
          if (prev.some((a) => a.action_id === action_id)) return prev;
          const params = tool === "execute_command" ? { command } : { path };
          return [...prev, { action_id, tool, params }];
        });
        return;
      }
      if (eventType === "local_tool_confirmed" || eventType === "local_tool_denied") {
        const { action_id } = payload;
        if (!action_id) return;
        setActions((prev) => prev.filter((a) => a.action_id !== action_id));
        setErrorById((prev) => {
          if (!(action_id in prev)) return prev;
          const next = { ...prev };
          delete next[action_id];
          return next;
        });
      }
    };
    channel.bind_global(handler);

    return () => {
      channel.unbind_global(handler);
      pusher.unsubscribe(channelName);
    };
  }, [workspaceId]);

  const respond = useCallback(async (action, verdict) => {
    setBusyId(action.action_id);
    setErrorById((prev) => {
      const next = { ...prev };
      delete next[action.action_id];
      return next;
    });
    try {
      if (verdict === "confirm") {
        const result = await apiPost(`/api/workspaces/${workspaceId}/local/confirm`, {
          action_id: action.action_id,
        });
        onConfirmedRef.current?.(action, result);
      } else {
        await apiPost(`/api/workspaces/${workspaceId}/local/deny`, { action_id: action.action_id });
      }
      // The local_tool_confirmed/denied Pusher event above will also
      // remove this row -- but that's a round-trip through Pusher this
      // same tab triggered, so remove it optimistically here too rather
      // than leaving the buttons clickable again for the second or two
      // that round-trip takes.
      setActions((prev) => prev.filter((a) => a.action_id !== action.action_id));
    } catch (e) {
      // A 404 here means someone else (another tab, an expiry) already
      // resolved this action -- treat that the same as success (remove
      // the row) rather than leaving a dead row with a confusing error.
      if (/^no pending action|^Request failed \(404\)/.test(e.message)) {
        setActions((prev) => prev.filter((a) => a.action_id !== action.action_id));
      } else {
        setErrorById((prev) => ({ ...prev, [action.action_id]: e.message }));
      }
    } finally {
      setBusyId(null);
    }
  }, [workspaceId]);

  if (actions.length === 0) return null;

  return (
    <div className="shrink-0 border-b border-amber-900/40 bg-amber-950/10 divide-y divide-amber-900/20">
      {actions.map((action) => {
        const meta = TOOL_META[action.tool] || TOOL_META.write_file;
        const Icon = meta.icon;
        const busy = busyId === action.action_id;
        const error = errorById[action.action_id];
        return (
          <div key={action.action_id} className="flex items-start gap-2.5 px-3 py-2 text-xs">
            <Icon size={13} className={`shrink-0 mt-0.5 ${meta.tone}`} />
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-1.5">
                <span className="text-[var(--neutral-300)] font-medium">{meta.label}</span>
                <span className="text-[var(--neutral-600)]">wants to run against your local folder</span>
              </div>
              <div className="mt-0.5 font-mono text-[11px] text-[var(--neutral-400)] truncate" title={describeAction(action)}>
                {describeAction(action)}
              </div>
              {error && (
                <div className="mt-1 flex items-center gap-1 text-[11px] text-rose-500">
                  <AlertTriangle size={11} className="shrink-0" /> {error}
                </div>
              )}
            </div>
            <div className="shrink-0 flex items-center gap-1.5">
              <button
                type="button"
                disabled={busy}
                onClick={() => respond(action, "deny")}
                className="flex items-center gap-1 text-[var(--neutral-400)] hover:text-[var(--neutral-200)] px-2 py-1 rounded-md disabled:opacity-40"
              >
                <X size={12} /> Deny
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() => respond(action, "confirm")}
                className="flex items-center gap-1 bg-[var(--accent)] text-[var(--accent-text)] rounded-lg px-2.5 py-1 font-medium disabled:opacity-40"
              >
                <Check size={12} /> Confirm
              </button>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default PendingActionBar;
