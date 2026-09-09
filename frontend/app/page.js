import LandingPage from "./components/LandingPage";

// "/" is now the marketing landing page — the first thing every visitor
// sees, whether they're signed in, have an account and haven't signed in
// yet, or are brand new. It deliberately does NOT read or write Supabase
// auth state (no supabaseServer() call here, unlike the old "/" below).
//
// The former "/" (the server-side auth check -> AuthProvider -> Gate.jsx
// -> LoginScreen/AppShell branch) now lives at "/app" -- see
// app/app/page.js. LandingPage's CTA hrefs point at "/app", which is what
// makes "already has an account" vs "doesn't yet" work correctly without
// this page needing to know which case it is: "/app" already contains
// that exact branch (Gate.jsx renders AppShell for a signed-in user,
// LoginScreen otherwise), so sending every visitor from here to "/app"
// reuses that decision instead of duplicating it.
export const metadata = {
  title: "MiniMe — One AI, every stage of the build",
  description:
    "MiniMe carries one idea through planning, research, study, and code — one system, one memory, start to finish.",
};

export const viewport = {
  themeColor: "#0A0A0C",
};

export default function Page() {
  return <LandingPage />;
}
