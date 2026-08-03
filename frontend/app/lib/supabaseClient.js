// frontend/app/lib/supabaseClient.js
//
// Part 8.9: one shared Supabase client for the whole frontend. Both
// AuthContext.jsx (login/signup/logout, session state) and
// SessionContext.jsx (authHeaders(), reading the current access_token for
// every existing fetch() call) import this same instance — a second,
// independently-created client would maintain its own separate in-memory
// session and silently drift out of sync with the first (e.g. one
// refreshing a token the other doesn't know about).
//
// Perf audit §2.3 step C: switched from supabase-js's bare createClient()
// to @supabase/ssr's createBrowserClient(). Behaves the same day-to-day
// (still persists across reloads, still auto-refreshes, still handles
// email-link redirects) but stores the session in a cookie instead of
// localStorage — that's what lets app/lib/supabaseServer.js and
// middleware.js read the same session on the server. A plain
// createClient() session is invisible to the server; this one isn't.
import { createBrowserClient } from "@supabase/ssr";

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

if (!supabaseUrl || !supabaseAnonKey) {
  // Loud, not silent — a missing env var here means every fetch() in
  // SessionContext.jsx will send no Authorization header at all and get
  // 401s that look unrelated to their actual cause. Throwing at import
  // time (module load, not render) surfaces this immediately in the
  // console/build instead of as a confusing runtime 401 later.
  console.error(
    "Missing NEXT_PUBLIC_SUPABASE_URL / NEXT_PUBLIC_SUPABASE_ANON_KEY. " +
    "These belong in frontend/.env.local (public, browser-exposed values — " +
    "not the backend's root .env). See the Supabase project's API settings page."
  );
}

export const supabase = createBrowserClient(supabaseUrl, supabaseAnonKey);
