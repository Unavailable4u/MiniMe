"use client";
// frontend/app/context/UsageStatsContext.jsx
//
// Audit §2.1 / Item 2 (remaining work), slice 2 of the "split
// SessionContext into concern-based contexts" fix (slice 1 was
// NotificationsContext.jsx — see that file's header comment).
//
// Extracted verbatim from SessionContext.jsx's Part 17 usage-tracking
// code: `usageStats`/`usageHistory`/`combinedUsageHistory`, the
// `latestByProviderRef` bookkeeping ref, and `handleUsageEvent` itself.
// Branch bodies are byte-for-byte unchanged from the original — only the
// call sites moved.
//
// UNLIKE slice 1, this state isn't fed by its own standalone Pusher
// subscription — `handleUsageEvent` is invoked from two other places
// that keep ownership of their own subscriptions:
//   - SessionContext.jsx's session-${sessionId} bind_global handler,
//     on the "usage_update" and "quota_alert" branches (unchanged call
//     sites, now calling into this context via useUsageStats() instead
//     of a same-component useCallback).
//   - WorkspaceDockContext.jsx's per-dock handleDockEvent, via the
//     `onUsageEvent` prop threaded in from AppShell's WorkspaceDockBridge
//     (see that file's own header comment on "usage-event ownership,
//     option 1" — unchanged by this slice, just re-sourced from
//     useUsageStats() instead of useSession()).
// That means UsageStatsProvider has to sit ABOVE SessionProvider in the
// tree (not just alongside it, the way NotificationsProvider does) so
// SessionContext can call useUsageStats() internally. See AppShell.jsx.
//
// A usage_update tick now only re-renders whoever calls useUsageStats()
// (today: TokenUsageTab) instead of every useSession() consumer
// app-wide — this was explicitly called out in §2.1 as one of the
// three example ticks that used to re-render the whole tree.
import { createContext, useContext, useState, useRef, useCallback, useMemo } from "react";

const UsageStatsContext = createContext(null);

export function useUsageStats() {
  const ctx = useContext(UsageStatsContext);
  if (!ctx) throw new Error("useUsageStats() must be used inside <UsageStatsProvider>");
  return ctx;
}

export function UsageStatsProvider({ children }) {
  const [usageStats, setUsageStats] = useState({});
  const [usageHistory, setUsageHistory] = useState({});       // { [statKey]: [{t, tokens}, ...] } — Part 17
  const [combinedUsageHistory, setCombinedUsageHistory] = useState([]); // [{t, [provider]: tokens}, ...] — Part 17
  const latestByProviderRef = useRef({});                       // provider -> summed tokens across its keys, for the combined chart — Part 17

  // NEW — 3e usage-event ownership (architecture doc §2.3's three
  // options; going with option 1): called from both SessionContext's
  // own session-${sessionId} handler AND WorkspaceDockContext's per-dock
  // handler (via the threaded-in onUsageEvent callback), rather than
  // duplicated or left unhandled on either path. Pure extraction from
  // SessionContext.jsx — the branch bodies are unchanged, only the
  // definition site moved from a useCallback in SessionProvider to one
  // here.
  const handleUsageEvent = useCallback((eventType, payload) => {
    if (eventType === "usage_update") {
      const statKey = `${payload?.provider}:${payload?.key_id}`;
      setUsageStats((prev) => ({ ...prev, [statKey]: payload }));

      // Part 17: append to this key's own history (capped so a very
      // long session doesn't grow this unbounded).
      setUsageHistory((prev) => {
        const series = prev[statKey] || [];
        const next = [...series, { t: Date.now(), tokens: payload?.tokens_used_today ?? 0 }];
        return { ...prev, [statKey]: next.length > 300 ? next.slice(-300) : next };
      });

      // Part 17: maintain a per-provider running total (summed across
      // every key seen so far for that provider) and append one row to
      // a combined, time-aligned series every update, forward-filling
      // every OTHER provider's last known value so the combined chart
      // has a real value for every provider at every timestamp, not
      // just the one that happened to fire this particular event.
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
    if (eventType === "quota_alert") {
      console.warn("quota_alert:", payload);
      return;
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const value = useMemo(
    () => ({ usageStats, usageHistory, combinedUsageHistory, handleUsageEvent }),
    [usageStats, usageHistory, combinedUsageHistory, handleUsageEvent]
  );

  return (
    <UsageStatsContext.Provider value={value}>
      {children}
    </UsageStatsContext.Provider>
  );
}
