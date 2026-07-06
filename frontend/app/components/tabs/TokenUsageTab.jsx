"use client";
import { useMemo } from "react";
import { AreaChart, Area, XAxis, YAxis, Tooltip, ResponsiveContainer, CartesianGrid } from "recharts";
import { useSession } from "../../context/SessionContext";

const GRAFANA_QUOTA_URL = process.env.NEXT_PUBLIC_GRAFANA_QUOTA_URL || null;

const PROVIDER_COLOR = {
  groq: "#f97316",
  cerebras: "#3b82f6",
  cloudflare: "#fb923c",
  mistral: "#ef4444",
  github: "#a1a1aa",
  huggingface: "#eab308",
};
const colorFor = (provider) => PROVIDER_COLOR[provider] || "#8b5cf6";

export default function TokenUsageTab() {
  const { usageStats, usageHistory, combinedUsageHistory } = useSession();

  const byProvider = useMemo(() => groupByProvider(usageStats), [usageStats]);
  const providers = Object.keys(byProvider).sort();

  if (providers.length === 0) {
    return (
      <div className="h-full overflow-y-auto px-4 py-6 max-w-4xl mx-auto">
        <p className="text-neutral-500 text-sm">
          No usage events yet this session. Send a task from Chat — accounts
          light up here as soon as they make their first call today.
        </p>
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
        <span className="text-2xl font-medium text-neutral-100">{totalUsed.toLocaleString()}</span>
        <span className="text-xs text-neutral-500">
          tokens{totalLimit ? ` / ~${totalLimit.toLocaleString()} est. capacity` : ""}
        </span>
      </div>
      {pct !== null && (
        <div className="h-1.5 rounded-full bg-neutral-900 overflow-hidden mt-2">
          <div
            className={`h-full rounded-full ${pct >= 80 ? "bg-amber-500" : "bg-neutral-500"}`}
            style={{ width: `${pct}%` }}
          />
        </div>
      )}
      {unmeasuredProviders.length > 0 && (
        <p className="text-[11px] text-neutral-600 mt-2">
          Capacity estimate excludes {unmeasuredProviders.join(", ")} — no verified
          daily limit configured for {unmeasuredProviders.length === 1 ? "it" : "them"} yet
          (see <code>utils/llm_client.py</code>'s <code>QUOTA_CONFIG</code>).
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
    <div className="rounded-lg border border-neutral-800 p-3 space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium" style={{ color }}>{provider}</span>
        <span className="text-xs text-neutral-500">
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
                contentStyle={{ background: "#0a0a0a", border: "1px solid #262626", fontSize: 11 }}
                labelFormatter={(t) => new Date(t).toLocaleTimeString()}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
      <div className="space-y-1 pt-1 border-t border-neutral-800/70">
        {keys.map((k) => {
          const keyPct = limit ? Math.min(100, Math.round((k.tokens_used_today / limit) * 100)) : null;
          const near = keyPct !== null && keyPct >= 80;
          return (
            <div key={k.statKey} className="text-[11px] text-neutral-500 flex items-center justify-between">
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
          <CartesianGrid stroke="#262626" strokeDasharray="3 3" />
          <XAxis dataKey="t" tickFormatter={(t) => new Date(t).toLocaleTimeString()} tick={{ fontSize: 10, fill: "#737373" }} />
          <YAxis tick={{ fontSize: 10, fill: "#737373" }} />
          <Tooltip
            contentStyle={{ background: "#0a0a0a", border: "1px solid #262626", fontSize: 11 }}
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

function Card({ title, children }) {
  return (
    <div className="rounded-lg border border-neutral-800 p-4">
      <h3 className="text-xs text-neutral-500 mb-2">{title}</h3>
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
