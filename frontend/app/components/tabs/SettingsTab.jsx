"use client";
import { memo } from "react";
import { useSession } from "../../context/SessionContext";
import { useDensity } from "../../hooks/useDensity";
import { useProactiveSuggestions } from "../../hooks/useProactiveSuggestions";   // NEW — Phase 3 step 3.7
import IntegrationsPanel from "../IntegrationsPanel";   // NEW — Part 8.5/8.9
import AuditLogTab from "./AuditLogTab";   // NEW — Part 8.6: audit log

const DENSITY_OPTIONS = [
  { id: "comfortable", label: "Comfortable" },
  { id: "compact", label: "Compact" },
];

function SettingsTab() {
  const { sessionId, API_URL, registerProject, pusherConnected } = useSession();
  const [density, setDensity] = useDensity();
  const [proactiveSuggestions, setProactiveSuggestions] = useProactiveSuggestions();   // NEW — Phase 3 step 3.7
  return (
    // CHANGED — Phase 3 (mobile cheap wins): px/py were hard-coded (1rem/
    // 1.5rem). This is genuinely cosmetic-only (spacing, touch targets —
    // no different component tree), so per useViewport.js's own file-header
    // rule it stays inline in this file rather than forking into
    // components/mobile/ — first real consumer of the `--viewport-*`
    // tokens globals.css defined back in Phase 0 but nothing used yet.
    // py-6 already equalled the desktop token (1.5rem) so this is a no-op
    // there; px-4 (1rem) is slightly below it, so desktop gains ~8px of
    // side padding to match py — deliberate, not a side effect. Mobile
    // gets tighter padding (0.75rem) than either.
    <div className="h-full overflow-y-auto px-[var(--viewport-content-padding)] py-[var(--viewport-content-padding)] max-w-xl mx-auto space-y-6 text-sm">
      <section>
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Appearance</h2>
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs text-[var(--neutral-300)]">Density</p>
            <p className="text-[var(--neutral-600)] text-xs mt-0.5">
              Compact tightens padding and line spacing across role
              cards, template cards, and chat messages.
            </p>
          </div>
          <div className="flex shrink-0 rounded-lg border border-[var(--neutral-800)] p-0.5 gap-0.5">
            {DENSITY_OPTIONS.map((opt) => (
              <button
                key={opt.id}
                type="button"
                onClick={() => setDensity(opt.id)}
                className={`text-xs rounded-md px-2.5 min-h-[var(--viewport-touch-target)] flex items-center justify-center transition-colors ${
                  density === opt.id
                    ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                    : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      </section>
      <section>
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Chat</h2>
        <div className="flex items-center justify-between gap-3">
          <div className="min-w-0">
            <p className="text-xs text-[var(--neutral-300)]">Proactive suggestions</p>
            <p className="text-[var(--neutral-600)] text-xs mt-0.5">
              Offer a follow-up generation after one finishes (e.g. a quiz
              after flashcards), and mention related topics you haven&apos;t
              covered yet. Always a one-tap offer — nothing generates
              without you accepting it.
            </p>
          </div>
          <div className="flex shrink-0 rounded-lg border border-[var(--neutral-800)] p-0.5 gap-0.5">
            {[{ id: true, label: "On" }, { id: false, label: "Off" }].map((opt) => (
              <button
                key={String(opt.id)}
                type="button"
                onClick={() => setProactiveSuggestions(opt.id)}
                className={`text-xs rounded-md px-2.5 min-h-[var(--viewport-touch-target)] flex items-center justify-center transition-colors ${
                  proactiveSuggestions === opt.id
                    ? "bg-[var(--accent)] text-[var(--accent-text)] font-medium"
                    : "text-[var(--neutral-500)] hover:text-[var(--neutral-300)]"
                }`}
              >
                {opt.label}
              </button>
            ))}
          </div>
        </div>
      </section>
      <section>
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Project</h2>
        <button
          onClick={registerProject}
          className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)] border border-[var(--neutral-800)] rounded-lg px-3 min-h-[var(--viewport-touch-target)] inline-flex items-center"
        >
          + Register external project
        </button>
        <p className="text-[var(--neutral-600)] text-xs mt-2">
          Registers a folder for cross-project control (tier 2 tasks can then
          target it via <code>project_unique_name</code>).
        </p>
      </section>
      <section>
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Integrations</h2>
        <IntegrationsPanel />
      </section>
      <section>
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Activity</h2>
        <div className="border border-[var(--neutral-800)] rounded-lg">
          <AuditLogTab />
        </div>
      </section>
      <section>
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Connection</h2>
        <dl className="text-xs text-[var(--neutral-500)] space-y-1">
          <div className="flex justify-between"><dt>API URL</dt><dd>{API_URL}</dd></div>
          <div className="flex justify-between">
            <dt>Live events (Pusher)</dt>
            <dd className={pusherConnected ? "text-emerald-500" : "text-amber-500"}>
              {pusherConnected ? "connected" : "not configured"}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt>Session ID</dt>
            <dd className="truncate max-w-[60%] cursor-default" title={sessionId || ""}>{sessionId}</dd>
          </div>
        </dl>
      </section>
    </div>
  );
}

// Item 6 (perf audit, tab-body pilot): SettingsTab takes no props from its
// parent -- everything it reads comes from useSession()/useDensity()/
// useProactiveSuggestions() -- so memo here only skips a re-render forced
// by an unrelated parent re-render; it still updates normally whenever any
// of those hooks' own values change.
export default memo(SettingsTab);
