// frontend/app/components/UsagePanel.jsx
// Patch X02 (M2C build plan, WP X02 -- Usage panel).
//
// "Your credits": what the account has used this period, what is left, when it
// resets, a bar for used out of the pool, and where the credits went (web app,
// M2C, and Menux once it has any). One account, one pool, one ledger (build
// plan section O2): the web app and M2C draw from the same pool, so this is
// the one place that shows it. It sits at the top of the Token Usage tab, above
// the older per-provider panels, which describe provider rate limits rather
// than the account's plan.
//
// Reads GET /api/usage (backend/api/routes/usage.py, patch S01). The response
// shape, the clamping rules and every string shown live in lib/usage.js, which
// is the part plain `node` can test (lib/__tests__/usage.test.mjs); this file is
// only state, one fetch and markup. Credits only -- users never see dollars
// (Rulebook section 7.2), and nothing here reads a dollar field.
//
// TWO PIECES in this file:
//   UsagePanelView (named export)  markup only: takes the finished view model,
//                                  an error string or a loading flag, and draws
//                                  it. No hooks, so it can be rendered to HTML
//                                  for each state without a browser.
//   UsagePanel (default export)    owns the request and the state, and hands
//                                  the result to UsagePanelView.
//
// THE STATES, all drawn from one view model (describeUsage):
//   loading        first load: one dim line (a refresh keeps the numbers up)
//   error          401 (signed out), 503 (unavailable), network, or a 200 whose
//                  body is not usable: a fixed sentence and "Try again". Stale
//                  numbers are not left on screen next to an error.
//   no usage yet   an empty bar, the whole pool left, a friendly line
//   normal         the three numbers, the bar, the by-surface list
//   warning        at 70% used: amber bar and a line saying so and when it resets
//   exhausted      nothing left (or a pool of 0): red bar and the reset date
//
// NOT SHOWN: "what a run costs roughly", the fourth number in the plan's
// wording. GET /api/usage has no field for it and this page does not invent
// one; see the header of lib/usage.js.
//
// Requests: authedFetch + authHeaders, as DevicesPanel does, so a 401 gets one
// forced session refresh and a 503 gets one bounded retry before an error shows.
// Only the newest response may set state, so a slow earlier request cannot
// overwrite a newer one.
"use client";
import { useState, useEffect, useCallback, useRef } from "react";
import { authedFetch, authHeaders } from "../lib/authFetch";
import { describeUsage, usageErrorMessage } from "../lib/usage";

// One authenticated request -> {ok, status, data}. Never throws: a network
// failure is status 0, which usageErrorMessage words as such.
async function request(apiUrl) {
  try {
    const res = await authedFetch(`${apiUrl}/api/usage`, { headers: await authHeaders() });
    let data = null;
    try {
      data = await res.json();
    } catch {
      // an empty or non-JSON body: the status is what matters
    }
    return { ok: res.ok, status: res.status, data };
  } catch {
    return { ok: false, status: 0, data: null };
  }
}

// The app's own accent (cyber-cyan) is crimson, which would make a healthy bar
// look like an exhausted one, so the normal state is green and the bar reads
// green -> amber -> red as the pool drains.
const BAR_CLASS = {
  ok: "bg-emerald-500 shadow-[0_0_8px_rgba(16,185,129,0.5)]",
  warning: "bg-amber-500 shadow-[0_0_8px_rgba(245,158,11,0.6)]",
  exhausted: "bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.6)]",
};

const MESSAGE_CLASS = {
  ok: "text-cyber-dim",
  warning: "text-amber-500",
  exhausted: "text-rose-400",
};

const REFRESH_BUTTON =
  "font-display text-[10px] uppercase tracking-wide rounded px-2 py-1 border border-cyber-border text-cyber-dim hover:text-cyber-text disabled:opacity-50";

function Figure({ label, value, note }) {
  return (
    <div className="min-w-0">
      <dt className="font-display text-[10px] uppercase tracking-wide text-cyber-dim">{label}</dt>
      <dd className="text-sm text-cyber-text break-words">{value}</dd>
      {note && <dd className="text-[11px] text-cyber-dim">{note}</dd>}
    </div>
  );
}

