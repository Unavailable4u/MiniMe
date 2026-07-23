"use client";
import { useEffect, useState } from "react";
import { useSession, authHeaders } from "../../context/SessionContext";
import WorkspaceChatPanel from "../WorkspaceChatPanel";
import WorkspaceDataBubble from "../WorkspaceDataBubble";
import { Layers, Loader2, ArrowUpRight, ChevronRight, MessageSquare } from "lucide-react";
// Part 8.9: replaces the old static NEXT_PUBLIC_API_KEY/x-api-key header
// -- every fetch() below now sends the real per-user Supabase JWT via
// authHeaders(), matching require_auth()'s Authorization: Bearer check.

// §7 fix: Tasks is now scoped to a build-stage workspace instead of
// whatever chat happens to be open. Same left-hand picker pattern as
// NotebooksTab.jsx (localStorage-persisted selection, auto-select first
// item once loaded), filtered to workspace.stage === "build" instead of
// "note". Requires the backend's SELECTs in eo/chat_workspace.py's
// list_workspaces()/get_workspace() to actually return w.stage --
// right now they don't, so every workspace reads as stage "note" and
// this filter would show nothing. That's a pre-existing bug, not
// something introduced here, but it blocks this feature until fixed.
const SELECTED_BUILD_WS_KEY = "minime_tasks_selected_ws_id";
const CHAT_DOCK_KEY = "minime_build_chatdock_collapsed";
const PROMOTE_TARGETS = ["test", "growth"];
const PROMOTE_LABELS = {
  test: "Test",
  growth: "Growth",
};

// feature_status's own value vocabulary (see agents/idea_planner.py's
// SYSTEM_PROMPT) -- "done" | "in_progress" | missing. No new taxonomy
// invented here; "missing" is just the absence of a feature_status entry
// for a feature that current_plan["features"] lists.
const COLUMNS = [
  { status: "missing", label: "Missing" },
  { status: "in_progress", label: "In Progress" },
  { status: "done", label: "Done" },
];

function statusFor(featureStatus, featureName) {
  return featureStatus[featureName] || "missing";
}

function Card({ title, action, children }) {
  return (
    <div className="cyber-panel p-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="font-display text-[11px] uppercase tracking-wide text-cyber-dim">{title}</h3>
        {action}
      </div>
      {children}
    </div>
  );
}

