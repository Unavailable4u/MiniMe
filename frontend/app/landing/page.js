import LandingPage from "../components/LandingPage";

// Standalone marketing route. Deliberately NOT the "/" route: "/" and its
// sign-in flow (page.js -> AuthProvider -> Gate.jsx -> LoginScreen/AppShell)
// are untouched by this page, so nothing here reads or writes Supabase
// auth state. This page only decides what to *link to* -- see the CTA
// hrefs in LandingPage.jsx, which point at "/" itself.
//
// That's what makes "already has an account" vs "doesn't yet" work
// correctly without this page needing to know which case it is: "/" already
// contains that exact branch (Gate.jsx renders AppShell for a signed-in
// user, LoginScreen otherwise), so sending every visitor from here to "/"
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