/** Markup only. `view` is describeUsage()'s result (or null), `error` a fixed
 *  sentence from usageErrorMessage (or null), `loading` true while a request is
 *  out, `onRefresh` what the two buttons call. An error wins over a view: the
 *  container clears the view when it sets an error, and this checks again so a
 *  caller cannot put stale numbers next to an error message. */
export function UsagePanelView({ view, error, loading, onRefresh }) {
  const shown = error ? null : view;
  return (
    <div className="cyber-panel p-4" role="region" aria-label="Your credits">
      <div className="flex items-center justify-between mb-2">
        <h3 className="font-display text-[11px] uppercase tracking-wide text-cyber-dim">
          Your credits{shown && shown.planLabel ? ` · ${shown.planLabel} plan` : ""}
        </h3>
        <button type="button" onClick={onRefresh} disabled={loading} className={REFRESH_BUTTON}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {loading && !shown && !error && <p className="text-xs text-cyber-dim">Loading your usage…</p>}

      {error && (
        <div className="space-y-2">
          <p role="alert" className="text-xs text-rose-400">{error}</p>
          <button type="button" onClick={onRefresh} disabled={loading} className={REFRESH_BUTTON}>
            Try again
          </button>
        </div>
      )}

      {shown && (
        <>
          <dl className="grid grid-cols-1 sm:grid-cols-3 gap-3">
            <Figure label="Used" value={shown.usedText} />
            <Figure label="Left" value={shown.remainingText} />
            <Figure label="Resets" value={shown.resetDate || "Date not available"} note={shown.resetsIn} />
          </dl>

          <div
            role="progressbar"
            aria-label="Credits used this period"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={shown.percent}
            aria-valuetext={shown.showPercent ? `${shown.percent}% of your credits used` : "No credits available"}
            className="h-1.5 rounded-full bg-black/50 border border-cyber-border overflow-hidden mt-3"
          >
            <div
              className={`h-full rounded-full transition-all ${BAR_CLASS[shown.level]}`}
              style={{ width: `${shown.percent}%` }}
            />
          </div>
          <div className="flex items-center justify-between mt-1 text-[11px] text-cyber-dim">
            <span>{shown.showPercent ? `${shown.percent}% used` : "No credits on this plan"}</span>
            <span>Pool: {shown.poolText}</span>
          </div>

          <div aria-live="polite">
            {shown.message && (
              <p role="status" className={`text-xs mt-2 ${MESSAGE_CLASS[shown.level]}`}>
                {shown.message}
              </p>
            )}
          </div>

          <div className="mt-3 pt-2 border-t border-cyber-border">
            <p className="font-display text-[10px] uppercase tracking-wide text-cyber-dim mb-1">
              Where your credits went
            </p>
            <ul className="space-y-1">
              {shown.surfaces.map((s) => (
                <li key={s.key} className="flex items-center justify-between text-[11px] font-mono">
                  <span className="text-cyber-dim">{s.label}</span>
                  <span className="text-cyber-text">{s.text}</span>
                </li>
              ))}
            </ul>
            <p className="text-[10px] text-cyber-dim mt-2">
              The web app and M2C draw from the same pool, so what you use in either counts here.
            </p>
          </div>
        </>
      )}
    </div>
  );
}

export default function UsagePanel({ apiUrl }) {
  const [view, setView] = useState(null); // describeUsage() of the last good response
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const loadSeq = useRef(0); // only the newest response may set state
  const mounted = useRef(true);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    setLoading(true);
    const r = await request(apiUrl);
    if (!mounted.current || seq !== loadSeq.current) return;
    if (r.ok) {
      // No locale argument: Intl uses the browser's own language, so credits
      // and dates read the way this person's browser is set up.
      const next = describeUsage(r.data);
      if (next) {
        setView(next);
        setError(null);
      } else {
        setView(null);
        setError(usageErrorMessage({ status: r.status, unreadable: true }));
      }
    } else {
      setView(null);
      setError(usageErrorMessage(r));
    }
    setLoading(false);
  }, [apiUrl]);

  useEffect(() => {
    mounted.current = true;
    load();
    return () => {
      mounted.current = false;
    };
  }, [load]);

  return <UsagePanelView view={view} error={error} loading={loading} onRefresh={load} />;
}
