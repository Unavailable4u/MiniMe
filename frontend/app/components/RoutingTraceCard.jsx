"use client";

export default function RoutingTraceCard({ decision }) {
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
