"use client";

import { useState, useRef, useEffect } from "react";
import Pusher from "pusher-js";
import RoutingTraceGraph from "./components/RoutingTraceGraph";
import DependencyGraph from "./components/DependencyGraph";
import StructurePlanDiagram from "./components/StructurePlanDiagram";
const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
// Part 4 step 5.1 -- Grafana public dashboard embed URL. Set in
// frontend/.env.local, e.g.:
//   NEXT_PUBLIC_GRAFANA_QUOTA_URL=http://localhost:3001/public-dashboards/abc123def456
// Left unset-safe (renders nothing) so local dev without Grafana running
// doesn't show a broken iframe.
const GRAFANA_QUOTA_URL = process.env.NEXT_PUBLIC_GRAFANA_QUOTA_URL || null;

export default function ChatPage() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef(null);
  const [sessionId] = useState(() => {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
      return `sess_${crypto.randomUUID()}`;
    }
    return `sess_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  });
  const [liveDecision, setLiveDecision] = useState(null);
  const [liveLanes, setLiveLanes] = useState({});
  const [usageStats, setUsageStats] = useState({});
  const [routeTrace, setRouteTrace] = useState([]);
  const [dependencyMap, setDependencyMap] = useState({});
  const [structurePlan, setStructurePlan] = useState(null);
  const [mode, setMode] = useState("auto");

  useEffect(() => {
    const key = process.env.NEXT_PUBLIC_PUSHER_KEY;
    const cluster = process.env.NEXT_PUBLIC_PUSHER_CLUSTER;
    if (!key || !cluster) {
      console.warn("Pusher env vars not set — live agent events disabled.");
      return;
    }

    const pusher = new Pusher(key, { cluster });
    const channelName = `session-${sessionId.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
    const channel = pusher.subscribe(channelName);

    channel.bind_global((eventType, data) => {
      const { agent, payload } = data;

      if (eventType === "routing_decision") {
        setLiveDecision(payload);
        return;
      }
      if (eventType === "usage_update") {
        // Keyed by provider:key_id so each account's bar tracks
        // independently — Part 6.7's dashboard is per-key, not just
        // per-provider, since separate keys have separate quotas.
        const statKey = `${payload?.provider}:${payload?.key_id}`;
        setUsageStats((prev) => ({ ...prev, [statKey]: payload }));
        return;
      }
      if (eventType === "dispatch_event") {
        setRouteTrace((prev) => [...prev, { destination: payload?.destination, reason: payload?.reason }]);
        return;
      }
      if (eventType === "dependency_map") {
        setDependencyMap(payload?.map || {});
        return;
      }
      if (eventType === "structure_plan") {
        setStructurePlan(payload?.mermaid || null);
        return;
      }
      if (eventType === "quota_alert") {
        // No dedicated state needed -- Grafana (Part 4 §5) is the real
        // display surface for this; log it for now so it's at least
        // visible during dev.
        console.warn("quota_alert:", payload);
        return;
      }
      if (eventType === "agent_start") {
        setLiveLanes((prev) => ({
          ...prev,
          [agent]: { label: payload?.label || agent, text: "", status: "running" },
        }));
        return;
      }
      if (eventType === "agent_token_chunk") {
        setLiveLanes((prev) => {
          const lane = prev[agent];
          if (!lane) return prev; // token chunk with no matching agent_start — ignore rather than crash
          return { ...prev, [agent]: { ...lane, text: lane.text + (payload?.text || "") } };
        });
        return;
      }
      if (eventType === "agent_done") {
        setLiveLanes((prev) => {
          const lane = prev[agent];
          if (!lane) return prev;
          return {
            ...prev,
            [agent]: { ...lane, status: "done", summary: payload?.summary, durationMs: payload?.duration_ms },
          };
        });
        return;
      }
      if (eventType === "error") {
        setLiveLanes((prev) => {
          const lane = prev[agent];
          if (!lane) return prev;
          return { ...prev, [agent]: { ...lane, status: "error", summary: payload?.message } };
        });
      }
    });

    return () => {
      pusher.unsubscribe(channelName);
      pusher.disconnect();
    };
  }, [sessionId]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  async function sendTask(taskText) {
    setMessages((prev) => [...prev, { role: "user", text: taskText }]);
    setLoading(true);
    setLiveDecision(null);
    setLiveLanes({});
    setRouteTrace([]);
    setDependencyMap({});
    setStructurePlan(null);
    try {
      const res = await fetch(`${API_URL}/api/task`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": process.env.NEXT_PUBLIC_API_KEY,
        },
        body: JSON.stringify({ task_text: taskText, session_id: sessionId, mode }),
      });
      const data = await res.json();
      setMessages((prev) => [...prev, { role: "assistant", data }]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", data: { status: "error", message: String(err) } },
      ]);
    } finally {
      setLoading(false);
    }
  }

  async function registerProject() {
    // Migration Part 8 §8.4 — intentionally minimal: a prompt() dialog,
    // not a polished picker modal, so the mechanism (§8.1/§8.2) is
    // actually usable today. A full project-picker UI (blueprint §15)
    // is a larger frontend task than this part's scope.
    const path = prompt("Full path to the project folder:");
    const name = prompt("Display name for this project:");
    if (!path || !name) return;
    try {
      const res = await fetch(`${API_URL}/api/projects`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-api-key": process.env.NEXT_PUBLIC_API_KEY,
        },
        body: JSON.stringify({ path, display_name: name }),
      });
      const data = await res.json();
      if (!res.ok) {
        alert(`Registration failed: ${data.detail || res.status}`);
        return;
      }
      // No project-picker UI yet (see comment above), so surfacing the
      // unique_name directly is the only way to hand it back right now --
      // reference it via project_unique_name on a task (CLI: --project).
      alert(`Registered as '${data.unique_name}' -> ${data.root_path}`);
    } catch (err) {
      alert(`Registration failed: ${String(err)}`);
    }
  }

  function handleSubmit(e) {
    e.preventDefault();
    const text = input.trim();
    if (!text || loading) return;
    setInput("");
    sendTask(text);
  }

  return (
    <div className="flex flex-col h-screen max-w-3xl mx-auto">
      <header className="border-b border-neutral-800 px-4 py-3 flex items-center justify-between">
        <h1 className="text-sm font-medium text-neutral-400">MiniMe</h1>
        <button
          onClick={registerProject}
          className="text-xs text-neutral-500 hover:text-neutral-300 border border-neutral-800 rounded-lg px-2 py-1"
          title="Register an external project folder for cross-project control"
        >
          + Register project
        </button>
      </header>

      <TokenQuotaDashboard stats={usageStats} />
      <GrafanaQuotaPanel url={GRAFANA_QUOTA_URL} />

      <div className="flex-1 overflow-y-auto px-4 py-6 space-y-4">
        {messages.length === 0 && (
          <p className="text-neutral-500 text-sm">
            Send a task — the EO layer will classify it and route it through
            the appropriate tier.
          </p>
        )}
        {messages.map((m, i) => (
          <MessageBubble key={i} message={m} />
        ))}
        {loading && (
          <LiveActivity
            decision={liveDecision}
            lanes={liveLanes}
            routeTrace={routeTrace}
            dependencyMap={dependencyMap}
            structurePlan={structurePlan}
          />
        )}
        <div ref={bottomRef} />
      </div>

      <form onSubmit={handleSubmit} className="border-t border-neutral-800 p-4 flex gap-2">
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value)}
          disabled={loading}
          className="bg-neutral-900 border border-neutral-800 rounded-lg px-2 py-2 text-sm outline-none"
        >
          <option value="auto">Auto</option>
          <option value="simple">Simple</option>
          <option value="fast">Fast</option>
          <option value="expert">Expert</option>
          <option value="beast">Beast</option>
        </select>
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="Describe a task..."
          disabled={loading}
          className="flex-1 bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm outline-none focus:border-neutral-600 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={loading}
          className="bg-neutral-100 text-neutral-900 rounded-lg px-4 py-2 text-sm font-medium disabled:opacity-50"
        >
          Send
        </button>
      </form>
    </div>
  );
}

