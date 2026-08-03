// frontend/app/lib/supabaseServer.js
//
// Perf audit §2.3 step C. Server-side counterpart to supabaseClient.js —
// same project, but reads/writes the session via Next's cookie store
// (next/headers) instead of localStorage, which is what lets page.js (a
// Server Component) check auth before any client JS ships. Only for use
// in Server Components, Server Actions, and Route Handlers — never
// import this into a "use client" file, next/headers isn't available
// there and it'll fail the build.
//
// Requires supabaseClient.js's browser client to be cookie-backed too
// (createBrowserClient from @supabase/ssr, not supabase-js's bare
// createClient) — otherwise the browser writes its session to
// localStorage and this server client, reading cookies, never sees it.
import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
const supabaseAnonKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

export async function supabaseServer() {
  const cookieStore = await cookies();
  return createServerClient(supabaseUrl, supabaseAnonKey, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          cookiesToSet.forEach(({ name, value, options }) =>
            cookieStore.set(name, value, options)
          );
        } catch {
          // Thrown when called from a plain Server Component render (as
          // opposed to a Server Action or Route Handler) — cookies() is
          // read-only there. Safe to swallow: middleware.js refreshes
          // the session cookie on every request regardless, so a write
          // attempt here isn't the only place that happens.
        }
      },
    },
  });
}
