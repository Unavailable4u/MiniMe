// frontend/app/components/CalendarEventsPanel.jsx
// NEW — Part 8.5 follow-up: direct list/create/delete UI for Google
// Calendar events, hitting GET/POST/DELETE /api/integrations/google_calendar/events
// (api/server.py) straight from the panel. This is deliberately separate
// from the agent's calendar access (agents/calendar_agent.py is also
// used indirectly by task runs) — this component is the user manually
// looking at / editing their calendar from Settings, not the agent
// acting on their behalf mid-task.
//
// Only rendered by IntegrationsPanel once google_calendar shows up in
// GET /api/integrations — there's no point showing an events UI for a
// provider the user hasn't connected, and every endpoint here 409s
// anyway if they haven't (IntegrationNotConnectedError in
// agents/calendar_agent.py).
"use client";
import { useState, useCallback } from "react";
import { Calendar, Plus, Trash2, Loader2, ExternalLink, ChevronDown, ChevronUp } from "lucide-react";
import { useSession, authHeaders } from "../context/SessionContext";

const EMPTY_FORM = { summary: "", start: "", end: "", location: "", description: "" };

// time_min/time_max the backend wants are RFC3339 UTC — <input type="datetime-local">
// gives local wall-clock with no timezone, so this is the one conversion
// point for every request in this file.
function toRFC3339(localDatetimeValue) {
  if (!localDatetimeValue) return null;
  return new Date(localDatetimeValue).toISOString();
}

function formatEventTime(value) {
  if (!value) return "";
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return value; // all-day events come back as plain "YYYY-MM-DD"
  return d.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit",
  });
}

