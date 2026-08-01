"use client";

// Phase 8 §8.5 — one-line routing "why", built entirely from fields the
// routing_decision event already carries (eo/inspector.py's `parsed` /
// eo/panel.py's synthesis) — no new backend event or field needed.
//
// `execution_order` is the hires-driven roster SaveRunAsTemplate already
// treats as "the real agent list for this run" (only populated once
// staff_task() has actually hired); `suggested_agents` is the
// Inspector/Panel's own pre-hire estimate, used as a fallback for tier 0/1
// runs (or anything caught before hires exist) so the line never reads
// "0 agents" for a run that plainly did something.
//
// Deliberately doesn't reference eo/modes.py's user-selected mode
// (auto/simple/fast/expert/beast) — that lives only in the legacy
// SessionContext's `mode` state today, not in per-dock state (see
// WorkspaceDockContext.jsx's sendTask() comment), so a dock-mode caller
// (e.g. Notebooks) has no reliable value to read here. Tier + staffed
// count is the honest subset of "why" available in every mode today.
function routingWhy(decision) {
  const tier = decision.tier;
  const agentCount = decision.execution_order?.length || decision.suggested_agents?.length || 0;
  let label;
  if (tier === 0) {
    label = "Instant response — no pipeline";
  } else if (tier === 1) {
    const n = agentCount || 3;
    label = `Lean pipeline — ${n} agent${n === 1 ? "" : "s"} staffed`;
  } else if (tier === 2) {
    const n = agentCount || 1;
    const task = decision.directed_task_type ? ` (${decision.directed_task_type})` : "";
    label = `Directed task${task} — ${n} specialist${n === 1 ? "" : "s"} staffed`;
  } else if (agentCount > 0) {
    label = `Full pipeline — ${agentCount} agent${agentCount === 1 ? "" : "s"} staffed`;
  } else {
    label = "Full pipeline staffed";
  }
  return decision.panel_reviewed ? `Panel reviewed · ${label}` : label;
}

export default function RoutingTraceCard({ decision }) {
  // server.py returns decision={} on a caught server-side error — nothing
  // to show in that case, and empty-object access below would just print
  // "confidence 0.00" noise for no reason.
  if (!decision || !decision.reasoning) return null;

  const isPanel = decision.panel_reviewed && Array.isArray(decision.panel_votes);
  const pct = (c) => (typeof c === "number" ? c.toFixed(2) : "0.00");

  return (
    <details className="rounded-lg border border-[var(--neutral-800)] bg-[var(--neutral-950-a50)] text-xs">
      <summary className="cursor-pointer select-none px-2 py-1.5 text-[var(--neutral-400)] hover:text-[var(--neutral-300)]">
        <span className={isPanel ? "text-amber-500/80" : "text-[var(--neutral-300)]"}>
          {routingWhy(decision)}
        </span>
        <span className="text-[var(--neutral-600)]"> · routing trace</span>
      </summary>

      <div className="space-y-2 border-t border-[var(--neutral-800)] px-2 pb-2 pt-1.5">
        {isPanel ? (
          <>
            <div className="space-y-1.5">
              {decision.panel_votes.map((v) => (
                <div key={v.member} className="border-l-2 border-[var(--neutral-800)] pl-2">
                  <div className="text-[var(--neutral-500)]">
                    member {v.member} · tier {v.tier} · confidence {pct(v.confidence)}
                    {v.directed_task_type ? ` · ${v.directed_task_type}` : ""}
                  </div>
                  <div className="text-[var(--neutral-400)]">{v.reasoning}</div>
                </div>
              ))}
            </div>
            <div className="border-t border-[var(--neutral-800-a70)] pt-1.5">
              <div className="text-[var(--neutral-500)]">
                synthesis · tier {decision.tier} (max) · confidence {pct(decision.confidence)} (avg) ·{" "}
                {decision.directed_task_type
                  ? decision.directed_task_type
                  : "directed_task_type: none (members disagreed)"}
              </div>
              {decision.suggested_agents?.length > 0 && (
                <div className="mt-0.5 text-[var(--neutral-500)]">
                  agents: {decision.suggested_agents.join(", ")}
                </div>
              )}
            </div>
          </>
        ) : (
          <>
            <div className="text-[var(--neutral-500)]">
              inspector · tier {decision.tier} · confidence {pct(decision.confidence)}
              {decision.directed_task_type ? ` · ${decision.directed_task_type}` : ""}
            </div>
            <div className="text-[var(--neutral-400)]">{decision.reasoning}</div>
          </>
        )}
      </div>
    </details>
  );
}