"use client";
import { useMemo, useState, useEffect } from "react";
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid } from "recharts";
import { useSession, authHeaders } from "../../context/SessionContext";

const GRAFANA_QUOTA_URL = process.env.NEXT_PUBLIC_GRAFANA_QUOTA_URL || null;

// Kept as distinct, saturated per-provider colors on purpose — these
// identify DATA series in charts, not chrome, so they stay separate from
// the cyan/magenta UI accent palette rather than being folded into it.
const PROVIDER_COLOR = {
  groq: "#f97316",
  cerebras: "#3b82f6",
  cloudflare: "#fb923c",
  mistral: "#ef4444",
  github: "#a1a1aa",
  huggingface: "#eab308",
};
const colorFor = (provider) => PROVIDER_COLOR[provider] || "#a78bfa";

const TOOLTIP_STYLE = {
  background: "#0a0f1a",
  border: "1px solid #1a2740",
  borderRadius: 6,
  fontSize: 11,
  fontFamily: "'Share Tech Mono', monospace",
  color: "#d6e4f0",
};

const DAY_RANGES = [7, 14, 30];

// Best-effort provider guess from an agent_key's naming convention
// (e.g. "EO_INSPECTOR_GROQ_KEY_1" -> "groq"), purely for a color accent
// in the list below — get_quota_snapshot()'s response deliberately
// doesn't include a provider field per-entry, so this is display-only
// and never used for any actual grouping/calculation.
function guessProvider(agentKey) {
  const upper = (agentKey || "").toUpperCase();
  return Object.keys(PROVIDER_COLOR).find((p) => upper.includes(p.toUpperCase())) || null;
}

