import { AuthProvider } from "./context/AuthContext";
import Gate from "./components/auth/Gate";
import { supabaseServer } from "./lib/supabaseServer";

// Perf audit §2.3 step C: this used to be "use client" solely so Gate()
// could call useAuth() and block on a client-side supabase-js session
// fetch before deciding what to render — meaning AppShell's and
// LoginScreen's JS both had to ship to the browser (inside AuthProvider's
// client boundary) before that decision was even made, and every load
// showed the "Loading…" state for however long that fetch took.
//
// Page is now a Server Component: the auth check happens here, on the
// server, via the session cookie middleware.js keeps fresh. The result
// (initialUser) seeds AuthProvider so Gate.jsx's client-side check
// resolves immediately instead of re-fetching and re-flashing on mount.
// Gate.jsx still exists as a client component to catch sign-out
// happening mid-session — this server check only runs once, on the
// initial request.
export default async function Page() {
  const supabase = await supabaseServer();
  // Deliberately getUser(), not getSession() — validates the token
  // against Supabase rather than trusting whatever the cookie claims,
  // since this decides which component tree renders at all.
  const {
    data: { user },
  } = await supabase.auth.getUser();

  return (
    <AuthProvider initialUser={user}>
      <Gate />
    </AuthProvider>
  );
}
