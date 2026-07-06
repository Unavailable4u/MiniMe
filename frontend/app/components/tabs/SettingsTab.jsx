"use client";
import { useSession } from "../../context/SessionContext";

export default function SettingsTab() {
  const { sessionId, API_URL, registerProject, pusherConnected } = useSession();
  return (
    <div className="h-full overflow-y-auto px-4 py-6 max-w-xl mx-auto space-y-6 text-sm">
      <section>
        <h2 className="text-neutral-400 font-medium mb-2">Project</h2>
        <button
          onClick={registerProject}
          className="text-xs text-neutral-500 hover:text-neutral-300 border border-neutral-800 rounded-lg px-3 py-1.5"
        >
          + Register external project
        </button>
        <p className="text-neutral-600 text-xs mt-2">
          Registers a folder for cross-project control (tier 2 tasks can then
          target it via <code>project_unique_name</code>).
        </p>
      </section>
      <section>
        <h2 className="text-neutral-400 font-medium mb-2">Connection</h2>
        <dl className="text-xs text-neutral-500 space-y-1">
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
