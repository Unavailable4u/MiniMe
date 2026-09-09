import { AuthProvider } from "../context/AuthContext";
import Gate from "../components/auth/Gate";
import { supabaseServer } from "../lib/supabaseServer";

// Moved from app/page.js ("/") to here ("/app") so the marketing landing
// page can own "/" and be the first thing every visitor sees, signed in
// or not (see app/page.js's own comment, and app/components/LandingPage.jsx's
// CTA hrefs, which now point at "/app" instead of "/").
//
// Nothing about the auth check itself changed in this move: still a Server
// Component, still checks the session cookie via supabaseServer() before
// anything client-side mounts, still seeds AuthProvider with the result so
// Gate.jsx's client-side check resolves immediately instead of re-fetching
// and re-flashing on mount. Gate.jsx still exists as a client component to
// catch sign-out happening mid-session -- this server check only runs once,
// on the initial request to /app.
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
