// frontend/app/components/IntegrationsPanel.jsx
// NEW — Part 8.5/8.9: connect/disconnect panel for per-user third-party
// OAuth credentials (eo/integrations.py). Google Calendar is the only
// wired-up provider today; PROVIDERS below is deliberately a small,
// literal list rather than driven off GET /api/integrations, since a
// provider can only be OFFERED here once its OAuth routes exist in
// api/server.py — Gmail/Slack/Jira-Asana-Linear each add one more entry
// once their own connect/callback routes land, no rebuild of this
// component needed (per the original ask: "generic enough to fold in
// Gmail/Slack/Jira later without a rebuild").
"use client";
import { useState, useEffect, useCallback } from "react";
import { Calendar, Check, Loader2 } from "lucide-react";
import { useSession, authHeaders } from "../context/SessionContext";
import CalendarEventsPanel from "./CalendarEventsPanel";

const PROVIDERS = [
  {
    id: "google_calendar",
    label: "Google Calendar",
    icon: Calendar,
    connectPath: "/api/integrations/google_calendar/connect",
  },
  // Future: { id: "gmail", label: "Gmail", ... }, { id: "slack", ... }, etc.
];

export default function IntegrationsPanel() {
  const { API_URL } = useSession();
  const [connected, setConnected] = useState(null); // null = loading
  const [busyProvider, setBusyProvider] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`${API_URL}/api/integrations`, { headers: await authHeaders() });
      if (!res.ok) throw new Error(`Failed to load integrations (${res.status})`);
      setConnected(await res.json());
    } catch (e) {
      setError(String(e.message || e));
      setConnected([]);
    }
  }, [API_URL]);

  useEffect(() => {
    load();
    // The Google OAuth callback (api/server.py's google_calendar_callback)
    // redirects the browser back to /settings/integrations?connected=... —
    // reload the list once more if that param is present, in case this
    // component mounted before the redirect's round-trip finished.
    if (typeof window !== "undefined" && window.location.search.includes("connected=")) {
      const url = new URL(window.location.href);
      url.searchParams.delete("connected");
      window.history.replaceState({}, "", url.toString());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function handleConnect(provider) {
    setBusyProvider(provider.id);
    setError(null);
    try {
      const res = await fetch(`${API_URL}${provider.connectPath}`, { headers: await authHeaders() });
      if (!res.ok) throw new Error(`Failed to start connection (${res.status})`);
      const { auth_url } = await res.json();
      window.location.href = auth_url; // real browser navigation to Google's consent screen
    } catch (e) {
      setError(String(e.message || e));
      setBusyProvider(null);
    }
  }

  async function handleDisconnect(provider) {
    setBusyProvider(provider.id);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/integrations/${provider.id}`, {
        method: "DELETE",
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`Failed to disconnect (${res.status})`);
      await load();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setBusyProvider(null);
    }
  }

  if (connected === null) {
    return <p className="text-xs text-[var(--neutral-600)]">Loading integrations…</p>;
  }

  const byProvider = Object.fromEntries(connected.map((c) => [c.provider, c]));

  return (
    <div className="space-y-2">
      {error && <p className="text-xs text-red-400">{error}</p>}
      {PROVIDERS.map((provider) => {
        const info = byProvider[provider.id];
        const isConnected = !!info;
        const Icon = provider.icon;
        const busy = busyProvider === provider.id;
        return (
          <div key={provider.id} className="space-y-2">
            <div className="flex items-center justify-between border border-[var(--neutral-800)] rounded-lg px-3 py-2">
              <div className="flex items-center gap-2 min-w-0">
                <Icon size={14} className="text-[var(--neutral-500)] shrink-0" />
                <div className="min-w-0">
                  <p className="text-xs text-[var(--neutral-300)]">{provider.label}</p>
                  {isConnected && (
                    <p className="text-[10px] text-[var(--neutral-600)] truncate flex items-center gap-1">
                      <Check size={10} className="text-emerald-500 shrink-0" />
                      {info.account_label || "Connected"}
                    </p>
                  )}
                </div>
              </div>
              <button
                type="button"
                disabled={busy}
                onClick={() => (isConnected ? handleDisconnect(provider) : handleConnect(provider))}
                className={`shrink-0 text-xs rounded-lg px-3 py-1.5 disabled:opacity-50 ${
                  isConnected
                    ? "text-[var(--neutral-500)] hover:text-[var(--neutral-300)] border border-[var(--neutral-800)]"
                    : "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                }`}
              >
                {busy ? <Loader2 size={12} className="animate-spin" /> : isConnected ? "Disconnect" : "Connect"}
              </button>
            </div>
            {/* Direct list/create/delete UI for this provider's events —
                separate from the agent's indirect access via
                agents/calendar_agent.py during task runs. Only shown once
                connected; every endpoint it calls 409s otherwise anyway. */}
            {provider.id === "google_calendar" && isConnected && <CalendarEventsPanel />}
          </div>
        );
      })}
    </div>
  );
}