function GrafanaQuotaPanel({ url }) {
  // Part 4 step 5.1. Complements TokenQuotaDashboard above rather than
  // replacing it: TokenQuotaDashboard shows live, per-task usage_update
  // events over Pusher for the run in progress; this panel shows
  // Grafana's own cross-run view of the real usage data tracked in
  // memory.bus (eo/quota_sentinel.py, since Part 8 §2 -- no longer a
  // separate cache-Redis counter), which persists across sessions and
  // page reloads.
  if (!url) return null;
  return (
    <div className="border-b border-neutral-800 px-4 py-3">
      <iframe
        src={url}
        width="100%"
        height="300"
        frameBorder="0"
        title="Grafana quota dashboard"
      />
    </div>
  );
}

function TokenQuotaDashboard({ stats }) {
  const entries = Object.entries(stats);
  // Nothing to show until the first usage_update arrives -- no point
  // rendering an empty dashboard before any agent has made a call.
  if (entries.length === 0) return null;

  return (
    <div className="border-b border-neutral-800 px-4 py-3 space-y-2">
      {entries.map(([statKey, s]) => {
        const used = s.tokens_used_today ?? 0;
        const limit = s.daily_limit;
        // daily_limit is a rough static estimate (Part 6.7's QUOTA_CONFIG),
        // not every provider is in that table yet -- show the raw count
        // instead of a bar when we don't have a limit to compare against.
        const pct = limit ? Math.min(100, Math.round((used / limit) * 100)) : null;
        const nearLimit = pct !== null && pct >= 80;

        return (
          <div key={statKey} className="text-xs">
            <div className="flex items-center justify-between text-neutral-500 mb-1">
              <span>
                {s.provider} <span className="text-neutral-600">· {s.key_id}</span>
              </span>
              <span className={nearLimit ? "text-amber-500" : ""}>
                {used.toLocaleString()}
                {limit ? ` / ${limit.toLocaleString()} tokens` : " tokens today"}
              </span>
            </div>
            {pct !== null && (
              <div className="h-1.5 rounded-full bg-neutral-900 overflow-hidden">
                <div
                  className={`h-full rounded-full ${nearLimit ? "bg-amber-500" : "bg-neutral-500"}`}
                  style={{ width: `${pct}%` }}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function MessageBubble({ message }) {
  if (message.role === "user") {
    return (
      <div className="flex justify-end">
        <div className="bg-neutral-800 rounded-lg px-3 py-2 text-sm max-w-[80%]">
          {message.text}
        </div>
      </div>
    );
  }

  const { data } = message;
  return (
    <div className="flex justify-start">
      <div className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2 text-sm max-w-[80%] space-y-1">
        <div className="text-xs text-neutral-500">
          tier {data.tier} · {data.status}
        </div>
        <RoutingTraceCard decision={data.decision} />
        <ResultBody data={data} />
      </div>
    </div>
  );
}

function RoutingTraceCard({ decision }) {
  // server.py returns decision={} on a caught server-side error — nothing
  // to show in that case, and empty-object access below would just print
  // "confidence 0.00" noise for no reason.
  if (!decision || !decision.reasoning) return null;

  const isPanel = decision.panel_reviewed && Array.isArray(decision.panel_votes);
  const pct = (c) => (typeof c === "number" ? c.toFixed(2) : "0.00");

  return (
    <details className="rounded-lg border border-neutral-800 bg-neutral-950/50 text-xs">
      <summary className="cursor-pointer select-none px-2 py-1.5 text-neutral-400 hover:text-neutral-300">
        routing trace
        {isPanel && <span className="ml-1 text-amber-500/80">· panel reviewed</span>}
      </summary>

      <div className="space-y-2 border-t border-neutral-800 px-2 pb-2 pt-1.5">
        {isPanel ? (
          <>
            <div className="space-y-1.5">
              {decision.panel_votes.map((v) => (
                <div key={v.member} className="border-l-2 border-neutral-800 pl-2">
                  <div className="text-neutral-500">
                    member {v.member} · tier {v.tier} · confidence {pct(v.confidence)}
                    {v.directed_task_type ? ` · ${v.directed_task_type}` : ""}
                  </div>
                  <div className="text-neutral-400">{v.reasoning}</div>
                </div>
              ))}
            </div>
            <div className="border-t border-neutral-800/70 pt-1.5">
              <div className="text-neutral-500">
                synthesis · tier {decision.tier} (max) · confidence {pct(decision.confidence)} (avg) ·{" "}
                {decision.directed_task_type
                  ? decision.directed_task_type
                  : "directed_task_type: none (members disagreed)"}
              </div>
              {decision.suggested_agents?.length > 0 && (
                <div className="mt-0.5 text-neutral-500">
                  agents: {decision.suggested_agents.join(", ")}
                </div>
              )}
            </div>
          </>
        ) : (
          <>
            <div className="text-neutral-500">
              inspector · tier {decision.tier} · confidence {pct(decision.confidence)}
              {decision.directed_task_type ? ` · ${decision.directed_task_type}` : ""}
            </div>
            <div className="text-neutral-400">{decision.reasoning}</div>
          </>
        )}
      </div>
    </details>
  );
}

function LiveActivity({ decision, lanes, routeTrace, dependencyMap, structurePlan }) {
  const laneList = Object.entries(lanes);

  if (!decision) {
    return (
      <div className="text-neutral-500 text-sm animate-pulse">
        Classifying and routing...
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <RoutingTraceCard decision={decision} />
      {routeTrace.length > 1 && <RoutingTraceGraph trace={routeTrace} />}
      {Object.keys(dependencyMap).length > 0 && <DependencyGraph map={dependencyMap} />}
      {structurePlan && <StructurePlanDiagram mermaidText={structurePlan} />}
      {laneList.map(([agent, lane]) => (
        <div
          key={agent}
          className={`rounded-lg border px-3 py-2 text-xs ${
            lane.status === "error"
              ? "border-red-900 bg-red-950/30"
              : lane.status === "done"
              ? "border-neutral-800 bg-neutral-950/50"
              : "border-neutral-700 bg-neutral-900/50"
          }`}
        >
          <div className="flex items-center justify-between text-neutral-400">
            <span>{lane.label}</span>
            <span className={lane.status === "running" ? "animate-pulse" : ""}>
              {lane.status}
              {lane.durationMs ? ` · ${lane.durationMs}ms` : ""}
            </span>
          </div>
          {lane.status === "running" && lane.text && (
            <pre className="mt-1 whitespace-pre-wrap text-neutral-500 max-h-24 overflow-y-auto">
              {lane.text}
            </pre>
          )}
          {lane.status === "done" && lane.summary && (
            <div className="mt-1 text-neutral-500">{lane.summary}</div>
          )}
          {lane.status === "error" && lane.summary && (
            <div className="mt-1 text-red-400">{lane.summary}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function ResultBody({ data }) {
  if (data.status === "error" || data.message) {
    return <div className="text-red-400">{data.message}</div>;
  }
  if (data.tier === "sga" || data.tier === "cache") {
    return <div>{data.result?.answer}</div>;
  }
  if (data.tier === 0) {
    return <div>{data.result?.answer}</div>;
  }
  if (data.tier === 1) {
    return (
      <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
        {data.result?.code}
      </pre>
    );
  }
  if (data.tier === 2) {
    return (
      <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
        {JSON.stringify(data.result?.output, null, 2)}
      </pre>
    );
  }
  return (
    <pre className="whitespace-pre-wrap text-xs bg-black/40 rounded p-2 overflow-x-auto">
      {JSON.stringify(data, null, 2)}
    </pre>
  );
}