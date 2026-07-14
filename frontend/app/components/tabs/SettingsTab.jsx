"use client";
import { useSession } from "../../context/SessionContext";
import { useDensity } from "../../hooks/useDensity";
import IntegrationsPanel from "../IntegrationsPanel";   // NEW — Part 8.5/8.9

const DENSITY_OPTIONS = [
  { id: "comfortable", label: "Comfortable" },
  { id: "compact", label: "Compact" },
];

export default function SettingsTab() {
  const { sessionId, API_URL, registerProject, pusherConnected } = useSession();
  const [density, setDensity] = useDensity();
  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-xl mx-auto space-y-6 text-sm">
      <section>
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Appearance</h2>
        <div className="flex items-center justify-between">
          <div>
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
                className={`text-xs rounded-md px-2.5 py-1 transition-colors ${
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
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Project</h2>
        <button
          onClick={registerProject}
          className="text-xs text-[var(--neutral-500)] hover:text-[var(--neutral-300)] border border-[var(--neutral-800)] rounded-lg px-3 py-1.5"
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
        <h2 className="text-[var(--neutral-400)] font-medium mb-2">Connection</h2>
        <dl className="text-xs text-[var(--neutral-500)] space-y-1">
          <div className="flex justify-between"><dt>API URL</dt><dd>{API_URL}</dd></div>
          <div className="flex justify-between">
            <dt>Live events (Pusher)</dt>
            <dd className={pusherConnected ? "text-emerald-500" : "text-amber-500"}>
              {pusherConnected ? "connected" : "not configured"}
            </dd>
          </div>
          <div className="flex justify-between"><dt>Session ID</dt><dd className="truncate max-w-[60%]">{sessionId}</dd></div>
        </dl>
      </section>
    </div>
  );
}