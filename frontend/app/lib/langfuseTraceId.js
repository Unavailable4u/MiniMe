// D1 patch 5 — derives the SAME trace_id backend/eo/executor.py's
// _open_session_trace() opens, so the frontend can build a Langfuse deep
// link with no new backend endpoint/lookup table.
//
// IMPORTANT: trace_id is NOT session_id. Langfuse's Python SDK
// (Langfuse.create_trace_id(seed=...), which _open_session_trace() calls
// with seed=session_id) is documented as "deterministic" but that means
// "same seed -> same id", not "id equals seed" -- the actual algorithm
// (confirmed against Langfuse's own migration-cookbook source, which
// reimplements the identical helper) is:
//
//     sha256(seed.encode("utf-8")).hexdigest()[:32]
//
// A link built from the raw session_id would 404 in Langfuse, since a
// session_id (e.g. a Supabase chat row id) is essentially never itself a
// 32-char lowercase hex string. This module reproduces that exact
// derivation client-side via SubtleCrypto so the link matches the trace
// the backend actually opened.
//
// SubtleCrypto.digest is only available in a "secure context" (https, or
// http://localhost for local dev) -- deriveLangfuseTraceId() resolves to
// null when unavailable, and callers (TracesPanel.jsx) treat null the
// same as "nothing to link to yet" rather than throwing.
export async function deriveLangfuseTraceId(seed) {
  if (!seed) return null;
  if (typeof crypto === "undefined" || !crypto.subtle) return null;
  try {
    const bytes = new TextEncoder().encode(seed);
    const digest = await crypto.subtle.digest("SHA-256", bytes);
    const hex = Array.from(new Uint8Array(digest))
      .map((b) => b.toString(16).padStart(2, "0"))
      .join("");
    return hex.slice(0, 32);
  } catch {
    return null; // fire-and-forget, same posture as this app's other optional-enhancement fetches
  }
}
