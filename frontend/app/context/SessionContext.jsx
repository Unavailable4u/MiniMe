"use client";
import { createContext, useContext, useState, useRef, useEffect } from "react";
import Pusher from "pusher-js";

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

const SessionContext = createContext(null);

export function useSession() {
  const ctx = useContext(SessionContext);
  if (!ctx) throw new Error("useSession() must be used inside <SessionProvider>");
  return ctx;
}

export function SessionProvider({ children }) {
  const [messages, setMessages] = useState([]);
  const [loading, setLoading] = useState(false);
  const [sessionId] = useState(() => {
    if (typeof crypto !== "undefined" && crypto.randomUUID) {
      return `sess_${crypto.randomUUID()}`;
    }
    return `sess_${Date.now()}_${Math.random().toString(36).slice(2)}`;
  });
  const [liveDecision, setLiveDecision] = useState(null);
  // CHANGE — Part 18: liveLanes (object keyed by module name) replaced
  // with liveSteps (an ordered array). Two reasons, both found reading
  // eo/executor.py directly:
  //   1. agent_start/agent_done are keyed by the RESOLVED MODULE name
  //      (e.g. "generic_worker"), not the role. Multiple hired roles
  //      routinely share the same module, so an object keyed by module
  //      name silently overwrote one role's lane with another's.
  //   2. Object state also meant a finished message could never show
  //      its own trace afterward — liveLanes just got reset to {} on
  //      the next sendTask() and was never attached to the message.
  // Array order is safe to rely on here because eo/executor.py's
  // execute_graph() is a strictly sequential `while` loop (confirmed
  // reading it directly) — no two agent_start events are ever "in
  // flight" at once, so "operate on the most recently pushed entry" is
  // always correct and never ambiguous.
  const [liveSteps, setLiveSteps] = useState([]);
  const stepsRef = useRef([]);           // NEW — Part 18: mirrors liveSteps synchronously
  const stepSeq = useRef(0);             // NEW — Part 18: unique id per step, for React keys
  const [usageStats, setUsageStats] = useState({});
  const [usageHistory, setUsageHistory] = useState({});       // { [statKey]: [{t, tokens}, ...] } — Part 17
  const [combinedUsageHistory, setCombinedUsageHistory] = useState([]); // [{t, [provider]: tokens}, ...] — Part 17
  const latestByProviderRef = useRef({});                       // provider -> summed tokens across its keys, for the combined chart — Part 17
  const [routeTrace, setRouteTrace] = useState([]);
  const [macroLoopDecisions, setMacroLoopDecisions] = useState([]);
  const [dependencyMap, setDependencyMap] = useState({});
  const [structurePlan, setStructurePlan] = useState(null);
  const [mode, setMode] = useState("auto");
  const [pusherConnected, setPusherConnected] = useState(false); // NEW — Settings tab diagnostic, §6

  // --- Pusher subscription: identical to today's page.js effect, just
  // living up here instead of inside the page that used to render
  // everything. This is the fix described in §1 — this effect now only
  // ever mounts/unmounts with the whole app, never with a tab switch.
  useEffect(() => {
    const key = process.env.NEXT_PUBLIC_PUSHER_KEY;
    const cluster = process.env.NEXT_PUBLIC_PUSHER_CLUSTER;
    if (!key || !cluster) {
      console.warn("Pusher env vars not set — live agent events disabled.");
      return;
    }
    const pusher = new Pusher(key, { cluster });
    setPusherConnected(true);
    const channelName = `session-${sessionId.replace(/[^A-Za-z0-9_=@,.;-]/g, "-")}`;
    const channel = pusher.subscribe(channelName);
    channel.bind_global((eventType, data) => {
      const { agent, payload } = data;
      if (eventType === "routing_decision") {
        setLiveDecision(payload);
        return;
      }
      if (eventType === "usage_update") {
        const statKey = `${payload?.provider}:${payload?.key_id}`;
        setUsageStats((prev) => ({ ...prev, [statKey]: payload }));

        // NEW — Part 17: append to this key's own history (capped so a
        // very long session doesn't grow this unbounded).
        setUsageHistory((prev) => {
          const series = prev[statKey] || [];
          const next = [...series, { t: Date.now(), tokens: payload?.tokens_used_today ?? 0 }];
          return { ...prev, [statKey]: next.length > 300 ? next.slice(-300) : next };
        });

        // NEW — Part 17: maintain a per-provider running total (summed
        // across every key seen so far for that provider) and append one
        // row to a combined, time-aligned series every update, forward-
        // filling every OTHER provider's last known value so the combined
        // chart has a real value for every provider at every timestamp,
        // not just the one that happened to fire this particular event.
        const provider = payload?.provider;
        if (provider) {
          // Recompute this provider's total from every key of theirs
          // we've seen so far, rather than a running += — a += would
          // double count if this same key's usage_update fires again
          // with a lower number for any reason (shouldn't happen, but
          // recomputing from source is one fewer thing to trust blindly).
          setUsageStats((prevStats) => {
            const total = Object.entries(prevStats)
              .filter(([k]) => k.startsWith(`${provider}:`))
              .reduce((sum, [, v]) => sum + (v.tokens_used_today || 0), 0)
              + (payload?.tokens_used_today || 0); // this event's own key may not be in prevStats yet
            latestByProviderRef.current = { ...latestByProviderRef.current, [provider]: total };
            return prevStats; // this call is read-only against usageStats — the actual write already happened above
          });
          setCombinedUsageHistory((prev) => {
            const row = { t: Date.now(), ...latestByProviderRef.current };
            const next = [...prev, row];
            return next.length > 300 ? next.slice(-300) : next;
          });
        }
        return;
      }
      if (eventType === "dispatch_event") {
        setRouteTrace((prev) => [...prev, { destination: payload?.destination, reason: payload?.reason }]);
        return;
      }
      if (eventType === "macro_loop_decision") {
        setMacroLoopDecisions((prev) => [
          ...prev,
          { action: payload?.decision, loop: payload?.loop, cause: payload?.cause },
        ]);
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
        console.warn("quota_alert:", payload);
        return;
      }
      // CHANGE — Part 18: agent_start/agent_token_chunk/agent_done/error
      // now push/update against liveSteps (array), not a lanes object.
      if (eventType === "agent_start") {
        const step = {
          id: stepSeq.current++,
          agent,                                   // resolved module name (executor.py's current_name)
          role: payload?.label || agent,            // actual role — payload.label per executor.py's emit_event() call
          text: "",
          summary: null,
          durationMs: null,
          status: "running",
        };
        stepsRef.current = [...stepsRef.current, step];
        setLiveSteps(stepsRef.current);
        return;
      }
      if (eventType === "agent_token_chunk") {
        // §1: not every agent is confirmed to emit this. Steps that
        // never receive a chunk simply fall back to agent_done's
        // summary below.
        if (stepsRef.current.length === 0) return;
        const last = stepsRef.current.length - 1;
        const updated = [...stepsRef.current];
        updated[last] = { ...updated[last], text: updated[last].text + (payload?.text || "") };
        stepsRef.current = updated;
        setLiveSteps(updated);
        return;
      }
      if (eventType === "agent_done") {
        if (stepsRef.current.length === 0) return;
        const last = stepsRef.current.length - 1;
        const updated = [...stepsRef.current];
        updated[last] = {
          ...updated[last],
          status: "done",
          summary: payload?.summary,
          durationMs: payload?.duration_ms,
        };
        stepsRef.current = updated;
        setLiveSteps(updated);
        return;
      }
      if (eventType === "error") {
        if (stepsRef.current.length === 0) return;
        const last = stepsRef.current.length - 1;
        const updated = [...stepsRef.current];
        updated[last] = { ...updated[last], status: "error", summary: payload?.message };
        stepsRef.current = updated;
        setLiveSteps(updated);
      }
    });
    return () => {
      pusher.unsubscribe(channelName);
      pusher.disconnect();
      setPusherConnected(false);
    };
  }, [sessionId]);

  async function sendTask(taskText) {
    setMessages((prev) => [...prev, { role: "user", text: taskText }]);
    setLoading(true);
    setLiveDecision(null);
    stepsRef.current = [];
    setLiveSteps([]);
    setRouteTrace([]);
    setDependencyMap({});
    setStructurePlan(null);
    setMacroLoopDecisions([]);   // NEW — same clean-slate treatment as the others
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
      // NEW — Part 18: snapshot the just-finished run's steps onto the
      // message itself, via the ref (not the `liveSteps` state variable,
      // which would be stale here — this closure captured whatever
      // liveSteps was at the moment sendTask() was called, not the
      // latest value after every event that streamed in since).
      setMessages((prev) => [...prev, { role: "assistant", data, steps: stepsRef.current }]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { role: "assistant", data: { status: "error", message: String(err) }, steps: stepsRef.current },
      ]);
    } finally {
      setLoading(false);
    }
  }

  async function registerProject() {
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
      alert(`Registered as '${data.unique_name}' -> ${data.root_path}`);
    } catch (err) {
      alert(`Registration failed: ${String(err)}`);
    }
  }

  const value = {
  sessionId, API_URL,
  messages, loading,
  liveDecision, liveSteps, usageStats, usageHistory, combinedUsageHistory, routeTrace, dependencyMap, structurePlan,
  macroLoopDecisions,   // NEW
  mode, setMode,
  pusherConnected,
  sendTask, registerProject,
  };
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}
