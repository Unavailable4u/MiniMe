"use client";
import { useMemo, useState, useEffect } from "react";
import { AreaChart, Area, BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer, CartesianGrid } from "recharts";
import { useSession } from "../../context/SessionContext";

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

// Reads NEXT_PUBLIC_API_KEY if you've set one for the frontend to send as
// x-api-key -- matches server.py's require_auth() header check. If your
// SessionContext.jsx already attaches this header some other way (e.g. a
// value read from context instead of env), swap this constant for that
// instead; this is just the simplest thing that matches server.py as
// written.
const API_KEY = process.env.NEXT_PUBLIC_API_KEY || null;

function UsageHistoryPanel({ apiUrl }) {
  const [days, setDays] = useState(7);
  const [historyData, setHistoryData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fetch(`${apiUrl}/api/usage/history?days=${days}`, {
      headers: API_KEY ? { "x-api-key": API_KEY } : {},
    })
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
          reachable and, if <code className="font-mono">API_AUTH_SECRET</code> is set on the backend, that{" "}
          <code className="font-mono">NEXT_PUBLIC_API_KEY</code> is set to match on the frontend.
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
        <UsageHistoryPanel apiUrl={API_URL} />
        <GrafanaQuotaPanel url={GRAFANA_QUOTA_URL} />
      </div>
    );
  }

  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto space-y-6">
      <OverallRollup byProvider={byProvider} />
      {combinedUsageHistory.length > 1 && (
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
    <Card title="Overall, this session">
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
          Capacity estimate excludes {unmeasuredProviders.join(", ")} — no verified
          daily limit configured for {unmeasuredProviders.length === 1 ? "it" : "them"} yet
          (see <code className="font-mono">utils/llm_client.py</code>'s <code className="font-mono">QUOTA_CONFIG</code>).
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
      <iframe src={url} width="100%" height="300" frameBorder="0" title="Grafana quota dashboard" />
    </Card>
  );
}
