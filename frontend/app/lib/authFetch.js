// frontend/app/lib/authFetch.js
//
// BUGFIX (sign-out -> sign-in 401 storm): previously this exact function
// was defined three times — inline in SessionContext.jsx, ChatListContext.jsx,
// and WorkspacesContext.jsx — each reading `supabase.auth.getSession()` fresh
// on every call. That part was already correct (no stale-token closures, no
// axios-defaults-style caching). The actual bug is a documented Supabase
// footgun: getSession() "loads values directly from the storage attached to
// the client" and does NOT validate them — it can return a token that looks
// present but that the backend legitimately rejects, which is exactly the
// window right after a sign-out -> sign-in transition (a fresh SIGNED_IN
// event has just fired and this component's mount effect fires its
// bootstrap fetches in the same tick). The backend then correctly 401s with
// "Invalid token", and — because none of the three call sites ever retried —
// the sidebar/batches/workspaces just silently stayed empty.
//
// Fix: keep authHeaders() doing a live lookup (unchanged behavior/signature,
// still no unnecessary round trip on the common path), but wrap the fetch
// itself in authedFetch(), which retries exactly once — after forcing a
// real, server-validated supabase.auth.refreshSession() — whenever the
// first attempt comes back 401. A second 401 after a genuine refresh means
// the user really is signed out (or the refresh token itself is dead), and
// is returned as-is for the caller's existing error handling to deal with.
//
// Pulled into its own module (rather than staying duplicated, or living in
// any one of the three context files) specifically so it has NO dependency
// on SessionContext/ChatListContext/WorkspacesContext — those three have a
// real circularity constraint on each other (see WorkspacesContext.jsx's
// and ChatListContext.jsx's own header comments on why they sit ABOVE
// SessionProvider), so a shared helper any of them can import safely has to
// live below all three, next to the supabaseClient.js singleton itself.
import { supabase } from "./supabaseClient";

export async function authHeaders(opts = {}) {
  const { data: { session } } = await supabase.auth.getSession();
  const token = session?.access_token;
  const headers = {};
  if (opts.json) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  return headers;
}

// Drop-in replacement for `fetch(url, init)` everywhere `init.headers` was
// built from authHeaders(). Same return shape (a Response), so existing
// `res.ok`/`res.json()` call sites don't need to change at all — only the
// function name at the call site does.
export async function authedFetch(url, init = {}) {
  let res = await fetch(url, init);
  if (res.status !== 401) return res;

  // getSession() above already returned *a* token, so a 401 here means
  // the backend didn't accept it -- force a real refresh (a network round
  // trip to Supabase, not a storage read) rather than assuming the user
  // is actually signed out.
  let refreshed;
  try {
    refreshed = await supabase.auth.refreshSession();
  } catch {
    return res; // refresh itself threw (e.g. no session at all) -- surface the original 401
  }
  const newToken = refreshed?.data?.session?.access_token;
  if (refreshed?.error || !newToken) return res; // genuinely signed out / dead refresh token -- surface the original 401

  const retryHeaders = { ...(init.headers || {}), Authorization: `Bearer ${newToken}` };
  return fetch(url, { ...init, headers: retryHeaders });
}
