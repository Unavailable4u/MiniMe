// frontend/app/lib/supabaseClient.js
//
// Part 8.9: one shared Supabase client for the whole frontend. Both
// AuthContext.jsx (login/signup/logout, session state) and
// SessionContext.jsx (authHeaders(), reading the current access_token for
// every existing fetch() call) import this same instance — a second,
// independently-created client would maintain its own separate in-memory
// session and silently drift out of sync with the first (e.g. one
// refreshing a token the other doesn't know about).
import { createClient } from "@supabase/supabase-js";

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

export const supabase = createClient(supabaseUrl, supabaseAnonKey, {
  auth: {
    persistSession: true,       // survives a page refresh
    autoRefreshToken: true,     // keeps access_token fresh in the background
    detectSessionInUrl: true,   // needed for email-link / OAuth-style sign-in redirects
  },
});