// module_specs (agents/prompt_writer.py) is {"modules": [{name, description,
// inputs, outputs, edge_cases}, ...]} for whichever ONE feature
// current_plan["target_feature"] names this cycle -- it is not keyed by
// feature name, and there is no per-feature history for features that
// haven't been the target yet. So only the target feature card can
// expand to something real; every other card is a plain label.
function FeatureCard({ name, isTarget, targetModules, expanded, onToggle }) {
  const canExpand = isTarget;
  return (
    <div className="cyber-panel p-3 space-y-2">
      <button
        type="button"
        onClick={canExpand ? onToggle : undefined}
        disabled={!canExpand}
        className={`w-full text-left flex items-start justify-between gap-2 ${canExpand ? "" : "cursor-default"}`}
      >
        <span className="text-xs text-cyber-text">{name}</span>
        {isTarget && (
          <span className="shrink-0 font-display text-[9px] uppercase tracking-wide text-cyber-cyan border border-cyber-cyan/40 rounded px-1.5 py-0.5">
            this cycle
          </span>
        )}
      </button>
      {expanded && canExpand && (
        <div className="pt-2 border-t border-cyber-border text-[11px] text-cyber-dim space-y-2">
          {(!targetModules || targetModules.length === 0) && (
            <p>No module_specs recorded for this cycle yet.</p>
          )}
          {targetModules?.map((m, i) => (
            <div key={m.name || i} className="space-y-0.5">
              <p className="text-cyber-text font-mono">{m.name}</p>
              {m.description && <p>{m.description}</p>}
              {m.inputs && <p><span className="text-cyber-dim/70">in:</span> {String(m.inputs)}</p>}
              {m.outputs && <p><span className="text-cyber-dim/70">out:</span> {String(m.outputs)}</p>}
              {Array.isArray(m.edge_cases) && m.edge_cases.length > 0 && (
                <p><span className="text-cyber-dim/70">edge cases:</span> {m.edge_cases.join(", ")}</p>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// Part 7 §7.3 -- fixed six-category vocabulary, matching
// eo/registry.py's integration_flagger seed brief exactly (no new
// taxonomy invented on the frontend either).
const INTEGRATION_LABELS = {
  auth: "Auth",
  payments: "Payments",
  email_notifications: "Email / Notifications",
  analytics: "Analytics",
  file_storage: "File Storage",
  monitoring: "Monitoring",
};

function IntegrationChecklist({ integrations }) {
  if (!integrations || integrations.length === 0) {
    return (
      <Card title="Integrations flagged">
        <p className="text-[11px] text-cyber-dim">
          None flagged yet -- integration_flagger runs once, early in the
          cycle, and its result is cached for the rest of this session.
        </p>
      </Card>
    );
  }
  return (
    <Card title={`Integrations flagged (${integrations.length})`}>
      <ul className="space-y-1.5">
        {integrations.map((item, i) => (
          <li key={`${item.type}-${i}`} className="text-[11px] flex items-start gap-2">
            <span className="shrink-0 font-display uppercase tracking-wide text-[10px] text-cyber-cyan border border-cyber-cyan/40 rounded px-1.5 py-0.5">
              {INTEGRATION_LABELS[item.type] || item.type}
            </span>
            <span className="text-cyber-dim">{item.evidence}</span>
          </li>
        ))}
      </ul>
    </Card>
  );
}

// Part 7 §7.6 -- deploy action button + status indicator, three separate
// calls matching the three separate-risk backend endpoints from §7.4
// (propose / write / go-live). "Go Live" is intentionally left
// unwired for now -- see the accompanying chat message: the backend
// endpoint currently blocks on a server-terminal y/N prompt
// (agents/deploy_agent.py's _confirm_deploy()), which a browser fetch()
// can't answer. Wiring it here today would just hang the request.
function DeployPanel({ sessionId, apiUrl, deployConfigPlan, lastDeployConfigSummary, onRefresh }) {
  const [busy, setBusy] = useState(null); // "propose" | "write" | null
  const [error, setError] = useState(null);

  async function call(action) {
    setBusy(action);
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/deploy/${sessionId}/${action}`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({}),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  const hasConfig = Boolean(lastDeployConfigSummary);
  const hasPlan = Boolean(deployConfigPlan);

  return (
    <Card title="Deploy">
      <div className="space-y-2 text-[11px]">
        {!hasPlan && (
          <p className="text-cyber-dim">
            No deploy plan proposed yet for this project.
          </p>
        )}
        {hasPlan && (
          <div className="text-cyber-text">
            <p>
              <span className="text-cyber-dim/70">platform:</span>{" "}
              {deployConfigPlan.platform}
            </p>
            <p>
              <span className="text-cyber-dim/70">config file:</span>{" "}
              <span className="font-mono">{deployConfigPlan.config_filename}</span>
            </p>
            {deployConfigPlan.reason && (
              <p className="text-cyber-dim">{deployConfigPlan.reason}</p>
            )}
          </div>
        )}
        {hasConfig && (
          <p className="text-cyber-cyan">
            Config written to disk ({lastDeployConfigSummary.config_filename}) --
            ready for a manual deploy, or for "Go Live" once that's wired up.
          </p>
        )}
        {error && <p className="text-rose-400">{error}</p>}
        <div className="flex gap-2 pt-1">
          <button
            type="button"
            disabled={busy !== null}
            onClick={() => call("propose")}
            className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
          >
            {busy === "propose" ? "Proposing..." : hasPlan ? "Re-propose" : "Propose"}
          </button>
          <button
            type="button"
            disabled={busy !== null || !hasPlan}
            onClick={() => call("write")}
            className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
          >
            {busy === "write" ? "Writing..." : "Write Config"}
          </button>
          <button
            type="button"
            disabled
            title="Not wired up yet -- see chat"
            className="font-display text-[10px] uppercase tracking-wide border border-cyber-dim/30 text-cyber-dim/50 rounded px-2 py-1 cursor-not-allowed"
          >
            Go Live
          </button>
        </div>
      </div>
    </Card>
  );
}

// Part 7 §7.6 -- monitoring widget. Sentry is read-only status (it's an
// ordinary generated module, nothing for the user to configure here);
// UptimeRobot needs a one-time API key + a URL to register, since
// agents/deploy_agent.py's register_uptimerobot_monitor() deliberately
// takes an explicit url rather than reading one off a real deploy (no
// real host client exists yet -- see that module's docstring).
const SENTRY_STATUS_LABELS = {
  not_planned: "Not planned",
  planned: "Planned this cycle",
  configured: "Configured",
};

function MonitoringWidget({ sessionId, apiUrl, monitoring, onRefresh }) {
  const [apiKey, setApiKey] = useState("");
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(null); // "key" | "register" | null
  const [error, setError] = useState(null);

  const sentryStatus = monitoring?.sentry_status || "not_planned";
  const uptimerobot = monitoring?.uptimerobot || null;

  async function saveKey() {
    if (!apiKey) return;
    setBusy("key");
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/monitoring/${sessionId}/uptimerobot-key`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ api_key: apiKey }),
      });
      if (!res.ok) throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      setApiKey("");
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  async function register() {
    if (!url) return;
    setBusy("register");
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/monitoring/${sessionId}/uptimerobot-register`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ url }),
      });
      const body = await res.json().catch(() => null);
      if (!res.ok) throw new Error(body?.detail || `${res.status} ${res.statusText}`);
      await onRefresh();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card title="Monitoring">
      <div className="space-y-3 text-[11px]">
        <div>
          <span className="text-cyber-dim/70">Sentry:</span>{" "}
          <span className="text-cyber-text">{SENTRY_STATUS_LABELS[sentryStatus] || sentryStatus}</span>
        </div>

        <div className="space-y-1.5">
          <span className="text-cyber-dim/70">UptimeRobot:</span>{" "}
          {uptimerobot ? (
            uptimerobot.status === "registered" ? (
              <span className="text-cyber-cyan">
                registered -- {uptimerobot.friendly_name} ({uptimerobot.url})
              </span>
            ) : (
              <span className="text-rose-400">{uptimerobot.message}</span>
            )
          ) : (
            <span className="text-cyber-dim">not registered yet</span>
          )}

          <div className="flex flex-wrap gap-1.5 pt-1">
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder="UptimeRobot API key"
              className="flex-1 min-w-[140px] bg-cyber-bg border border-cyber-border rounded px-2 py-1 text-cyber-text placeholder:text-cyber-dim/50 outline-none"
            />
            <button
              type="button"
              disabled={busy !== null || !apiKey}
              onClick={saveKey}
              className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
            >
              {busy === "key" ? "Saving..." : "Save Key"}
            </button>
          </div>
          <div className="flex flex-wrap gap-1.5">
            <input
              type="text"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://your-deployed-url.example.com"
              className="flex-1 min-w-[180px] bg-cyber-bg border border-cyber-border rounded px-2 py-1 text-cyber-text placeholder:text-cyber-dim/50 outline-none"
            />
            <button
              type="button"
              disabled={busy !== null || !url}
              onClick={register}
              className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
            >
              {busy === "register" ? "Registering..." : "Register"}
            </button>
          </div>
          <p className="text-cyber-dim/70">
            No live URL from a real deploy yet (see the Deploy panel above)
            -- paste the URL to monitor manually for now.
          </p>
        </div>

        {error && <p className="text-rose-400">{error}</p>}
      </div>
    </Card>
  );
}

// Parts pricing — live-fetch panel, same shape as DeployPanel/
// MonitoringWidget above: a direct fetch() with authHeaders(), not the
// paste-panel pattern PlanTab.jsx uses. Parts live in
// workspace_facts.custom.parts (see api/server.py's refresh_part_prices),
// read back via the existing GET /api/workspaces/{ws_id}/facts endpoint
// rather than a new one -- no dedicated parts store exists yet.
function PartsPanel({ wsId, apiUrl }) {
  const [parts, setParts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);
  const [newPartName, setNewPartName] = useState("");
  const [newPartQty, setNewPartQty] = useState(1);

  async function loadFacts() {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/workspaces/${wsId}/facts`, {
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
      const facts = await res.json();
      setParts(facts?.custom?.parts || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (wsId) loadFacts();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wsId]);

  function addPart() {
    if (!newPartName.trim()) return;
    setParts((prev) => [
      ...prev,
      { id: crypto.randomUUID(), name: newPartName.trim(), qty: Number(newPartQty) || 1 },
    ]);
    setNewPartName("");
    setNewPartQty(1);
  }

  function removePart(id) {
    setParts((prev) => prev.filter((p) => p.id !== id));
  }

  async function refreshPrices(forceRefresh = false) {
    if (parts.length === 0) return;
    setRefreshing(true);
    setError(null);
    try {
      const res = await fetch(`${apiUrl}/api/workspaces/${wsId}/parts/refresh-prices`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({ parts, force_refresh: forceRefresh }),
      });
      if (!res.ok) {
        throw new Error((await res.json().catch(() => null))?.detail || `${res.status} ${res.statusText}`);
      }
      const { parts: updated } = await res.json();
      setParts(updated);
    } catch (err) {
      setError(err.message);
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <Card
      title={`Parts${parts.length ? ` (${parts.length})` : ""}`}
      action={
        <button
          type="button"
          disabled={refreshing || parts.length === 0}
          onClick={() => refreshPrices(false)}
          className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1 disabled:opacity-50"
        >
          {refreshing ? "Refreshing..." : "Refresh prices"}
        </button>
      }
    >
      <div className="space-y-2 text-[11px]">
        {loading ? (
          <p className="text-cyber-dim">Loading...</p>
        ) : (
          <>
            {parts.length === 0 && (
              <p className="text-cyber-dim">No parts added yet.</p>
            )}
            {parts.map((p) => (
              <div key={p.id} className="flex items-center justify-between gap-2 border-b border-cyber-border/50 pb-1.5">
                <div className="min-w-0">
                  <p className="text-cyber-text truncate">{p.name} <span className="text-cyber-dim/70">×{p.qty}</span></p>
                  {p.estimated_price_bdt != null ? (
                    <p className="text-cyber-dim">
                      ৳{p.estimated_price_bdt}
                      {p.vendor_url ? (
                        <a href={p.vendor_url} target="_blank" rel="noreferrer" className="text-cyber-cyan ml-1 underline">
                          {p.vendor_name || "source"}
                        </a>
                      ) : null}
                      {p.price_checked_at && (
                        <span className="text-cyber-dim/60"> — checked {new Date(p.price_checked_at).toLocaleDateString()}</span>
                      )}
                    </p>
                  ) : (
                    <p className="text-cyber-dim/60">Not priced yet</p>
                  )}
                </div>
                <button onClick={() => removePart(p.id)} className="text-cyber-dim/60 hover:text-rose-400 shrink-0">✕</button>
              </div>
            ))}
          </>
        )}
        {error && <p className="text-rose-400">{error}</p>}
        <div className="flex gap-1.5 pt-1">
          <input
            value={newPartName}
            onChange={(e) => setNewPartName(e.target.value)}
            placeholder="Part name, e.g. HolyBro Kakute H7 V2"
            className="flex-1 min-w-0 bg-black/30 border border-cyber-border rounded px-2 py-1 text-[11px] outline-none focus:border-cyber-cyan"
          />
          <input
            type="number"
            min={1}
            value={newPartQty}
            onChange={(e) => setNewPartQty(e.target.value)}
            className="w-14 bg-black/30 border border-cyber-border rounded px-2 py-1 text-[11px] outline-none focus:border-cyber-cyan"
          />
          <button
            type="button"
            onClick={addPart}
            className="font-display text-[10px] uppercase tracking-wide border border-cyber-cyan/40 text-cyber-cyan rounded px-2 py-1"
          >
            Add
          </button>
        </div>
      </div>
    </Card>
  );
}

export default function BuildTab({ onPromoted }) {
  // §7 fix: workspaces + promoteWorkspace come from the same
  // SessionContext NotebooksTab/ResearchTab already use — no new context
  // plumbing needed, Tasks just reads the shared list and filters it.
  const { workspaces, fetchWorkspaces, promoteWorkspace, API_URL } = useSession();

  // Build-stage workspaces only -- the "picked" list for this tab, same
  // shape as NotebooksTab's `notebooks` / ResearchTab's `researchProjects`.
  const buildProjects = workspaces.filter((w) => (w.active_stages || [w.stage]).includes("build"));

  const [selectedWsId, setSelectedWsId] = useState(null);
  const [restoredSelection, setRestoredSelection] = useState(false);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedFeature, setExpandedFeature] = useState(null);
  const [promoting, setPromoting] = useState(false);
  const [promoteError, setPromoteError] = useState(null);
  const [promoteTargetStage, setPromoteTargetStage] = useState("test");
  // NEW — §2.6 step 4: "complete" (existing behavior, leaves this tab)
  // vs "partial" (stays active here too, per §2.1/§2.2). Same toggle as
  // Notebooks/Research/Plan.
  const [promoteMode, setPromoteMode] = useState("complete");
  const [chatDockCollapsed, setChatDockCollapsed] = useState(false);

  useEffect(() => {
    setChatDockCollapsed(localStorage.getItem(CHAT_DOCK_KEY) === "1");
  }, []);

  function toggleChatDock() {
    setChatDockCollapsed((prev) => {
      localStorage.setItem(CHAT_DOCK_KEY, !prev ? "1" : "0");
      return !prev;
    });
  }

  // Restore last-selected build project on mount (same pattern as
  // NotebooksTab's SELECTED_NOTEBOOK_KEY restore effect).
  useEffect(() => {
    const savedId = localStorage.getItem(SELECTED_BUILD_WS_KEY);
    if (savedId) setSelectedWsId(savedId);
    setRestoredSelection(true);
  }, []);

  useEffect(() => {
    if (!restoredSelection || !selectedWsId) return;
    localStorage.setItem(SELECTED_BUILD_WS_KEY, selectedWsId);
  }, [selectedWsId, restoredSelection]);

  // Auto-select the first build project once loaded, or recover if a
  // previously-saved selection was promoted onward / deleted.
  useEffect(() => {
    if (!restoredSelection || buildProjects.length === 0) return;
    const stillExists = selectedWsId && buildProjects.some((w) => w.id === selectedWsId);
    if (!stillExists) setSelectedWsId(buildProjects[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [buildProjects, selectedWsId, restoredSelection]);

  // data._session_id is the raw chat_id the backend resolved ws_id to
  // (see api/server.py's get_tasks_for_workspace) -- DeployPanel and
  // MonitoringWidget still hit /api/deploy/{session_id}/... and
  // /api/monitoring/{session_id}/... directly, unchanged, so they need
  // that resolved id rather than the workspace id.
  const resolvedSessionId = data?._session_id || null;

  async function refresh() {
    if (!selectedWsId) return;
    const res = await fetch(`${API_URL}/api/tasks/workspace/${selectedWsId}`, {
      headers: await authHeaders(),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const d = await res.json();
    setData(d);
    return d;
  }

  useEffect(() => {
    if (!selectedWsId) {
      setData(null);
      setLoading(false);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    refresh()
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [API_URL, selectedWsId]);

  async function handlePromote(wsId, toStage = promoteTargetStage, mode = promoteMode) {
    setPromoting(true);
    setPromoteError(null);
    try {
      await promoteWorkspace(wsId, toStage, mode);
      await fetchWorkspaces();
      onPromoted?.(toStage, wsId);
      setPromoteMode("complete");
    } catch (err) {
      setPromoteError(err.message);
    } finally {
      setPromoting(false);
    }
  }

  const selected = buildProjects.find((w) => w.id === selectedWsId);

  const features = data?.current_plan?.features || [];
  const featureStatus = data?.feature_status || {};
  const targetFeature = data?.current_plan?.target_feature || null;
  const targetModules = data?.module_specs?.modules || null;
  const byColumn = COLUMNS.map((col) => ({
    ...col,
    features: features.filter((f) => statusFor(featureStatus, f) === col.status),
  }));

  return (
    <div className="flex h-full">
      {/* Build-project picker -- same left column pattern as
          NotebooksTab/ResearchTab, filtered to stage === "build" instead
          of "note"/"research". */}
      <div className="w-56 shrink-0 border-r border-[var(--neutral-800)] flex flex-col h-full">
        <div className="flex items-center justify-between px-3 py-3 border-b border-[var(--neutral-800)]">
          <span className="text-xs font-medium text-[var(--neutral-400)] flex items-center gap-1.5">
            <Layers size={13} /> Build
          </span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {buildProjects.map((ws) => (
            <button
              key={ws.id}
              onClick={() => setSelectedWsId(ws.id)}
              className={`w-full flex items-center justify-between gap-1 px-3 py-2 text-left border-b border-[var(--neutral-900)] ${
                ws.id === selectedWsId ? "bg-[var(--neutral-800-a70)]" : "hover:bg-[var(--neutral-900)]"
              }`}
            >
              <span className="text-xs text-[var(--neutral-200)] truncate">{ws.name}</span>
              {ws.id === selectedWsId && <ChevronRight size={12} className="text-[var(--neutral-500)] shrink-0" />}
            </button>
          ))}
          {buildProjects.length === 0 && (
            <p className="px-3 py-3 text-xs text-[var(--neutral-600)]">
              No build-stage projects yet — promote a project to Build from the Plan tab to see it here.
            </p>
          )}
        </div>
      </div>

      {/* Selected project's board */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        {!selected ? (
          <div className="h-full flex items-center justify-center text-sm text-[var(--neutral-600)]">
            Select a build project to see its task board.
          </div>
        ) : loading ? (
          <div className="px-4 py-6 max-w-4xl mx-auto">
            <p className="text-xs text-cyber-dim">Loading board...</p>
          </div>
        ) : error ? (
          <div className="px-4 py-6 max-w-4xl mx-auto">
            <p className="text-xs text-rose-400">
              Couldn't load the task board: {error}. Check that{" "}
              <code className="font-mono">GET /api/tasks/workspace/{"{ws_id}"}</code> is reachable
              and that you're signed in with a valid session.
            </p>
          </div>
        ) : (
          <div className="relative px-4 py-6 max-w-4xl mx-auto space-y-4">
            <WorkspaceDataBubble
              workspaceId={selected.id}
              workspaceName={selected.name}
              storageKey="minime_build_data_bubble_collapsed"
            />
            <div className="flex items-center justify-between gap-3">
              <h2 className="text-base font-medium text-[var(--neutral-100)]">{selected.name}</h2>
              <div className="flex items-center gap-2 shrink-0">
                {(() => {
                  // NEW — §2.2: exclude stages already active for this
                  // workspace — same rule as Notebooks/Research/Plan.
                  const activeHere = selected.active_stages || [selected.stage];
                  const availableTargets = PROMOTE_TARGETS.filter((s) => !activeHere.includes(s));
                  const targetStage = availableTargets.includes(promoteTargetStage)
                    ? promoteTargetStage
                    : availableTargets[0];
                  if (!availableTargets.length) return null;
                  return (
                    <>
                      <label className="sr-only" htmlFor="build-promote-target">Promote to</label>
                      <select
                        id="build-promote-target"
                        value={targetStage}
                        onChange={(e) => setPromoteTargetStage(e.target.value)}
                        disabled={promoting}
                        className="bg-[var(--neutral-900)] border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-2 py-1.5 text-xs outline-none disabled:opacity-50"
                      >
                        {availableTargets.map((stage) => (
                          <option key={stage} value={stage}>{PROMOTE_LABELS[stage]}</option>
                        ))}
                      </select>
                      {/* NEW — §2.6 step 4: complete/partial toggle. */}
                      <div
                        role="radiogroup"
                        aria-label="Promote mode"
                        className="flex items-center rounded-lg border border-[var(--neutral-700)] overflow-hidden text-xs shrink-0"
                      >
                        <button
                          type="button"
                          role="radio"
                          aria-checked={promoteMode === "complete"}
                          onClick={() => setPromoteMode("complete")}
                          disabled={promoting}
                          title="Move the project fully into the target stage"
                          className={`px-2 py-1.5 font-medium disabled:opacity-50 ${
                            promoteMode === "complete"
                              ? "bg-[var(--accent)] text-[var(--accent-text)]"
                              : "bg-[var(--neutral-900)] text-[var(--neutral-400)]"
                          }`}
                        >
                          Complete
                        </button>
                        <button
                          type="button"
                          role="radio"
                          aria-checked={promoteMode === "partial"}
                          onClick={() => setPromoteMode("partial")}
                          disabled={promoting}
                          title="Keep the project active here too"
                          className={`px-2 py-1.5 font-medium disabled:opacity-50 ${
                            promoteMode === "partial"
                              ? "bg-[var(--accent)] text-[var(--accent-text)]"
                              : "bg-[var(--neutral-900)] text-[var(--neutral-400)]"
                          }`}
                        >
                          Partial
                        </button>
                      </div>
                      <button
                        onClick={() => handlePromote(selected.id, targetStage)}
                        disabled={promoting}
                        className="flex items-center gap-1.5 text-xs border border-[var(--neutral-700)] text-[var(--neutral-200)] rounded-lg px-3 py-1.5 font-medium disabled:opacity-50"
                      >
                        {promoting ? <Loader2 size={13} className="animate-spin" /> : <ArrowUpRight size={13} />}
                        {promoteMode === "partial" ? "Add to" : "Promote to"} {PROMOTE_LABELS[targetStage]} →
                      </button>
                    </>
                  );
                })()}
              </div>
            </div>
            {promoteError && <p className="text-xs text-red-400">{promoteError}</p>}

            {features.length === 0 ? (
              <p className="text-cyber-dim text-sm">
                No plan yet for this project. Once a coding-domain build cycle runs, its features
                and status will show up here.
              </p>
            ) : (
              <>
                <div className="grid gap-4 sm:grid-cols-3">
                  {byColumn.map((col) => (
                    <Card key={col.status} title={`${col.label} (${col.features.length})`}>
                      <div className="space-y-2">
                        {col.features.length === 0 && (
                          <p className="text-[11px] text-cyber-dim">Nothing here.</p>
                        )}
                        {col.features.map((name) => (
                          <FeatureCard
                            key={name}
                            name={name}
                            isTarget={name === targetFeature}
                            targetModules={name === targetFeature ? targetModules : null}
                            expanded={expandedFeature === name}
                            onToggle={() => setExpandedFeature(expandedFeature === name ? null : name)}
                          />
                        ))}
                      </div>
                    </Card>
                  ))}
                </div>
                <IntegrationChecklist integrations={data?.integrations} />
                <div className="grid gap-4 sm:grid-cols-2">
                  <DeployPanel
                    sessionId={resolvedSessionId}
                    apiUrl={API_URL}
                    deployConfigPlan={data?.deploy_config_plan}
                    lastDeployConfigSummary={data?.last_deploy_config_summary}
                    onRefresh={refresh}
                  />
                  <MonitoringWidget
                    sessionId={resolvedSessionId}
                    apiUrl={API_URL}
                    monitoring={data?.monitoring}
                    onRefresh={refresh}
                  />
                </div>
              </>
            )}
          </div>
        )}
      </div>

      {/* CHANGED — step 3e: was rendered bare (legacy mode, reading the
          global SessionContext sessionId regardless of which build
          project was selected above). Now passes this tab's own
          `selected` project so the dock resolves ws:${selected.id} and
          shows/updates the right project's chat, same fix already
          applied to NotebooksTab. BuildTab never called switchChat
          itself, so no other change was needed here. */}
      <div className="hidden lg:flex shrink-0 border-l border-[var(--neutral-800)]" style={{ width: chatDockCollapsed ? undefined : 560 }}>
        <WorkspaceChatPanel collapsed={chatDockCollapsed} onToggleCollapse={toggleChatDock} workspaceId={selected?.id} />
      </div>
      {!chatDockCollapsed && (
        <div className="lg:hidden fixed inset-0 z-40 bg-[var(--neutral-950)]">
          <WorkspaceChatPanel collapsed={false} onToggleCollapse={toggleChatDock} workspaceId={selected?.id} />
        </div>
      )}
      {chatDockCollapsed && (
        <button
          onClick={toggleChatDock}
          title="Open chat"
          className="lg:hidden fixed bottom-4 right-4 z-40 bg-[var(--cyber-amber)] text-black rounded-full p-3 shadow-lg"
        >
          <MessageSquare size={18} />
        </button>
      )}
    </div>
  );
}
