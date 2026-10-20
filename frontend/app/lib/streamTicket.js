// frontend/app/lib/streamTicket.js
//
// Auth for the two browser transports that can't send an Authorization
// header: the /ws/{session_id} WebSocket (SessionContext.jsx) and the
// /api/task/{session_id}/stream EventSource (MessageBubble.jsx).
//
// Both used to put the user's full Supabase access token in the URL as
// `?token=<jwt>`, which leaks a whole-API bearer credential into access
// logs, proxies and error reports. They now open with `?ticket=<ticket>`:
// a random, single-use value the backend mints on request
// (POST /api/stream-ticket, see backend/api/stream_tickets.py), bound to
// one transport + one session and dead after one use or ~30 seconds.
//
// Because a ticket is single-use, EVERY connection attempt must mint its
// own -- never cache one, never reuse it for a reconnect.
//
// Deliberately dependency-free (no imports): the caller passes in its
// fetch and header builders (authFetch.js's authedFetch / authHeaders), so
// this file is testable under plain `node` via loadSource.mjs and never
// pulls in the Supabase client. The mint request itself carries a normal
// Authorization header; that's the one place the real token is presented.

/**
 * Ask the backend for a single-use ticket.
 *
 * @param {object} args
 * @param {string} args.apiUrl      backend origin, e.g. NEXT_PUBLIC_API_URL
 * @param {"ws"|"sse"} args.kind    which transport the ticket is for
 * @param {string} args.sessionId   the session/chat the transport will open on
 * @param {Function} args.fetchFn   (url, init) => Promise<Response> -- authedFetch
 * @param {Function} args.getHeaders async ({ json }) => headers -- authHeaders
 * @returns {Promise<string|null>}  the ticket, or null on ANY failure
 *   (not signed in, 404, 503, network error). Callers treat null as
 *   "don't open the stream", same as they did when there was no token.
 */
export async function requestStreamTicket({ apiUrl, kind, sessionId, fetchFn, getHeaders }) {
  try {
    const res = await fetchFn(`${apiUrl}/api/stream-ticket`, {
      method: "POST",
      headers: await getHeaders({ json: true }),
      body: JSON.stringify({ kind, session_id: sessionId }),
    });
    if (!res || !res.ok) return null;
    const body = await res.json();
    return typeof body?.ticket === "string" && body.ticket ? body.ticket : null;
  } catch {
    return null;
  }
}

/** ws(s):// URL for the /ws/{session_id} socket, authenticated by `ticket`. */
export function buildWsUrl(apiUrl, sessionId, ticket) {
  return `${apiUrl.replace(/^http/, "ws")}/ws/${encodeURIComponent(sessionId)}?ticket=${encodeURIComponent(ticket)}`;
}

/** URL for the SSE answer stream, authenticated by `ticket`. */
export function buildSseUrl(apiUrl, sessionId, ticket) {
  return `${apiUrl}/api/task/${encodeURIComponent(sessionId)}/stream?ticket=${encodeURIComponent(ticket)}`;
}
