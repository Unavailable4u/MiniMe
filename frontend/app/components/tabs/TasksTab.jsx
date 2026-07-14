"use client";
import { useEffect, useState } from "react";
import { useSession, authHeaders } from "../../context/SessionContext";
// Part 8.9: replaces the old static NEXT_PUBLIC_API_KEY/x-api-key header
// -- every fetch() below now sends the real per-user Supabase JWT via
// authHeaders(), matching require_auth()'s Authorization: Bearer check.

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

export default function TasksTab() {
  const { sessionId, API_URL } = useSession();
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [expandedFeature, setExpandedFeature] = useState(null);

  async function refresh() {
    if (!sessionId) return;
    const res = await fetch(`${API_URL}/api/tasks/${sessionId}`, {
      headers: await authHeaders(),
    });
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
    const d = await res.json();
    setData(d);
    return d;
  }

  useEffect(() => {
    if (!sessionId) {
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
  }, [API_URL, sessionId]);

  if (loading) {
    return (
      <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto">
        <p className="text-xs text-cyber-dim">Loading board...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto">
        <p className="text-xs text-rose-400">
          Couldn't load the task board: {error}. Check that{" "}
          <code className="font-mono">GET /api/tasks/{"{session_id}"}</code> is reachable and that
          you're signed in with a valid session.
        </p>
      </div>
    );
  }

  const features = data?.current_plan?.features || [];
  const featureStatus = data?.feature_status || {};
  const targetFeature = data?.current_plan?.target_feature || null;
  const targetModules = data?.module_specs?.modules || null;

  if (features.length === 0) {
    return (
      <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto">
        <p className="text-cyber-dim text-sm">
          No plan yet for this session. Once a coding-domain build cycle runs,
          its features and status will show up here.
        </p>
      </div>
    );
  }

  const byColumn = COLUMNS.map((col) => ({
    ...col,
    features: features.filter((f) => statusFor(featureStatus, f) === col.status),
  }));

  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto space-y-4">
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
          sessionId={sessionId}
          apiUrl={API_URL}
          deployConfigPlan={data?.deploy_config_plan}
          lastDeployConfigSummary={data?.last_deploy_config_summary}
          onRefresh={refresh}
        />
        <MonitoringWidget
          sessionId={sessionId}
          apiUrl={API_URL}
          monitoring={data?.monitoring}
          onRefresh={refresh}
        />
      </div>
    </div>
  );
}