// The real thing quota_sentinel.py computes and no UI ever showed
// (§3 of the audit): TODAY's usage per account, cross-session,
// verified against utils/llm_client.py's QUOTA_CONFIG — not the
// client-side, session-only, guessed-limit numbers OverallRollup below
// shows. Kept as a clearly separate, clearly labeled panel rather than
// merged into OverallRollup, so "today, verified, everyone" is never
// confused with "this session, live, on this device".
function QuotaPanel({ apiUrl }) {
  const [snapshot, setSnapshot] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [refreshedAt, setRefreshedAt] = useState(null);

  function load() {
    setLoading(true);
    setError(null);
    authHeaders()
      .then((headers) => fetch(`${apiUrl}/api/quota`, { headers }))
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then((data) => {
        setSnapshot(data);
        setRefreshedAt(new Date());
      })
      .catch((err) => setError(err.message))
      .finally(() => setLoading(false));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiUrl]);

  const rows = useMemo(() => {
    if (!snapshot) return [];
    return Object.entries(snapshot)
      .map(([agentKey, info]) => ({ agentKey, provider: guessProvider(agentKey), ...info }))
      .sort((a, b) => (b.pct ?? -1) - (a.pct ?? -1));
  }, [snapshot]);

  const anyNearLimit = rows.some((r) => r.pct !== null && r.pct >= 0.8);

  return (
    <Card
      title="Quota (today, verified, cross-session)"
      action={
        <button
          onClick={load}
          disabled={loading}
          className="font-display text-[10px] uppercase tracking-wide rounded px-2 py-1 border border-cyber-border text-cyber-dim hover:text-cyber-text disabled:opacity-50"
        >
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      }
    >
      {error && (
        <p className="text-xs text-rose-400">
          Couldn't load quota: {error}. Check that <code className="font-mono">GET /api/quota</code> is reachable.
        </p>
      )}
      {!error && loading && !snapshot && <p className="text-xs text-cyber-dim">Loading…</p>}
      {!error && !loading && rows.length === 0 && (
        <p className="text-xs text-cyber-dim">No accounts configured.</p>
      )}
      {!error && rows.length > 0 && (
        <>
          {anyNearLimit && (
            <p className="text-[11px] text-amber-500 mb-2">
              One or more accounts are at 80%+ of today's quota.
            </p>
          )}
          <div className="space-y-1.5">
            {rows.map((r) => {
              const near = r.pct !== null && r.pct >= 0.8;
              const pctDisplay = r.pct !== null ? Math.min(100, Math.round(r.pct * 100)) : null;
              return (
                <div key={r.agentKey} className="text-[11px] font-mono">
                  <div className="flex items-center justify-between gap-2">
                    <span className="flex items-center gap-1.5 min-w-0">
                      {r.provider && (
                        <span
                          className="w-1.5 h-1.5 rounded-full shrink-0"
                          style={{ background: colorFor(r.provider) }}
                        />
                      )}
                      <span className="truncate text-cyber-text">{r.agentKey}</span>
                    </span>
                    <span className={`shrink-0 ${near ? "text-amber-500" : "text-cyber-dim"}`}>
                      {r.used.toLocaleString()}
                      {r.quota ? ` / ${r.quota.toLocaleString()} (${pctDisplay}%)` : " tokens — no verified limit"}
                    </span>
                  </div>
                  {r.quota && (
                    <div className="h-1 rounded-full bg-black/50 border border-cyber-border overflow-hidden mt-0.5">
                      <div
                        className={`h-full rounded-full transition-all ${near ? "bg-amber-500" : "bg-cyber-cyan"}`}
                        style={{ width: `${pctDisplay}%` }}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {refreshedAt && (
            <p className="text-[10px] text-cyber-dim mt-2">
              Updated {refreshedAt.toLocaleTimeString()}
            </p>
          )}
        </>
      )}
    </Card>
  );
}


function UsageHistoryPanel({ apiUrl }) {
  const [days, setDays] = useState(7);
  const [historyData, setHistoryData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    authHeaders().then((headers) =>
      fetch(`${apiUrl}/api/usage/history?days=${days}`, { headers })
    )
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then((data) => {
        if (!cancelled) setHistoryData(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [apiUrl, days]);

  const chartRows = useMemo(() => {
    if (!historyData) return [];
    const { dates, providers } = historyData;
    return dates.map((d, i) => {
      const row = { date: d };
      for (const provider of Object.keys(providers)) {
        row[provider] = providers[provider].tokens[i];
      }
      return row;
    });
  }, [historyData]);

  const providerNames = historyData ? Object.keys(historyData.providers).sort() : [];

  return (
    <Card
      title="Usage history (cross-session)"
      action={
        <div className="flex gap-1">
          {DAY_RANGES.map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`font-display text-[10px] uppercase tracking-wide rounded px-2 py-1 border transition-colors ${
                days === d
                  ? "bg-cyber-cyan/10 border-cyber-cyan text-cyber-cyan"
                  : "border-cyber-border text-cyber-dim hover:text-cyber-text"
              }`}
            >
              {d}d
            </button>
          ))}
        </div>
      }
    >
      {loading && <p className="text-xs text-cyber-dim">Loading history...</p>}
      {error && (
        <p className="text-xs text-rose-400">
          Couldn't load usage history: {error}. Check that <code className="font-mono">GET /api/usage/history</code> is
          reachable and that you're signed in with a valid session.
        </p>
      )}
      {!loading && !error && providerNames.length === 0 && (
        <p className="text-xs text-cyber-dim">No usage recorded in this window yet.</p>
      )}
      {!loading && !error && providerNames.length > 0 && (
        <>
          <div style={{ height: 220 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chartRows}>
                <CartesianGrid stroke="#1a2740" strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: 10, fill: "#64748b" }}
                  tickFormatter={(d) => d.slice(5)}
                />
                <YAxis tick={{ fontSize: 10, fill: "#64748b" }} />
                <Tooltip contentStyle={TOOLTIP_STYLE} />
                <Legend wrapperStyle={{ fontSize: 11, fontFamily: "'Share Tech Mono', monospace" }} />
                {providerNames.map((p) => (
                  <Bar key={p} dataKey={p} fill={colorFor(p)} radius={[2, 2, 0, 0]} />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 mt-3 pt-2 border-t border-cyber-border">
            {providerNames.map((p) => (
              <div key={p} className="text-[11px] font-mono">
                <span style={{ color: colorFor(p) }} className="font-display uppercase tracking-wide text-[10px]">
                  {p}
                </span>
                <div className="text-cyber-dim">
                  avg {historyData.providers[p].avg_tokens_per_day.toLocaleString()} tok/day
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </Card>
  );
}

// Part 2 §2.6 -- known domain buckets, mirroring eo/structure.py's
// STRUCTURE_TEMPLATES keys. Hardcoded rather than fetched, since there's
// no existing "list domains" endpoint; update this list if
// STRUCTURE_TEMPLATES gains a new domain. A task can also carry no
// domain at all (Panel classified it as null) -- that traffic simply
// won't show up under any of these, which is correct, not a bug.
const KNOWN_DOMAINS = ["coding", "creative_writing", "research", "data_analysis", "simulate"];

function ProjectSectionUsagePanel({ apiUrl }) {
  const [days, setDays] = useState(7);
  const [domain, setDomain] = useState("");
  const [workspaceId, setWorkspaceId] = useState("");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!domain && !workspaceId) {
      setData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setError(null);
    const params = new URLSearchParams({ days: String(days) });
    if (domain) params.set("domain", domain);
    if (workspaceId) params.set("workspace_id", workspaceId);
    authHeaders().then((headers) =>
      fetch(`${apiUrl}/api/usage/history?${params.toString()}`, { headers })
    )
      .then((res) => {
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        return res.json();
      })
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((err) => {
        if (!cancelled) setError(err.message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => { cancelled = true; };
  }, [apiUrl, days, domain, workspaceId]);

  // Expects GET /api/usage/history to also accept ?domain=&workspace_id=
  // and, when either is present, call
  // eo.quota_sentinel.get_usage_history_scoped() instead of
  // get_usage_history() -- returning {dates, domain, workspace} rather
  // than {dates, providers, accounts}. Same route, shape depends on
  // query params, same way this endpoint's existing ?days= param already
  // changes its window without becoming a new route.
  const rows = useMemo(() => {
    if (!data) return [];
    return data.dates.map((d, i) => ({
      date: d,
      domain: data.domain ? data.domain.tokens[i] : undefined,
      workspace: data.workspace ? data.workspace.tokens[i] : undefined,
    }));
  }, [data]);

  return (
    <Card
      title="Usage by project / section"
      action={
        <div className="flex gap-1">
          {DAY_RANGES.map((d) => (
            <button
              key={d}
              onClick={() => setDays(d)}
              className={`font-display text-[10px] uppercase tracking-wide rounded px-2 py-1 border transition-colors ${
                days === d
                  ? "bg-cyber-cyan/10 border-cyber-cyan text-cyber-cyan"
                  : "border-cyber-border text-cyber-dim hover:text-cyber-text"
              }`}
            >
              {d}d
            </button>
          ))}
        </div>
      }
    >
      <div className="flex flex-wrap gap-2 mb-3">
        <select
          value={domain}
          onChange={(e) => setDomain(e.target.value)}
          className="bg-black/40 border border-cyber-border rounded px-2 py-1 text-xs text-cyber-text"
        >
          <option value="">All domains</option>
          {KNOWN_DOMAINS.map((d) => (
            <option key={d} value={d}>{d}</option>
          ))}
        </select>
        <input
          value={workspaceId}
          onChange={(e) => setWorkspaceId(e.target.value)}
          placeholder="workspace_id (optional)"
          className="bg-black/40 border border-cyber-border rounded px-2 py-1 text-xs text-cyber-text flex-1 min-w-[140px]"
        />
      </div>
      {!domain && !workspaceId && (
        <p className="text-xs text-cyber-dim">
          Pick a domain and/or paste a project's workspace_id to see its usage breakdown.
        </p>
      )}
      {loading && <p className="text-xs text-cyber-dim">Loading...</p>}
      {error && (
        <p className="text-xs text-rose-400">
          Couldn't load: {error}. Make sure <code className="font-mono">GET /api/usage/history</code> accepts{" "}
          <code className="font-mono">domain</code> / <code className="font-mono">workspace_id</code> query params.
        </p>
      )}
      {!loading && !error && data && !data.domain && !data.workspace && (
        <p className="text-xs text-cyber-dim">No usage recorded for that scope in this window yet.</p>
      )}
      {!loading && !error && data && (data.domain || data.workspace) && (
        <div style={{ height: 180 }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={rows}>
              <CartesianGrid stroke="#1a2740" strokeDasharray="3 3" />
              <XAxis dataKey="date" tick={{ fontSize: 10, fill: "#64748b" }} tickFormatter={(d) => d.slice(5)} />
              <YAxis tick={{ fontSize: 10, fill: "#64748b" }} />
              <Tooltip contentStyle={TOOLTIP_STYLE} />
              <Legend wrapperStyle={{ fontSize: 11, fontFamily: "'Share Tech Mono', monospace" }} />
              {data.domain && <Bar dataKey="domain" name={domain || "domain"} fill="#22d3ee" radius={[2, 2, 0, 0]} />}
              {data.workspace && <Bar dataKey="workspace" name="workspace" fill="#a78bfa" radius={[2, 2, 0, 0]} />}
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  );
}

export default function TokenUsageTab() {
  const { usageStats, usageHistory, combinedUsageHistory, API_URL } = useSession();

  const byProvider = useMemo(() => groupByProvider(usageStats), [usageStats]);
  const providers = Object.keys(byProvider).sort();

  if (providers.length === 0) {
    return (
      <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto space-y-4">
        <p className="text-cyber-dim text-sm">
          No usage events yet this session. Send a task from Chat — accounts
          light up here as soon as they make their first call today.
        </p>
        <QuotaPanel apiUrl={API_URL} />
        <UsageHistoryPanel apiUrl={API_URL} />
        <ProjectSectionUsagePanel apiUrl={API_URL} />
        <GrafanaQuotaPanel url={GRAFANA_QUOTA_URL} />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto space-y-6">
      <QuotaPanel apiUrl={API_URL} />
      <OverallRollup byProvider={byProvider} />
      {combinedUsageHistory.length > 0 && (
        <Card title="All providers, this session">
          <CombinedChart data={combinedUsageHistory} providers={providers} />
        </Card>
      )}
      <div className="grid gap-4 sm:grid-cols-2">
        {providers.map((provider) => (
          <ProviderCard
            key={provider}
            provider={provider}
            keys={byProvider[provider]}
            history={usageHistory}
          />
        ))}
      </div>
      <UsageHistoryPanel apiUrl={API_URL} />
      <ProjectSectionUsagePanel apiUrl={API_URL} />
      <GrafanaQuotaPanel url={GRAFANA_QUOTA_URL} />
    </div>
  );
}

function groupByProvider(usageStats) {
  const byProvider = {};
  for (const [statKey, s] of Object.entries(usageStats)) {
    const provider = s.provider || statKey.split(":")[0];
    (byProvider[provider] ||= []).push({ statKey, ...s });
  }
  return byProvider;
}

function OverallRollup({ byProvider }) {
  let totalUsed = 0;
  let totalLimit = 0;   // only sums keys that HAVE a numeric daily_limit — see footnote below
  let unmeasuredProviders = [];
  for (const [provider, keys] of Object.entries(byProvider)) {
    const providerUsed = keys.reduce((s, k) => s + (k.tokens_used_today || 0), 0);
    totalUsed += providerUsed;
    const limited = keys.filter((k) => k.daily_limit);
    if (limited.length > 0) {
      totalLimit += limited.reduce((s, k) => s + k.daily_limit, 0);
    } else {
      unmeasuredProviders.push(provider);
    }
  }
  const pct = totalLimit ? Math.min(100, Math.round((totalUsed / totalLimit) * 100)) : null;
  return (
    <Card title="This session, live (estimated)">
      <div className="flex items-baseline justify-between">
        <span className="font-display text-2xl text-cyber-cyan cyber-glow-text">{totalUsed.toLocaleString()}</span>
        <span className="text-xs text-cyber-dim">
          tokens{totalLimit ? ` / ~${totalLimit.toLocaleString()} est. capacity` : ""}
        </span>
      </div>
      {pct !== null && (
        <div className="h-1.5 rounded-full bg-black/50 border border-cyber-border overflow-hidden mt-2">
          <div
            className={`h-full rounded-full transition-all ${
              pct >= 80 ? "bg-amber-500 shadow-[0_0_8px_rgba(245,158,11,0.6)]" : "bg-cyber-cyan shadow-glow-cyan"
            }`}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {unmeasuredProviders.length > 0 && (
        <p className="text-[11px] text-cyber-dim mt-2">
          This session's estimate excludes {unmeasuredProviders.join(", ")} — no verified
          daily limit configured for {unmeasuredProviders.length === 1 ? "it" : "them"} yet
          (see <code className="font-mono">utils/llm_client.py</code>'s <code className="font-mono">QUOTA_CONFIG</code>).
          For real cross-session numbers, see the Quota panel above.
        </p>
      )}
    </Card>
  );
}

function ProviderCard({ provider, keys, history }) {
  const color = colorFor(provider);
  const used = keys.reduce((s, k) => s + (k.tokens_used_today || 0), 0);
  const limit = keys[0]?.daily_limit; // same static estimate for every key of a given provider
  const pct = limit ? Math.min(100, Math.round((used / (limit * keys.length)) * 100)) : null;
  const providerHistory = useMemo(() => mergeProviderHistory(keys, history), [keys, history]);
  return (
    <div className="cyber-panel p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="font-display text-xs uppercase tracking-wide" style={{ color }}>{provider}</span>
        <span className="text-xs text-cyber-dim">
          {used.toLocaleString()}{limit ? ` / ~${(limit * keys.length).toLocaleString()}` : ""} tokens today
        </span>
      </div>
      {providerHistory.length > 1 && (
        <div style={{ height: 90 }}>
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={providerHistory}>
              <Area type="monotone" dataKey="tokens" stroke={color} fill={color} fillOpacity={0.15} strokeWidth={1.5} isAnimationActive={false} />
              <XAxis dataKey="t" hide />
              <YAxis hide domain={[0, "auto"]} />
              <Tooltip
                contentStyle={TOOLTIP_STYLE}
                labelFormatter={(t) => new Date(t).toLocaleTimeString()}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
      <div className="space-y-1 pt-1 border-t border-cyber-border">
        {keys.map((k) => {
          const keyPct = limit ? Math.min(100, Math.round((k.tokens_used_today / limit) * 100)) : null;
          const near = keyPct !== null && keyPct >= 80;
          return (
            <div key={k.statKey} className="text-[11px] text-cyber-dim flex items-center justify-between font-mono">
              <span>{k.key_id}</span>
              <span className={near ? "text-amber-500" : ""}>
                {(k.tokens_used_today || 0).toLocaleString()}
                {limit ? ` / ${limit.toLocaleString()}` : ""}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function mergeProviderHistory(keys, history) {
  // Combine every key of this provider's own per-key history into one
  // provider-level series, summing whichever keys have a sample at each
  // timestamp seen. Simpler than a true resample/forward-fill (fine for
  // a live sparkline over one session; revisit if a provider with many
  // concurrently-active keys makes this look choppy).
  const rows = keys.flatMap((k) => (history[k.statKey] || []).map((p) => ({ ...p, statKey: k.statKey })));
  rows.sort((a, b) => a.t - b.t);
  return rows;
}

function CombinedChart({ data, providers }) {
  return (
    <div style={{ height: 200 }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data}>
          <CartesianGrid stroke="#1a2740" strokeDasharray="3 3" />
          <XAxis dataKey="t" tickFormatter={(t) => new Date(t).toLocaleTimeString()} tick={{ fontSize: 10, fill: "#64748b" }} />
          <YAxis tick={{ fontSize: 10, fill: "#64748b" }} />
          <Tooltip
            contentStyle={TOOLTIP_STYLE}
            labelFormatter={(t) => new Date(t).toLocaleTimeString()}
          />
          {providers.map((p) => (
            <Area
              key={p}
              type="monotone"
              dataKey={p}
              stackId="1"
              stroke={colorFor(p)}
              fill={colorFor(p)}
              fillOpacity={0.25}
              isAnimationActive={false}
            />
          ))}
        </AreaChart>
      </ResponsiveContainer>
    </div>
  );
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

function GrafanaQuotaPanel({ url }) {
  // Unchanged from Part 16 — this is the one thing here that DOES show
  // real cross-session, persisted usage (Part 4 §5.1's Grafana embed
  // reading eo/quota_sentinel.py's actual stored data), complementing
  // rather than duplicating the live-only charts above.
  if (!url) return null;
  return (
    <Card title="Grafana (cross-session)">
      <iframe
        src={url}
        width="100%"
        height="1280"
        style={{ minHeight: 600, border: "none" }}
        frameBorder="0"
        title="Grafana quota dashboard"
      />
    </Card>
  );
}