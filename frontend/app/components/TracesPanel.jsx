"use client";
import { useEffect, useState } from "react";
import { ExternalLink } from "lucide-react";
import { deriveLangfuseTraceId } from "../lib/langfuseTraceId";

// D1 patch 5 — extends RoutingTraceGraph.jsx's routing view with a
// "Traces" panel, per the guide's D1 note. Deliberately its own small
// component rather than folded into RoutingTraceGraph.jsx itself:
// _open_session_trace() (backend/eo/executor.py, patch 3a) opens ONE
// Langfuse trace per session_id, covering every message/run in that
// chat -- not one trace per RoutingTraceGraph instance, which is
// per-message. Rendered once per dock in WorkingPanel.jsx, not inside
// the per-message map.
//
// project_id: NEXT_PUBLIC_LANGFUSE_PROJECT_ID, read once here (module
// scope) rather than per-request -- same "set once" convention
// NEXT_PUBLIC_SENTRY_DSN/NEXT_PUBLIC_SUPABASE_URL already use in
// frontend/.env.example. Unset -> this component renders nothing, same
// "blank env var = disabled, nothing else has to branch on it"
// convention backend/eo/tracing.py's TRACING_ENABLED already documents.
const LANGFUSE_PROJECT_ID = process.env.NEXT_PUBLIC_LANGFUSE_PROJECT_ID;
// Matches backend/.env.example's LANGFUSE_BASE_URL default -- kept as
// its own var (not hardcoded) since a self-host or EU-region project
// would need a different host, same reasoning backend/.env.example
// gives for not hardcoding US cloud there.
const LANGFUSE_BASE_URL =
  process.env.NEXT_PUBLIC_LANGFUSE_BASE_URL || "https://us.cloud.langfuse.com";

export default function TracesPanel({ sessionId }) {
  const [traceId, setTraceId] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setTraceId(null); // clear the previous chat's link immediately on switchChat(), don't show a stale one while the new one derives
    if (!sessionId || !LANGFUSE_PROJECT_ID) return;
    (async () => {
      const id = await deriveLangfuseTraceId(sessionId);
      if (!cancelled) setTraceId(id);
    })();
    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  // Nothing configured, no session yet, or derivation unavailable/failed
  // (see deriveLangfuseTraceId's SubtleCrypto note) -- all fire-and-forget
  // no-ops, same posture as WorkingPanel.jsx's nodeBlurbs fetch.
  if (!LANGFUSE_PROJECT_ID || !sessionId || !traceId) return null;

  const href = `${LANGFUSE_BASE_URL}/project/${LANGFUSE_PROJECT_ID}/traces/${traceId}`;

  return (
    <div className="px-3 pt-2 shrink-0">
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        className="inline-flex items-center gap-1 text-[10px] text-[var(--neutral-500)] hover:text-[var(--neutral-200)] transition-colors"
      >
        View trace in Langfuse <ExternalLink size={10} />
      </a>
    </div>
  );
}