// Default window: now through 7 days out — a reasonable "what's coming up"
// view; the range picker below lets the user widen or shift it.
function defaultRange() {
  const now = new Date();
  const weekOut = new Date(now.getTime() + 7 * 24 * 60 * 60 * 1000);
  const toLocalInputValue = (d) => {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  return { timeMin: toLocalInputValue(now), timeMax: toLocalInputValue(weekOut) };
}

export default function CalendarEventsPanel() {
  const { API_URL } = useSession();
  const [expanded, setExpanded] = useState(false);
  const [range, setRange] = useState(defaultRange);
  const [events, setEvents] = useState(null); // null = not loaded yet
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showForm, setShowForm] = useState(false);
  const [form, setForm] = useState(EMPTY_FORM);
  const [creating, setCreating] = useState(false);
  const [deletingId, setDeletingId] = useState(null);

  const loadEvents = useCallback(async () => {
    const time_min = toRFC3339(range.timeMin);
    const time_max = toRFC3339(range.timeMax);
    if (!time_min || !time_max) return;

    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ time_min, time_max });
      const res = await fetch(`${API_URL}/api/integrations/google_calendar/events?${params}`, {
        headers: await authHeaders(),
      });
      if (res.status === 409) {
        setError("Google Calendar isn't connected. Connect it above first.");
        setEvents([]);
        return;
      }
      if (!res.ok) throw new Error(`Failed to load events (${res.status})`);
      const data = await res.json();
      setEvents(data.events || []);
    } catch (e) {
      setError(String(e.message || e));
      setEvents([]);
    } finally {
      setLoading(false);
    }
  }, [API_URL, range]);

  async function handleToggle() {
    const next = !expanded;
    setExpanded(next);
    if (next && events === null) await loadEvents();
  }

  async function handleCreate(e) {
    e.preventDefault();
    if (!form.summary.trim() || !form.start || !form.end) {
      setError("Title, start, and end are required.");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/integrations/google_calendar/events`, {
        method: "POST",
        headers: await authHeaders({ json: true }),
        body: JSON.stringify({
          summary: form.summary.trim(),
          start: toRFC3339(form.start),
          end: toRFC3339(form.end),
          location: form.location.trim(),
          description: form.description.trim(),
        }),
      });
      if (res.status === 409) throw new Error("Google Calendar isn't connected.");
      if (!res.ok) throw new Error(`Failed to create event (${res.status})`);
      setForm(EMPTY_FORM);
      setShowForm(false);
      await loadEvents();
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setCreating(false);
    }
  }

  async function handleDelete(eventId) {
    setDeletingId(eventId);
    setError(null);
    try {
      const res = await fetch(`${API_URL}/api/integrations/google_calendar/events/${eventId}`, {
        method: "DELETE",
        headers: await authHeaders(),
      });
      if (res.status === 409) throw new Error("Google Calendar isn't connected.");
      if (!res.ok) throw new Error(`Failed to delete event (${res.status})`);
      setEvents((prev) => (prev || []).filter((ev) => ev.id !== eventId));
    } catch (e) {
      setError(String(e.message || e));
    } finally {
      setDeletingId(null);
    }
  }

  return (
    <div className="border border-[var(--neutral-800)] rounded-lg">
      <button
        type="button"
        onClick={handleToggle}
        className="w-full flex items-center justify-between px-3 py-2 text-xs text-[var(--neutral-300)]"
      >
        <span className="flex items-center gap-2">
          <Calendar size={14} className="text-[var(--neutral-500)]" />
          Manage events
        </span>
        {expanded ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
      </button>

      {expanded && (
        <div className="px-3 pb-3 space-y-3 border-t border-[var(--neutral-800)] pt-3">
          {error && <p className="text-xs text-red-400">{error}</p>}

          <div className="flex items-center gap-2">
            <input
              type="datetime-local"
              value={range.timeMin}
              onChange={(e) => setRange((r) => ({ ...r, timeMin: e.target.value }))}
              className="flex-1 text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
            />
            <span className="text-[10px] text-[var(--neutral-600)]">to</span>
            <input
              type="datetime-local"
              value={range.timeMax}
              onChange={(e) => setRange((r) => ({ ...r, timeMax: e.target.value }))}
              className="flex-1 text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
            />
            <button
              type="button"
              onClick={loadEvents}
              disabled={loading}
              className="shrink-0 text-xs rounded-lg px-3 py-1.5 border border-[var(--neutral-800)] text-[var(--neutral-500)] hover:text-[var(--neutral-300)] disabled:opacity-50"
            >
              {loading ? <Loader2 size={12} className="animate-spin" /> : "Refresh"}
            </button>
          </div>

          <div className="space-y-1.5">
            {events === null || loading ? (
              <p className="text-xs text-[var(--neutral-600)]">Loading events…</p>
            ) : events.length === 0 ? (
              <p className="text-xs text-[var(--neutral-600)]">No events in this range.</p>
            ) : (
              events.map((ev) => (
                <div
                  key={ev.id}
                  className="flex items-center justify-between gap-2 border border-[var(--neutral-800)] rounded-lg px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="text-xs text-[var(--neutral-300)] truncate">{ev.summary}</p>
                    <p className="text-[10px] text-[var(--neutral-600)] truncate">
                      {formatEventTime(ev.start)} – {formatEventTime(ev.end)}
                      {ev.location ? ` · ${ev.location}` : ""}
                    </p>
                  </div>
                  <div className="flex items-center gap-1 shrink-0">
                    {ev.html_link && (
                      <a
                        href={ev.html_link}
                        target="_blank"
                        rel="noreferrer"
                        className="p-1.5 text-[var(--neutral-600)] hover:text-[var(--neutral-300)]"
                        title="Open in Google Calendar"
                      >
                        <ExternalLink size={12} />
                      </a>
                    )}
                    <button
                      type="button"
                      onClick={() => handleDelete(ev.id)}
                      disabled={deletingId === ev.id}
                      className="p-1.5 text-[var(--neutral-600)] hover:text-red-400 disabled:opacity-50"
                      title="Delete event"
                    >
                      {deletingId === ev.id ? (
                        <Loader2 size={12} className="animate-spin" />
                      ) : (
                        <Trash2 size={12} />
                      )}
                    </button>
                  </div>
                </div>
              ))
            )}
          </div>

          {showForm ? (
            <form onSubmit={handleCreate} className="space-y-1.5 border-t border-[var(--neutral-800)] pt-3">
              <input
                type="text"
                placeholder="Title"
                value={form.summary}
                onChange={(e) => setForm((f) => ({ ...f, summary: e.target.value }))}
                className="w-full text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
              />
              <div className="flex items-center gap-2">
                <input
                  type="datetime-local"
                  value={form.start}
                  onChange={(e) => setForm((f) => ({ ...f, start: e.target.value }))}
                  className="flex-1 text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
                />
                <span className="text-[10px] text-[var(--neutral-600)]">to</span>
                <input
                  type="datetime-local"
                  value={form.end}
                  onChange={(e) => setForm((f) => ({ ...f, end: e.target.value }))}
                  className="flex-1 text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
                />
              </div>
              <input
                type="text"
                placeholder="Location (optional)"
                value={form.location}
                onChange={(e) => setForm((f) => ({ ...f, location: e.target.value }))}
                className="w-full text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)]"
              />
              <textarea
                placeholder="Description (optional)"
                value={form.description}
                onChange={(e) => setForm((f) => ({ ...f, description: e.target.value }))}
                rows={2}
                className="w-full text-xs bg-transparent border border-[var(--neutral-800)] rounded-lg px-2 py-1.5 text-[var(--neutral-300)] resize-none"
              />
              <div className="flex items-center gap-2">
                <button
                  type="submit"
                  disabled={creating}
                  className="text-xs rounded-lg px-3 py-1.5 bg-[var(--accent)] text-[var(--accent-text)] font-medium disabled:opacity-50"
                >
                  {creating ? <Loader2 size={12} className="animate-spin" /> : "Create event"}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setShowForm(false);
                    setForm(EMPTY_FORM);
                  }}
                  className="text-xs rounded-lg px-3 py-1.5 text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                >
                  Cancel
                </button>
              </div>
            </form>
          ) : (
            <button
              type="button"
              onClick={() => setShowForm(true)}
              className="flex items-center gap-1.5 text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)] border border-[var(--neutral-800)] rounded-lg px-3 py-1.5"
            >
              <Plus size={12} />
              New event
            </button>
          )}
        </div>
      )}
    </div>
  );
}
